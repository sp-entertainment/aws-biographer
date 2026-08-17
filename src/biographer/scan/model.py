"""The collector contract.

A collector turns one AWS service in one region into `Resource` records. It
never touches the database, never mutates AWS, and never raises for an ordinary
denial -- the runner treats a collector as best-effort, because invariant 2 says
this must work against an account with nothing enabled and invariant 3 says the
credentials may legitimately lack reach.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import boto3

# Regions are stored literally; global services use this sentinel so the column
# is never null and `WHERE region = ?` behaves the same for both.
GLOBAL = "global"


@dataclass(frozen=True, slots=True)
class Resource:
    """One AWS resource as the current-state cache stores it.

    `arn` is the identity. Invariant 4 requires every answer that references a
    resource to carry something the user can paste into the console, so a
    collector that cannot produce a real ARN must synthesise a stable, unique,
    console-recognisable identifier rather than omit one.
    """

    arn: str
    region: str
    service: str
    resource_type: str
    name: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.arn:
            raise ValueError(f"{self.service}/{self.resource_type} produced no identifier")


@dataclass(frozen=True, slots=True)
class CollectorSpec:
    service: str
    resource_type: str
    scope: str  # "regional" | "global"
    fn: Callable[[boto3.Session, str], Iterable[Resource]]

    @property
    def label(self) -> str:
        return f"{self.service}:{self.resource_type}"


REGISTRY: list[CollectorSpec] = []


def collector(
    service: str, resource_type: str, scope: str = "regional"
) -> Callable[[Callable[..., Iterable[Resource]]], Callable[..., Iterable[Resource]]]:
    """Register a collector.

    The decorated function takes `(session, region)` and yields `Resource`. For
    global services `region` is the sentinel `GLOBAL`; call the API in whatever
    region it actually lives in.
    """

    def wrap(fn: Callable[..., Iterable[Resource]]) -> Callable[..., Iterable[Resource]]:
        if scope not in ("regional", "global"):
            raise ValueError(f"bad scope {scope!r} for {service}:{resource_type}")
        REGISTRY.append(CollectorSpec(service, resource_type, scope, fn))
        return fn

    return wrap


def jsonable(value: Any) -> Any:
    """Make a boto3 response safe for a JSONB column.

    boto3 returns datetimes and occasionally Decimals and bytes; json.dumps
    chokes on all three. Config blobs are for display and diffing, so stringify
    rather than lose the field.
    """
    return json.loads(json.dumps(value, default=str))


def paginate(client: Any, operation: str, key: str, **kwargs: Any) -> Iterator[dict]:
    """Yield items from a paginated operation, or fall back to a single call.

    Not every list operation has a paginator; those that do must use one, since
    a scan that silently reads only the first page reports a resource count that
    looks plausible and is wrong.
    """
    if client.can_paginate(operation):
        for page in client.get_paginator(operation).paginate(**kwargs):
            yield from page.get(key, [])
    else:
        yield from getattr(client, operation)(**kwargs).get(key, [])


def tag_list_to_dict(tags: Any) -> dict[str, str]:
    """Normalise AWS's several tag shapes into one dict.

    Services variously return [{Key,Value}], [{key,value}], or {k: v}.
    """
    if not tags:
        return {}
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    out: dict[str, str] = {}
    for item in tags:
        if not isinstance(item, dict):
            continue
        key = item.get("Key", item.get("key"))
        if key is not None:
            out[str(key)] = str(item.get("Value", item.get("value", "")))
    return out
