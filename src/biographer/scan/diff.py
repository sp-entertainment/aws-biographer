"""Scan-over-scan diffing into the change log.

Deltas only. Full state snapshots per scan were considered and rejected: they
grow without bound and bury the handful of real changes under a re-recording of
everything that did not move.

The diff runs *before* the cache is overwritten, because the previous scan's
state is the only thing that says what changed, and the upsert destroys it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

log = logging.getLogger(__name__)

# Config keys that move on their own without anything meaningful happening.
# Recording these would produce a change log that is mostly noise, and a noisy
# evidence trail is worse than a short one -- it trains the reader to ignore it.
VOLATILE_KEYS = frozenset(
    {
        "LastModified",
        "lastModified",
        "storedBytes",
        "ItemCount",
        "TableSizeBytes",
        "ApproximateNumberOfMessages",
        "runningTasksCount",
        "pendingTasksCount",
        "SubscriptionsConfirmed",
        "SubscriptionsPending",
        "metricFilterCount",
        "PasswordLastUsed",
    }
)


@dataclass
class Delta:
    arn: str
    region: str
    change_type: str
    field_name: str | None = None
    old: Any = None
    new: Any = None


@dataclass
class DiffResult:
    created: list[Delta] = field(default_factory=list)
    deleted: list[Delta] = field(default_factory=list)
    modified: list[Delta] = field(default_factory=list)

    @property
    def all(self) -> list[Delta]:
        return self.created + self.deleted + self.modified

    def __len__(self) -> int:
        return len(self.all)


def _changed_fields(old: dict, new: dict) -> list[tuple[str, Any, Any]]:
    """Top-level config keys whose value moved, volatile ones excluded."""
    out: list[tuple[str, Any, Any]] = []
    for key in sorted(set(old) | set(new)):
        if key in VOLATILE_KEYS:
            continue
        before, after = old.get(key), new.get(key)
        if before != after:
            out.append((key, before, after))
    return out


def compute(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
    swept_regions: set[str],
) -> DiffResult:
    """Compare two inventories.

    `previous` and `current` map ARN to a dict with `region`, `name`, `tags`,
    `config`.

    Disappearance is only meaningful inside a region this scan actually swept. A
    region we skipped is not a region whose resources vanished, and treating it
    as one would fabricate deletions on every partial scan -- the fastest way to
    make a change log untrustworthy.
    """
    result = DiffResult()

    for arn, now in current.items():
        before = previous.get(arn)
        if before is None:
            result.created.append(
                Delta(arn=arn, region=now["region"], change_type="created", new=now.get("name"))
            )
            continue

        for key, old_value, new_value in _changed_fields(
            before.get("config") or {}, now.get("config") or {}
        ):
            result.modified.append(
                Delta(arn, now["region"], "modified", key, old_value, new_value)
            )

        if (before.get("tags") or {}) != (now.get("tags") or {}):
            result.modified.append(
                Delta(arn, now["region"], "modified", "tags",
                      before.get("tags"), now.get("tags"))
            )

        if before.get("name") != now.get("name"):
            result.modified.append(
                Delta(arn, now["region"], "modified", "name",
                      before.get("name"), now.get("name"))
            )

    for arn, before in previous.items():
        if arn in current:
            continue
        if before["region"] not in swept_regions:
            continue
        result.deleted.append(
            Delta(arn=arn, region=before["region"], change_type="deleted",
                  old=before.get("name"))
        )

    return result


def to_rows(account_id: str, scan_id: str, diff: DiffResult) -> list[tuple[Any, ...]]:
    """Change-log rows for a diff. Source is `scan`, not `cloudtrail`.

    No actor: a scan observes that something moved, it does not witness who
    moved it. Attributing a scan-detected change to a person would be a lie the
    evidence cannot support -- CloudTrail is what carries identity.
    """
    return [
        (
            account_id,
            d.arn,
            d.region,
            d.change_type,
            d.field_name,
            Jsonb(d.old) if d.old is not None else None,
            Jsonb(d.new) if d.new is not None else None,
            None,
            "scan",
            # Unique per delta, not per scan: changes.event_id is uniquely
            # indexed, so one id shared across a scan's deltas would collide and
            # drop every change after the first. Including the field makes
            # re-persisting a scan idempotent rather than duplicative.
            f"scan:{scan_id}:{d.change_type}:{d.arn}:{d.field_name or ''}",
        )
        for d in diff.all
    ]
