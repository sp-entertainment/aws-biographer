"""Backfill CloudTrail's default event history into the change log.

This is the free past. Every AWS account records roughly ninety days of
management events with actor identity attached, with nothing enabled and
nothing to pay for -- and then AWS deletes it. That deletion is the entire
argument for the memory layer, and this module is what rescues the window
that still exists the first time the agent ever runs.

Invariant 2 is why this reads Event History rather than a CloudTrail trail: a
trail is something the user would have to turn on.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.types.json import Jsonb

from ..aws import client
from ..db import pool

log = logging.getLogger(__name__)

# AWS retains roughly 90 days of management events. Asking for more is harmless
# but pointless; asking for less throws away history we can never get back.
RETENTION_DAYS = 90

# lookup_events is throttled hard (single-digit TPS). A scan that hammers it
# gets backed off anyway, so pace deliberately rather than fighting the limiter.
PAGE_PAUSE_SECONDS = 0.35

# Prefixes that tell us what a write actually did. Anything unmatched is still
# recorded -- it is a real change, we just cannot name its shape.
_CREATED = ("Create", "Run", "Allocate", "Launch", "Register", "Provision", "Import")
_DELETED = ("Delete", "Terminate", "Release", "Deregister", "Remove", "Revoke")


def classify(event_name: str) -> str:
    if event_name.startswith(_CREATED):
        return "created"
    if event_name.startswith(_DELETED):
        return "deleted"
    return "modified"


@dataclass
class BackfillResult:
    account_id: str
    regions: list[str]
    inserted: int = 0
    scanned: int = 0
    oldest: dt.datetime | None = None
    unresolved: int = 0


def _actor(detail: dict[str, Any]) -> str | None:
    """Pull a human-meaningful actor out of a CloudTrail record.

    `Username` is absent for assumed roles and service principals, which are
    exactly the cases where "who did this?" is most interesting, so fall back
    through the userIdentity block rather than recording nothing.
    """
    identity = detail.get("userIdentity", {})
    session_issuer = identity.get("sessionContext", {}).get("sessionIssuer", {})
    return (
        identity.get("userName")
        or session_issuer.get("userName")
        or identity.get("arn")
        or identity.get("invokedBy")
        or identity.get("type")
    )


def _arn_index(account_id: str) -> dict[str, str]:
    """Map bare resource identifiers to the ARNs already in the cache.

    CloudTrail names resources inconsistently -- sometimes a full ARN, often
    just `i-0abc123` or a bucket name. Without this, history could not be joined
    to inventory, and "when did this instance change?" would not be answerable
    for the majority of events.
    """
    index: dict[str, str] = {}
    with pool().connection() as conn:
        for (arn,) in conn.execute(
            "SELECT arn FROM resources WHERE account_id = %s", (account_id,)
        ):
            index[arn] = arn
            tail = arn.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
            if tail:
                index.setdefault(tail, arn)
    return index


# AWS identifier shapes that turn up in request parameters and response
# elements. Anchored on the service prefix so ordinary words never match.
_ID_PATTERN = re.compile(
    r"\barn:aws[^\s\"',]+"
    r"|\b(?:i|vol|sg|subnet|vpc|eipalloc|eni|ami|snap|igw|rtb|acl|pl)-[0-9a-f]{8,17}\b"
)


def _resolve(event: dict, raw_detail: str, index: dict[str, str]) -> str | None:
    """Best ARN for an event.

    CloudTrail's `Resources` array is authoritative but frequently empty --
    plenty of write events name the thing they touched only inside
    `requestParameters` or `responseElements`. Falling back to identifier
    extraction is the difference between a change log that can answer "when did
    this instance change?" and one that mostly cannot.
    """
    for item in event.get("Resources", []) or []:
        name = item.get("ResourceName") or ""
        if name.startswith("arn:"):
            return name
        hit = index.get(name)
        if hit:
            return hit

    # Fall back to whatever identifiers appear anywhere in the raw record, and
    # keep only those we already know about -- an unknown id is not evidence.
    for candidate in _ID_PATTERN.findall(raw_detail):
        hit = index.get(candidate)
        if hit:
            return hit
    return None


def backfill(
    session: boto3.Session, account_id: str, regions: list[str], days: int = RETENTION_DAYS
) -> BackfillResult:
    """Pull write events for each region into the change log.

    Read-only events are excluded deliberately. They are the overwhelming
    majority of CloudTrail volume and none of them changed anything, so storing
    them would bury the actual history under console polling noise.

    Re-running is safe: `changes.event_id` is unique per account, so a second
    backfill inserts nothing rather than duplicating the past.
    """
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    result = BackfillResult(account_id=account_id, regions=regions)
    index = _arn_index(account_id)

    for region in regions:
        trail = client(session, "cloudtrail", region)
        rows: list[tuple[Any, ...]] = []
        try:
            pages = trail.get_paginator("lookup_events").paginate(
                LookupAttributes=[
                    {"AttributeKey": "ReadOnly", "AttributeValue": "false"}
                ],
                StartTime=start,
            )
            for page in pages:
                for event in page.get("Events", []):
                    result.scanned += 1
                    raw_detail = event.get("CloudTrailEvent", "{}")
                    try:
                        detail = json.loads(raw_detail)
                    except json.JSONDecodeError:
                        detail = {}

                    when = event["EventTime"]
                    if result.oldest is None or when < result.oldest:
                        result.oldest = when

                    arn = _resolve(event, raw_detail, index)
                    if arn is None:
                        result.unresolved += 1

                    name = event.get("EventName", "")
                    rows.append(
                        (
                            account_id,
                            arn,
                            region,
                            classify(name),
                            event.get("Username") or _actor(detail),
                            "cloudtrail",
                            name,
                            when,
                            event.get("EventId"),
                            Jsonb(
                                {
                                    "eventSource": event.get("EventSource"),
                                    "resources": event.get("Resources", []),
                                    # Kept because plenty of services name their
                                    # target only here -- Lightsail's
                                    # DeleteInstance carries an empty Resources
                                    # array and an instanceName in the request.
                                    # Without it, the record of a deletion loses
                                    # the identity of what was deleted.
                                    "requestParameters": detail.get("requestParameters"),
                                    "sourceIPAddress": detail.get("sourceIPAddress"),
                                    "userAgent": detail.get("userAgent"),
                                    "errorCode": detail.get("errorCode"),
                                    "userIdentityType": detail.get(
                                        "userIdentity", {}
                                    ).get("type"),
                                }
                            ),
                        )
                    )
                time.sleep(PAGE_PAUSE_SECONDS)
        except (ClientError, BotoCoreError) as exc:
            log.warning("cloudtrail backfill failed in %s: %s", region, exc)
            continue

        if rows:
            with pool().connection() as conn, conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO changes (account_id, arn, region, change_type,"
                    " actor, source, event_name, event_time, event_id, raw)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    # The predicate must be repeated: changes_by_event is a
                    # partial unique index, and without it Postgres cannot match
                    # the ON CONFLICT target to any constraint.
                    " ON CONFLICT (account_id, event_id) WHERE event_id IS NOT NULL"
                    " DO NOTHING",
                    rows,
                )
                conn.commit()
            result.inserted += len(rows)
        log.info("%s: %d write events", region, len(rows))

    return result


if __name__ == "__main__":
    from ..aws import account_id_of, session_for
    from ..config import settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        sess = session_for()
        acct = account_id_of(sess)
        with pool().connection() as c:
            swept = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT region FROM resources"
                    " WHERE account_id = %s AND region != 'global'",
                    (acct,),
                )
            ] or [settings().aws_region]

        outcome = backfill(sess, acct, swept)
        print(f"\nregions   {', '.join(outcome.regions)}")
        print(f"scanned   {outcome.scanned} write events")
        print(f"stored    {outcome.inserted}")
        print(f"oldest    {outcome.oldest}")
        print(f"unresolved to a known ARN: {outcome.unresolved}")

        with pool().connection() as c:
            print("\ntop actors:")
            for actor, n in c.execute(
                "SELECT actor, count(*) FROM changes WHERE account_id = %s"
                " GROUP BY actor ORDER BY count(*) DESC LIMIT 5",
                (acct,),
            ):
                print(f"  {str(actor):<40} {n}")
    finally:
        pool().close()
