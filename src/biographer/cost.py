"""Cost attribution against inventory.

The §2 verification established that resource-level granularity is opt-in only
on this account -- Cost Explorer answers with the exact wording "Resource-level
data granularity is an opt-in only feature." Enabling it would breach invariant
2 and it costs extra, so this module does what the design summary called for
instead: take service, region and tag level spend, attribute it against the
inventory the scan already built, and reason about the split.

"You have three t4g.nano instances in us-east-1 and nine dollars of EC2 spend
there" is a useful answer without per-ARN billing. Claiming a precise per-ARN
figure that Cost Explorer did not supply would not be.

Cost Explorer bills roughly a cent per request, so this asks for a whole period
in one call rather than iterating.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from .aws import client, session_for
from .db import pool

log = logging.getLogger(__name__)

CE_REGION = "us-east-1"

# Cost Explorer's service names do not match the service token used in ARNs, and
# nothing in either API bridges them. Only the mappings this project's collectors
# can actually produce are listed; an unmapped service is reported as
# unattributed rather than guessed at.
SERVICE_TO_ARN_TOKEN = {
    "Amazon Elastic Compute Cloud - Compute": "ec2",
    "EC2 - Other": "ec2",
    "Amazon Simple Storage Service": "s3",
    "AWS Lambda": "lambda",
    "Amazon Relational Database Service": "rds",
    "Amazon DynamoDB": "dynamodb",
    "Amazon Simple Queue Service": "sqs",
    "Amazon Simple Notification Service": "sns",
    "AmazonCloudWatch": "logs",
    "Amazon CloudFront": "cloudfront",
    "Amazon Route 53": "route53",
    "Amazon Lightsail": "lightsail",
    "Amazon Elastic Container Service": "ecs",
}


@dataclass
class ServiceCost:
    service_label: str
    arn_token: str | None
    amount_usd: float
    region: str | None = None
    resources: list[dict[str, Any]] = field(default_factory=list)

    @property
    def per_resource_hint(self) -> float | None:
        """Even spread across matching resources. A hint, never a figure.

        Presented as an average and named as one. Cost Explorer did not supply
        per-resource cost and this arithmetic does not conjure it -- a t4g.nano
        and an m5.24xlarge in the same service line do not cost the same, and
        printing this as if it were billing data would be a fabrication.
        """
        if not self.resources:
            return None
        return self.amount_usd / len(self.resources)


def spend_by_service(days: int = 30) -> tuple[list[ServiceCost], str | None]:
    """Service-level spend for the period. Returns (costs, error)."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    try:
        resp = client(session_for(), "ce", CE_REGION).get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ],
        )
    except ClientError as exc:
        # Invariant 2: Cost Explorer may simply not be enabled, and that must
        # degrade the cost answer rather than break the product.
        return [], f"{exc.response['Error']['Code']}: cost data unavailable"

    totals: dict[tuple[str, str], float] = {}
    for window in resp.get("ResultsByTime", []):
        for group in window.get("Groups", []):
            service, region = group["Keys"][0], group["Keys"][1]
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if amount > 0:
                key = (service, region)
                totals[key] = totals.get(key, 0.0) + amount

    return (
        [
            ServiceCost(service_label=service,
                        arn_token=SERVICE_TO_ARN_TOKEN.get(service),
                        amount_usd=amount, region=region)
            for (service, region), amount in sorted(
                totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
        None,
    )


def attribute(account_id: str, days: int = 30) -> dict[str, Any]:
    """Join spend to inventory. States plainly what it cannot know."""
    costs, error = spend_by_service(days)
    if error:
        return {"error": error, "attributed": [], "total_usd": 0.0}

    with pool().connection() as conn:
        for cost in costs:
            if not cost.arn_token:
                continue
            cost.resources = [
                {"arn": r[0], "name": r[1], "type": r[2]}
                for r in conn.execute(
                    "SELECT arn, name, resource_type FROM resources"
                    " WHERE account_id = %s AND service = %s"
                    "   AND (region = %s OR region = 'global')",
                    (account_id, cost.arn_token, cost.region),
                )
            ]

    total = sum(c.amount_usd for c in costs)
    unmapped = [c.service_label for c in costs if not c.arn_token]

    return {
        "period_days": days,
        "total_usd": round(total, 4),
        "granularity": "service and region",
        # Stated in the payload, not just in a docstring, so the model relays it
        # rather than implying precision the data does not have.
        "caveat": (
            "Resource-level granularity is an opt-in Cost Explorer feature and is "
            "not enabled on this account, so spend is attributed at service and "
            "region level. Per-resource figures below are averages across matching "
            "resources, not billing data."
        ),
        "attributed": [
            {
                "service": c.service_label,
                "region": c.region,
                "amount_usd": round(c.amount_usd, 4),
                "matching_resources": len(c.resources),
                "average_per_resource_usd": (
                    round(c.per_resource_hint, 4) if c.per_resource_hint else None
                ),
                "resources": c.resources[:10],
            }
            for c in costs
        ],
        "unmapped_services": unmapped,
    }
