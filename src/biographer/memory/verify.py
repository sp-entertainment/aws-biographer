"""Memory verification: the feature the whole submission rests on.

Staleness is the unsolved problem in agent memory because facts about people
cannot be cheaply re-checked. In AWS they can. So every memory this system
stores carries a **claim**: a small declarative statement of what makes it true,
which can be re-evaluated on demand.

Three decisions shape this module.

**Claims are declarative data, never code and never a model call.** A claim is
one of a closed set of kinds with a resource and an expected value. An
executable check authored by a language model would be arbitrary code with a
database behind it, and no amount of sandboxing makes that a good trade for a
feature whose entire value is trustworthiness. A closed enum is auditable, can
only read, and is impossible to turn into an injection.

**Verification runs against the current-state cache, not against AWS.** The
scan immediately preceding it already made every API call; asking AWS again
would double the cost of the most expensive operation in the product to learn
what the cache already knows. This is what makes re-checking every memory on
every pass affordable, which is what makes verification continuous rather than
occasional.

**Absence of evidence is not evidence of absence.** A claim whose resource is
missing from a region this scan did not sweep, or whose shape cannot be
evaluated, comes back UNVERIFIABLE -- not false. Retiring on unverifiable would
mean one permission error or one partial scan wipes out the memory base, which
is a far worse failure than a stale memory and much harder to notice.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any

from ..db import pool

log = logging.getLogger(__name__)


class Kind(str, enum.Enum):
    """The closed set of things a memory may claim."""

    RESOURCE_EXISTS = "resource_exists"
    RESOURCE_ABSENT = "resource_absent"
    # A field in the resource's config equals a value. Covers "is unattached"
    # (AssociationId is null), "is stopped", "has no retention".
    CONFIG_EQUALS = "config_equals"
    CONFIG_ABSENT = "config_absent"
    # The resource carries no tags. The hygiene claim.
    UNTAGGED = "untagged"
    # An edge exists in the graph. Covers "this belongs to that feature".
    EDGE_EXISTS = "edge_exists"


class Verdict(str, enum.Enum):
    HOLDS = "holds"
    FALSE = "false"
    UNVERIFIABLE = "unverifiable"


@dataclass
class Check:
    verdict: Verdict
    detail: str


def _resource(account_id: str, arn: str) -> dict[str, Any] | None:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT arn, tags, config, region, last_seen FROM resources"
            " WHERE account_id = %s AND arn = %s",
            (account_id, arn),
        ).fetchone()
    if row is None:
        return None
    return {"arn": row[0], "tags": row[1] or {}, "config": row[2] or {},
            "region": row[3], "last_seen": row[4]}


def _region_was_swept(account_id: str, region: str | None) -> bool:
    """Did the most recent scan actually cover this region?

    This is the guard that separates "the resource is gone" from "we did not
    look". Without it a tiered scan that skipped a quiet region would retire
    every memory about that region.
    """
    if not region:
        return False
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT regions FROM scans WHERE account_id = %s AND finished_at IS NOT NULL"
            " ORDER BY started_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()
    if not row:
        return False
    swept = row[0] or []
    return region in swept or region == "global"


def evaluate(account_id: str, claim: dict[str, Any] | None) -> Check:
    """Re-check one claim against the current-state cache."""
    if not claim:
        # A memory with no claim is not wrong, it is merely unverifiable --
        # human annotations often have nothing checkable about them, and those
        # are the most valuable memories in the system.
        return Check(Verdict.UNVERIFIABLE, "no claim attached")

    try:
        kind = Kind(claim.get("kind", ""))
    except ValueError:
        return Check(Verdict.UNVERIFIABLE, f"unknown claim kind {claim.get('kind')!r}")

    arn = claim.get("arn")

    if kind is Kind.EDGE_EXISTS:
        with pool().connection() as conn:
            found = conn.execute(
                "SELECT count(*) FROM edges WHERE account_id = %s AND src_arn = %s"
                "   AND dst_arn = %s",
                (account_id, arn, claim.get("dst_arn")),
            ).fetchone()[0]
        return (Check(Verdict.HOLDS, "edge present") if found
                else Check(Verdict.FALSE, "edge no longer present"))

    if not arn:
        return Check(Verdict.UNVERIFIABLE, "claim names no resource")

    resource = _resource(account_id, arn)
    claim_region = claim.get("region")

    if resource is None:
        if kind is Kind.RESOURCE_ABSENT:
            return Check(Verdict.HOLDS, "resource still absent")
        if not _region_was_swept(account_id, claim_region):
            return Check(Verdict.UNVERIFIABLE,
                         f"region {claim_region or '?'} was not swept by the last scan")
        return Check(Verdict.FALSE, "resource no longer exists")

    if kind is Kind.RESOURCE_EXISTS:
        return Check(Verdict.HOLDS, "resource still exists")
    if kind is Kind.RESOURCE_ABSENT:
        return Check(Verdict.FALSE, "resource exists again")
    if kind is Kind.UNTAGGED:
        return (Check(Verdict.HOLDS, "still untagged") if not resource["tags"]
                else Check(Verdict.FALSE, f"now tagged: {sorted(resource['tags'])}"))

    path = claim.get("path")
    if not path:
        return Check(Verdict.UNVERIFIABLE, "claim names no config field")
    actual = resource["config"].get(path)

    if kind is Kind.CONFIG_ABSENT:
        return (Check(Verdict.HOLDS, f"{path} still absent")
                if actual in (None, [], {}, "")
                else Check(Verdict.FALSE, f"{path} is now {actual!r}"))

    expected = claim.get("expect")
    return (Check(Verdict.HOLDS, f"{path} is still {expected!r}")
            if actual == expected
            else Check(Verdict.FALSE, f"{path} changed from {expected!r} to {actual!r}"))


@dataclass
class VerificationRun:
    checked: int = 0
    refreshed: int = 0
    retired: int = 0
    unverifiable: int = 0
    retirements: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.retirements is None:
            self.retirements = []


def verify_all(account_id: str) -> VerificationRun:
    """Re-check every live memory. Refresh what holds, retire what is false."""
    run = VerificationRun()
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT memory_id, topic, body, claim, origin FROM memories"
            " WHERE account_id = %s AND retired_at IS NULL",
            (account_id,),
        ).fetchall()

    for memory_id, topic, body, claim, origin in rows:
        run.checked += 1
        check = evaluate(account_id, claim)

        if check.verdict is Verdict.HOLDS:
            run.refreshed += 1
            with pool().connection() as conn:
                conn.execute("UPDATE memories SET verified_at = now()"
                             " WHERE memory_id = %s", (memory_id,))
                conn.commit()

        elif check.verdict is Verdict.FALSE:
            run.retired += 1
            reason = check.detail
            with pool().connection() as conn:
                # Retire, never delete. A retired memory is still evidence of
                # what the agent believed and why it stopped believing it --
                # which is exactly what the self-reinforcing-error failure mode
                # needs in order to be escapable.
                conn.execute(
                    "UPDATE memories SET retired_at = now(), retire_reason = %s"
                    " WHERE memory_id = %s",
                    (reason, memory_id),
                )
                conn.commit()
            run.retirements.append({
                "memory_id": str(memory_id), "topic": topic,
                "body": body, "reason": reason, "origin": origin,
            })
            log.info("retired memory %s (%s): %s", topic, origin, reason)

        else:
            run.unverifiable += 1

    return run


def recent_retirements(account_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Retired memories, newest first. This is how retirement reaches the user."""
    with pool().connection() as conn:
        return [
            {"topic": r[0], "body": r[1], "reason": r[2], "origin": r[3],
             "retired_at": r[4], "human_text": r[5]}
            for r in conn.execute(
                "SELECT topic, body, retire_reason, origin, retired_at, human_text"
                " FROM memories WHERE account_id = %s AND retired_at IS NOT NULL"
                " ORDER BY retired_at DESC LIMIT %s",
                (account_id, limit),
            )
        ]
