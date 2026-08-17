"""Turn scan observations into memories that carry their own verification.

This is the machine-side memory layer design summary §9 says no framework will
build for you. Frameworks extract memory from conversation transcripts; scan
deltas and structural findings arrive from a scheduled job with no conversation
anywhere near them.

The durability test from §5 decides what belongs here: not "this Elastic IP is
unattached", which one API call recomputes, but "this Elastic IP has been
unattached since 3 March", which only time reveals. So every finding below is
phrased with its duration, and duration comes from `first_seen` -- the column
the scan deliberately never overwrites.

Each finding ships with the claim that would falsify it. That pairing is the
whole point: a finding without a re-check is a snapshot, and snapshots are what
every comparable tool already produces.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

from ..db import pool
from . import store
from .verify import Kind

log = logging.getLogger(__name__)

# Below this, "it has been like this for a while" is not yet true, and a memory
# that says otherwise is just a snapshot wearing a timestamp.
#
# Configurable because the right value depends on the account's own age. An
# account with months of history wants hours or days here; a demo account seeded
# this morning would produce no findings at all under that threshold, which
# looks like a broken feature rather than a correctly conservative one.
MIN_AGE = dt.timedelta(minutes=int(os.environ.get("FINDING_MIN_AGE_MINUTES", "30")))


def _age_phrase(first_seen: dt.datetime) -> str:
    now = dt.datetime.now(first_seen.tzinfo) if first_seen.tzinfo else dt.datetime.now()
    days = (now - first_seen).days
    if days >= 1:
        # "%-d" is a glibc extension and raises on Windows; "%d %B" is portable
        # and the leading zero is not worth a platform-specific branch.
        return f"since {first_seen:%d %B}"
    hours = int((now - first_seen).total_seconds() // 3600)
    if hours:
        return f"for {hours} hours"
    minutes = int((now - first_seen).total_seconds() // 60)
    return f"for {minutes} minutes" if minutes else "since it was first seen"


def _suppressed(account_id: str, arn: str, finding_type: str) -> bool:
    """Has a human told us to stop reporting this?

    Checked before writing, not after. A suppression that only filters the
    display still lets the memory be written, re-verified, and re-surfaced
    everywhere else -- which is not what the user asked for.
    """
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM suppressions WHERE account_id = %s"
            "   AND finding_type = %s AND (arn = %s OR arn IS NULL)",
            (account_id, finding_type, arn),
        ).fetchone()
    return bool(row[0])


def _candidates(account_id: str) -> list[dict[str, Any]]:
    """Structural findings worth remembering, each with its falsifying claim."""
    cutoff = dt.datetime.now(dt.timezone.utc) - MIN_AGE
    out: list[dict[str, Any]] = []

    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT arn, region, service, resource_type, name, tags, config, first_seen"
            " FROM resources WHERE account_id = %s AND first_seen <= %s"
            "   AND arn NOT LIKE '%%:role/aws-service-role/%%'",
            (account_id, cutoff),
        ).fetchall()

    for arn, region, _service, kind, name, tags, config, first_seen in rows:
        config = config or {}
        label = name or arn.rsplit("/", 1)[-1]
        age = _age_phrase(first_seen)

        if kind == "address" and config.get("AssociationId") is None:
            out.append({
                "finding_type": "unattached-eip",
                "arn": arn,
                "topic": f"unattached-eip-{label}",
                "body": (f"Elastic IP {label} ({arn}) has been allocated but attached "
                         f"to nothing {age}. Unattached Elastic IPs bill hourly."),
                "claim": {"kind": Kind.CONFIG_ABSENT.value, "arn": arn,
                          "region": region, "path": "AssociationId"},
            })

        elif kind == "volume" and not (config.get("AttachedTo") or []):
            out.append({
                "finding_type": "orphaned-volume",
                "arn": arn,
                "topic": f"orphaned-volume-{label}",
                "body": (f"EBS volume {label} ({arn}) has been attached to nothing "
                         f"{age}. It is {config.get('Size', '?')} GiB of "
                         f"{config.get('VolumeType', 'unknown')} storage still billing."),
                "claim": {"kind": Kind.CONFIG_ABSENT.value, "arn": arn,
                          "region": region, "path": "AttachedTo"},
            })

        elif kind == "log-group" and config.get("retentionInDays") is None:
            out.append({
                "finding_type": "no-log-retention",
                "arn": arn,
                "topic": f"no-retention-{label}",
                "body": (f"Log group {label} ({arn}) has had no retention policy "
                         f"{age}, so its logs are kept forever and grow without bound."),
                "claim": {"kind": Kind.CONFIG_ABSENT.value, "arn": arn,
                          "region": region, "path": "retentionInDays"},
            })

        elif kind == "security-group" and name != "default":
            open_rules = [
                p for p in config.get("IpPermissions", [])
                if any(r.get("CidrIp") == "0.0.0.0/0" for r in p.get("IpRanges", []))
            ]
            if open_rules:
                ports = ", ".join(str(p.get("FromPort", "all")) for p in open_rules)
                out.append({
                    "finding_type": "world-open-sg",
                    "arn": arn,
                    "topic": f"world-open-sg-{label}",
                    "body": (f"Security group {label} ({arn}) has allowed inbound "
                             f"traffic from 0.0.0.0/0 on port(s) {ports} {age}."),
                    "claim": {"kind": Kind.CONFIG_EQUALS.value, "arn": arn,
                              "region": region, "path": "IpPermissions",
                              "expect": config.get("IpPermissions")},
                })

        elif not tags and kind in ("instance", "bucket"):
            out.append({
                "finding_type": "untagged",
                "arn": arn,
                "topic": f"untagged-{label}",
                "body": (f"{kind.title()} {label} ({arn}) has carried no tags at all "
                         f"{age}, so it cannot be attributed to an owner or a cost "
                         f"centre."),
                "claim": {"kind": Kind.UNTAGGED.value, "arn": arn, "region": region},
            })

    for candidate in out:
        # Single assignment point: the claim always agrees with the finding it
        # came from, which is what suppression matches on.
        candidate["claim"]["finding_type"] = candidate["finding_type"]
    return out


def record(account_id: str) -> dict[str, int]:
    """Write the current findings as memories. Idempotent by topic.

    These bypass the durability filter deliberately -- not because they are
    exempt from it, but because they already satisfy it by construction. Every
    one is phrased with a duration drawn from first_seen, and each carries a
    machine-checkable claim. Paying a model call to re-ask "is this durable?"
    about a fact built to be durable is pure cost.
    """
    written = skipped = suppressed = 0
    for candidate in _candidates(account_id):
        if _suppressed(account_id, candidate["arn"], candidate["finding_type"]):
            suppressed += 1
            continue
        memory = store.remember(
            account_id,
            candidate["topic"],
            candidate["body"],
            resource_key=candidate["arn"],
            origin=store.AGENT,
            claim=candidate["claim"],
            explicit=True,
        )
        if memory.dropped:
            skipped += 1
        else:
            written += 1
    return {"written": written, "skipped": skipped, "suppressed": suppressed}


def suppress(account_id: str, finding_type: str, arn: str | None = None,
             reason: str | None = None) -> None:
    """Stop reporting a finding, and retire any memory already made from it.

    Retiring the existing memory matters: without it the suppression only stops
    *future* writes and the finding keeps surfacing from what is already stored,
    which reads to the user as being ignored.
    """
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO suppressions (account_id, arn, finding_type, reason)"
            " VALUES (%s, %s, %s, %s)",
            (account_id, arn, finding_type, reason),
        )
        conn.execute(
            "UPDATE memories SET retired_at = now(),"
            " retire_reason = 'suppressed by a human'"
            " WHERE account_id = %s AND retired_at IS NULL"
            "   AND claim->>'finding_type' = %s"
            "   AND (%s::string IS NULL OR resource_key = %s::string)",
            (account_id, finding_type, arn, arn),
        )
        conn.commit()
