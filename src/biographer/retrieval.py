"""Four-lane retrieval over CockroachDB.

Design summary §7 left lane selection and ranking open and said not to default
to top-k vector search. Two decisions carry this module:

**Routing is deterministic, not a model call.** The signals that say which lane
applies -- does the question contain an ARN, a region, a service name -- are
regex-cheap and unambiguous. Spending a model call and its latency to decide
what a regex already knows would be slower, costlier, and less predictable. The
model is for reasoning about results, not for routing.

**Fusion is Reciprocal Rank Fusion.** Lane scores are not comparable: cosine
distance is a float in one range, a SQL predicate match is a boolean, a graph
hop is an integer depth. Normalising them against each other would require
tuning constants that nothing in the data justifies. RRF needs only *ranks*, so
it fuses incomparable lanes without inventing a shared scale, and it is about
fifteen lines.

Lane weights, recency, and human provenance then adjust the fused score, in that
order of magnitude -- weights matter most, provenance least, and none of them
can promote a result no lane returned.

The graph lane is what answers design summary §5's "memory blindness" failure
mode. If the fact you needed was the eleventh vector result, you do not need a
better embedding -- you need to walk two edges from the right node.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .bedrock import embed
from .db import pool, to_vector
from .graph import neighbours

log = logging.getLogger(__name__)

# RRF's damping constant. 60 is the value from the original paper and there is
# nothing in this data that justifies tuning it.
RRF_K = 60

LANE_WEIGHTS = {
    # An exact identifier match is the strongest evidence there is: the user
    # named the thing. Embeddings are genuinely bad at identifiers.
    "identifier": 3.0,
    "structured": 2.0,
    "vector": 1.0,
    # Graph results are reached, not matched, so they rank below direct hits
    # while still surfacing what pure similarity would have buried.
    "graph": 1.5,
}

# ARNs and AWS id shapes. Anchored on service prefixes so ordinary prose never
# matches -- "i-" alone would fire on any hyphenated word.
IDENTIFIER = re.compile(
    r"arn:aws[^\s\"',]+"
    r"|\b(?:i|vol|sg|subnet|vpc|eipalloc|eni|ami|snap|igw|rtb|acl)-[0-9a-f]{8,17}\b",
    re.IGNORECASE,
)
REGION = re.compile(
    r"\b(?:us|eu|ap|ca|sa|me|af)-(?:east|west|north|south|central|northeast|southeast)"
    r"-\d\b",
    re.IGNORECASE,
)
SERVICES = (
    "ec2", "s3", "lambda", "iam", "rds", "dynamodb", "sqs", "sns", "vpc", "ecs",
    "cloudfront", "route53", "logs", "elasticloadbalancing", "lightsail", "states",
    "events",
)
# Waste vocabulary. These map to structured predicates, not to embeddings,
# because "unattached" is a fact in a config blob, not a mood.
WASTE_TERMS = ("unattached", "orphan", "abandoned", "unused", "idle", "detached",
               "untagged", "never invoked", "no retention", "waste")


@dataclass
class Hit:
    kind: str                      # resource | memory
    identifier: str                # arn or memory_id
    title: str
    detail: str
    lanes: set[str] = field(default_factory=set)
    score: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    lanes: list[str]
    identifiers: list[str]
    regions: list[str]
    services: list[str]
    waste: bool

    @property
    def why(self) -> str:
        bits = []
        if self.identifiers:
            bits.append(f"identifiers={self.identifiers}")
        if self.regions:
            bits.append(f"regions={self.regions}")
        if self.services:
            bits.append(f"services={self.services}")
        if self.waste:
            bits.append("waste-vocabulary")
        return f"{'+'.join(self.lanes)}" + (f" ({', '.join(bits)})" if bits else "")


def route(question: str) -> Plan:
    """Decide which lanes to run, from evidence in the question itself."""
    lowered = question.lower()
    identifiers = IDENTIFIER.findall(question)
    regions = list(dict.fromkeys(REGION.findall(question)))
    services = [s for s in SERVICES if re.search(rf"\b{s}\b", lowered)]
    waste = any(term in lowered for term in WASTE_TERMS)

    lanes: list[str] = []
    if identifiers:
        lanes.append("identifier")
    if regions or services or waste:
        lanes.append("structured")
    # The vector lane always runs. It is one embedding call, it is the only lane
    # that can answer a question phrased in words the schema does not contain,
    # and omitting it to save a cent is how a retrieval system gets brittle.
    lanes.append("vector")
    if identifiers or regions or services:
        lanes.append("graph")
    return Plan(lanes, identifiers, regions, services, waste)


def _identifier_lane(account_id: str, plan: Plan, limit: int) -> list[Hit]:
    """Exact and substring identifier matching. No embeddings anywhere near it."""
    if not plan.identifiers:
        return []
    hits: list[Hit] = []
    with pool().connection() as conn:
        for token in plan.identifiers[:5]:
            for row in conn.execute(
                "SELECT arn, service, resource_type, name, region, config"
                " FROM resources WHERE account_id = %s AND arn LIKE %s LIMIT %s",
                (account_id, f"%{token}%", limit),
            ):
                hits.append(
                    Hit("resource", row[0], row[3] or row[0], f"{row[1]}/{row[2]} in {row[4]}",
                        {"identifier"}, payload={"config": row[5], "region": row[4],
                                                 "service": row[1]})
                )
            # Memories are keyed by resource, so an identifier finds them too.
            for row in conn.execute(
                "SELECT memory_id, topic, body, origin, resource_key FROM memories"
                " WHERE account_id = %s AND retired_at IS NULL"
                "   AND (resource_key LIKE %s OR body LIKE %s) LIMIT %s",
                (account_id, f"%{token}%", f"%{token}%", limit),
            ):
                hits.append(
                    Hit("memory", str(row[0]), row[1], row[2], {"identifier"},
                        payload={"origin": row[3], "resource_key": row[4]})
                )
    return hits


def _structured_lane(account_id: str, plan: Plan, limit: int) -> list[Hit]:
    """Relational filters: region, service, and the waste predicates."""
    clauses: list[str] = ["account_id = %s"]
    params: list[Any] = [account_id]

    if plan.regions:
        # Global resources -- S3, IAM, CloudFront, Route53 -- carry region
        # 'global', but a user asking about "us-east-1" means them too. Filtering
        # them out is why "untagged s3 buckets in us-east-1" returned nothing.
        clauses.append("(region = ANY(%s) OR region = 'global')")
        params.append(plan.regions)
    if plan.services:
        clauses.append("service = ANY(%s)")
        params.append(plan.services)

    # AWS creates these itself. They are not waste, nobody abandoned them, and
    # letting them rank crowds out the findings that matter.
    aws_managed = (
        " AND arn NOT LIKE '%%:role/aws-service-role/%%'"
        " AND NOT (resource_type = 'security-group' AND name = 'default')"
        " AND NOT (resource_type = 'vpc' AND (config->>'IsDefault')::bool = true)"
    )

    # Waste has degrees. An unattached Elastic IP bills hourly and is a finding;
    # an untagged resource is a hygiene note. Ranking them equally buries the
    # first under the second, which is exactly what a naive OR does.
    waste_rank = """
        CASE
          WHEN resource_type = 'address' AND config->>'AssociationId' IS NULL THEN 1
          WHEN resource_type = 'volume'  AND config->'AttachedTo' = '[]'::jsonb THEN 1
          WHEN resource_type = 'instance' AND config->>'State' = 'stopped' THEN 2
          WHEN resource_type = 'log-group' AND config->>'retentionInDays' IS NULL THEN 3
          WHEN tags = '{}'::jsonb THEN 4
          ELSE 5
        END
    """
    if plan.waste:
        clauses.append(f"({waste_rank}) < 5")

    order = f"({waste_rank}) ASC, first_seen ASC" if plan.waste else "first_seen ASC"
    sql = (
        "SELECT arn, service, resource_type, name, region, config, first_seen,"
        f" ({waste_rank}) AS waste_rank"
        f" FROM resources WHERE {' AND '.join(clauses)}{aws_managed if plan.waste else ''}"
        f" ORDER BY {order} LIMIT %s"
    )
    params.append(limit)
    with pool().connection() as conn:
        return [
            Hit("resource", r[0], r[3] or r[0], f"{r[1]}/{r[2]} in {r[4]}", {"structured"},
                payload={"config": r[5], "region": r[4], "service": r[1],
                         "first_seen": r[6], "waste_rank": r[7]})
            for r in conn.execute(sql, params)
        ]


def _vector_lane(account_id: str, question: str, limit: int) -> list[Hit]:
    """Cosine similarity over memory bodies."""
    vector = to_vector(embed(question, account_id))
    with pool().connection() as conn:
        return [
            Hit("memory", str(r[0]), r[1], r[2], {"vector"},
                payload={"origin": r[3], "distance": float(r[4]),
                         "verified_at": r[5], "resource_key": r[6]})
            for r in conn.execute(
                "SELECT memory_id, topic, body, origin, embedding <=> %s AS d,"
                " verified_at, resource_key FROM memories"
                " WHERE account_id = %s AND retired_at IS NULL"
                " ORDER BY d LIMIT %s",
                (vector, account_id, limit),
            )
        ]


def _graph_lane(account_id: str, seeds: list[Hit], limit: int) -> list[Hit]:
    """One hop out from what the other lanes already found.

    Depth one on purpose. Deeper walks from an unranked seed set produce
    plausible-looking noise faster than they produce answers; blast_radius
    exists for when the user actually asked a depth question.
    """
    hits: list[Hit] = []
    seen: set[str] = set()
    for seed in seeds[:5]:
        if seed.kind != "resource":
            continue
        for edge in neighbours(account_id, seed.identifier)[:limit]:
            if edge["arn"] in seen:
                continue
            seen.add(edge["arn"])
            hits.append(
                Hit("resource", edge["arn"], edge["arn"].rsplit("/", 1)[-1],
                    f"{edge['direction']} {edge['edge_type']} {seed.title}",
                    {"graph"}, payload={"via": seed.identifier,
                                        "edge_type": edge["edge_type"],
                                        "edge_source": edge["source"]})
            )
    return hits


def _fuse(lane_results: dict[str, list[Hit]], limit: int) -> list[Hit]:
    """Reciprocal Rank Fusion across lanes, then weight, recency, provenance."""
    merged: dict[str, Hit] = {}

    for lane, hits in lane_results.items():
        weight = LANE_WEIGHTS.get(lane, 1.0)
        for rank, hit in enumerate(hits, start=1):
            key = f"{hit.kind}:{hit.identifier}"
            contribution = weight / (RRF_K + rank)
            existing = merged.get(key)
            if existing is None:
                hit.score = contribution
                hit.lanes = {lane}
                merged[key] = hit
            else:
                # Appearing in several lanes is itself evidence; the scores add.
                existing.score += contribution
                existing.lanes.add(lane)
                existing.payload.update(hit.payload)

    for hit in merged.values():
        # Human-supplied memory outranks a machine's guess about the same thing.
        # Small on purpose: provenance breaks ties, it does not rewrite ranking.
        if hit.payload.get("origin") == "human":
            hit.score *= 1.15
        # A memory nobody has re-verified is worth less than one just checked.
        if hit.kind == "memory" and hit.payload.get("verified_at") is None:
            hit.score *= 0.9

    return sorted(merged.values(), key=lambda h: h.score, reverse=True)[:limit]


def search(account_id: str, question: str, limit: int = 10) -> tuple[list[Hit], Plan]:
    """Route, run the applicable lanes, fuse, and return ranked hits."""
    plan = route(question)
    per_lane = max(limit, 10)
    results: dict[str, list[Hit]] = {}

    if "identifier" in plan.lanes:
        results["identifier"] = _identifier_lane(account_id, plan, per_lane)
    if "structured" in plan.lanes:
        results["structured"] = _structured_lane(account_id, plan, per_lane)
    if "vector" in plan.lanes:
        results["vector"] = _vector_lane(account_id, question, per_lane)
    if "graph" in plan.lanes:
        seeds = (results.get("identifier") or []) + (results.get("structured") or [])
        results["graph"] = _graph_lane(account_id, seeds, 5)

    fused = _fuse(results, limit)
    log.info("retrieval: %s -> %d hits", plan.why, len(fused))
    return fused, plan
