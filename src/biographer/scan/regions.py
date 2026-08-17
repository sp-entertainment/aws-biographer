"""Which regions are worth a full sweep.

Seventeen default regions times roughly forty service APIs is thousands of
calls. Design summary section 4 says don't brute-force it: use cost data to find
regions with spend, probe candidates cheaply in parallel, then sweep only the
regions showing signs of life, plus one pass for global services.

It also names two holes, and they are the reason this module uses three signals
instead of one:

  - Free resources never appear in cost data. An unattached security group, an
    empty log group, a hand-built VPC all cost nothing and would be invisible to
    a cost-only gate.
  - The Tagging API only sees taggable resources, and the resources this product
    most wants to find are precisely the ones nobody tagged.

So: cost, tags, and a small canary probe. A region lights up if ANY of the three
sees something. Union, never intersection -- a false positive costs one wasted
region sweep, a false negative silently loses resources and reports a plausible
count that is wrong.
"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import boto3
from botocore.exceptions import ClientError

from ..aws import client, enabled_regions

log = logging.getLogger(__name__)

# Cost Explorer lives in us-east-1 regardless of where you're scanning.
CE_REGION = "us-east-1"


@dataclass
class RegionSignals:
    region: str
    has_cost: bool = False
    tagged_count: int = 0
    canary_hits: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def alive(self) -> bool:
        return self.has_cost or self.tagged_count > 0 or bool(self.canary_hits)

    @property
    def why(self) -> str:
        reasons = []
        if self.has_cost:
            reasons.append("spend")
        if self.tagged_count:
            reasons.append(f"{self.tagged_count} tagged")
        if self.canary_hits:
            reasons.append("+".join(self.canary_hits))
        return ", ".join(reasons) or "quiet"


def regions_with_spend(session: boto3.Session, days: int = 60) -> set[str]:
    """Regions Cost Explorer has billed anything to.

    Costs about a cent per request, which is why this is one call for all
    regions rather than one per region. Returns an empty set on failure -- Cost
    Explorer may not be enabled, and invariant 2 forbids requiring that it is.
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    try:
        resp = client(session, "ce", CE_REGION).get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}],
        )
    except ClientError as exc:
        log.warning("cost signal unavailable, falling back to probes: %s", exc)
        return set()

    found: set[str] = set()
    for window in resp.get("ResultsByTime", []):
        for group in window.get("Groups", []):
            name = group["Keys"][0]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            # "NoRegion" is Cost Explorer's bucket for global and unattributed
            # charges; it is not a region and must not become one.
            if name != "NoRegion" and amount > 0:
                found.add(name)
    return found


def _probe(session: boto3.Session, region: str, billed: set[str]) -> RegionSignals:
    """One region's three signals. Never raises."""
    signals = RegionSignals(region=region, has_cost=region in billed)

    try:
        tagging = client(session, "resourcegroupstaggingapi", region)
        page = tagging.get_resources(ResourcesPerPage=50)
        signals.tagged_count = len(page.get("ResourceTagMappingList", []))
    except ClientError as exc:
        signals.errors.append(f"tagging: {exc.response['Error']['Code']}")

    # Canaries: cheap, single-call, and chosen to catch exactly what the other
    # two signals miss -- free and untagged. A non-default security group or a
    # non-default VPC means somebody built something here by hand.
    try:
        ec2 = client(session, "ec2", region)
        groups = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["*"]}], MaxResults=100
        ).get("SecurityGroups", [])
        if any(g.get("GroupName") != "default" for g in groups):
            signals.canary_hits.append("sg")

        vpcs = ec2.describe_vpcs().get("Vpcs", [])
        if any(not v.get("IsDefault") for v in vpcs):
            signals.canary_hits.append("vpc")
    except ClientError as exc:
        signals.errors.append(f"ec2: {exc.response['Error']['Code']}")

    try:
        logs = client(session, "logs", region)
        if logs.describe_log_groups(limit=1).get("logGroups"):
            signals.canary_hits.append("logs")
    except ClientError as exc:
        signals.errors.append(f"logs: {exc.response['Error']['Code']}")

    return signals


def discover(
    session: boto3.Session, home_region: str, max_workers: int = 12
) -> tuple[list[str], list[RegionSignals]]:
    """Return (regions to sweep, the evidence for each region).

    The home region is always swept whether or not it shows signs of life. It is
    where a mostly-empty account puts its first resource, and skipping it to
    save one sweep would be a strange way to save nothing.
    """
    billed = regions_with_spend(session)
    candidates = enabled_regions(session)
    log.info("probing %d enabled regions (%d with spend)", len(candidates), len(billed))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        signals = list(pool.map(lambda r: _probe(session, r, billed), candidates))

    live = sorted({s.region for s in signals if s.alive} | {home_region})
    for s in signals:
        if s.alive:
            log.info("  %-15s %s", s.region, s.why)
        for err in s.errors:
            log.debug("  %-15s probe error: %s", s.region, err)

    log.info("sweeping %d of %d regions: %s", len(live), len(candidates), ", ".join(live))
    return live, signals
