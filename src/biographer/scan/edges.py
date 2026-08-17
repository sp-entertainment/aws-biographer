"""The resource graph: extraction, persistence, and traversal.

An adjacency table in CockroachDB, both directions indexed, walked with
recursive CTEs. Explicitly not a graph database -- at hundreds to low thousands
of resources, and with blast-radius questions that are two or three hops deep, a
recursive CTE outruns a network hop to a separate store and removes a service
that would otherwise have to stay alive through judging.

Three edge sources, and the difference between them is load-bearing:

  config   derived from resource configuration. Certain. Rewritten every scan.
  inferred guessed from convention, e.g. a log group path that matches a
           function name. Probabilistic. A proposal, never a fact.
  human    asserted by a person. Certain. NEVER touched by a scan.

Invariant 7 is why scans only ever delete their own `config` rows, and
invariant 8 is why an `inferred` edge is not permitted to become a confirmed
one without a human saying so.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from .model import Resource

log = logging.getLogger(__name__)

CONFIG = "config"
INFERRED = "inferred"
HUMAN = "human"


@dataclass(frozen=True, slots=True)
class Edge:
    src_arn: str
    dst_arn: str
    edge_type: str
    source: str = CONFIG
    confidence: float | None = None
    note: str | None = None


class Index:
    """Lookup helpers over one scan's resources.

    Config blobs reference other resources by bare id (`sg-0abc`, `vpc-0def`),
    never by ARN, so every extractor needs the same id-to-ARN map. Building it
    once and passing it around beats each extractor rebuilding it.
    """

    def __init__(self, resources: list[Resource]) -> None:
        self.by_arn = {r.arn: r for r in resources}
        self.by_id: dict[str, str] = {}
        self.by_name: dict[tuple[str, str], str] = {}
        for r in resources:
            tail = r.arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            if tail:
                self.by_id.setdefault(tail, r.arn)
            if r.name:
                self.by_name.setdefault((r.service, r.name), r.arn)

    def arn_for(self, identifier: Any) -> str | None:
        if not identifier or not isinstance(identifier, str):
            return None
        if identifier.startswith("arn:"):
            return identifier if identifier in self.by_arn else None
        return self.by_id.get(identifier)


def _cfg(resource: Resource, key: str) -> Any:
    return (resource.config or {}).get(key)


def extract(resources: list[Resource]) -> list[Edge]:
    """Every edge derivable from this scan's configuration.

    Coverage is a long tail and completeness is explicitly not the goal. These
    are the edge types that answer the questions the product actually asks:
    what breaks if I delete this, and what is this thing attached to.
    """
    index = Index(resources)
    edges: list[Edge] = []

    def link(src: str, dst_id: Any, edge_type: str) -> None:
        dst = index.arn_for(dst_id)
        if dst and dst != src:
            edges.append(Edge(src, dst, edge_type))

    for r in resources:
        kind = (r.service, r.resource_type)

        if kind == ("ec2", "instance"):
            link(r.arn, _cfg(r, "SubnetId"), "in_subnet")
            link(r.arn, _cfg(r, "VpcId"), "in_vpc")
            link(r.arn, _cfg(r, "IamInstanceProfile"), "uses_role")
            for group in _cfg(r, "SecurityGroups") or []:
                link(r.arn, group, "protected_by")

        elif kind == ("ec2", "volume"):
            for instance in _cfg(r, "AttachedTo") or []:
                link(r.arn, instance, "attached_to")

        elif kind == ("ec2", "address"):
            link(r.arn, _cfg(r, "InstanceId"), "attached_to")
            link(r.arn, _cfg(r, "NetworkInterfaceId"), "attached_to")

        elif kind == ("ec2", "security-group"):
            link(r.arn, _cfg(r, "VpcId"), "in_vpc")

        elif kind == ("ec2", "subnet"):
            link(r.arn, _cfg(r, "VpcId"), "in_vpc")

        elif kind == ("ec2", "route-table"):
            link(r.arn, _cfg(r, "VpcId"), "in_vpc")
            for subnet in _cfg(r, "Associations") or []:
                link(r.arn, subnet, "routes_for")
            for route in _cfg(r, "Routes") or []:
                link(r.arn, route.get("GatewayId"), "routes_via")

        elif kind == ("ec2", "internet-gateway"):
            for vpc in _cfg(r, "Attachments") or []:
                link(r.arn, vpc, "in_vpc")

        elif kind == ("lambda", "function"):
            link(r.arn, _cfg(r, "Role"), "uses_role")
            for group in (_cfg(r, "VpcConfig") or {}).get("SecurityGroupIds", []) or []:
                link(r.arn, group, "protected_by")
            for subnet in (_cfg(r, "VpcConfig") or {}).get("SubnetIds", []) or []:
                link(r.arn, subnet, "in_subnet")

        elif kind == ("elasticloadbalancing", "load-balancer"):
            link(r.arn, _cfg(r, "VpcId"), "in_vpc")
            for subnet in _cfg(r, "AvailabilityZones") or []:
                link(r.arn, subnet, "in_subnet")

        elif kind == ("rds", "db-instance"):
            link(r.arn, _cfg(r, "DBClusterIdentifier"), "member_of")

    edges.extend(infer(resources, index))
    return edges


def infer(resources: list[Resource], index: Index) -> Iterator[Edge]:
    """Convention-based guesses. Proposals, never facts.

    AWS creates a log group at /aws/lambda/<function-name> by convention, but
    nothing in either resource's configuration records the link -- the only
    evidence is the name. That makes it exactly the kind of relationship
    invariant 8 says may be proposed and may not be asserted.
    """
    for r in resources:
        if (r.service, r.resource_type) != ("logs", "log-group"):
            continue
        name = _cfg(r, "logGroupName") or r.name or ""
        if not name.startswith("/aws/lambda/"):
            continue
        function = name.removeprefix("/aws/lambda/")
        target = index.by_name.get(("lambda", function))
        if target:
            yield Edge(
                src_arn=target,
                dst_arn=r.arn,
                edge_type="logs_to",
                source=INFERRED,
                confidence=0.9,
                note="log group path matches function name",
            )
