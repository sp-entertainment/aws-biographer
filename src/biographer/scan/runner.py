"""Run a scan and land it in the current-state cache.

Collectors are best-effort by contract. A denial, an opted-out region, or a
service that doesn't exist where we asked is recorded and stepped over -- a scan
that aborts on the first AccessDenied would be useless against exactly the
accounts this product exists to study.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from psycopg.types.json import Jsonb

from ..aws import account_id_of, client, session_for
from ..config import settings
from ..db import pool
from . import collectors as collectors_pkg
from ..graph import store as store_edges
from .diff import DiffResult, compute, to_rows
from .edges import extract as extract_edges
from .model import GLOBAL, REGISTRY, CollectorSpec, Resource
from .regions import discover

log = logging.getLogger(__name__)


def load_collectors() -> list[CollectorSpec]:
    """Import every module under `collectors/` so decorators run.

    Registration is a side effect of import, so without this the registry is
    empty and a scan reports zero resources with no error at all -- the worst
    possible failure for an inventory tool.
    """
    for module in pkgutil.iter_modules(collectors_pkg.__path__):
        importlib.import_module(f"{collectors_pkg.__name__}.{module.name}")
    if not REGISTRY:
        raise RuntimeError("no collectors registered")
    return REGISTRY


@dataclass
class ScanResult:
    account_id: str
    regions: list[str] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    disappeared: list[str] = field(default_factory=list)
    coverage_gaps: dict[str, int] = field(default_factory=dict)
    delta: DiffResult = field(default_factory=DiffResult)
    edges: int = 0

    @property
    def by_service(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.resources:
            counts[r.service] = counts.get(r.service, 0) + 1
        return dict(sorted(counts.items()))


def _run_one(spec: CollectorSpec, session: boto3.Session, region: str) -> list[Resource]:
    return list(spec.fn(session, region))


def collect(
    session: boto3.Session, regions: list[str], max_workers: int = 16
) -> tuple[list[Resource], dict[str, str]]:
    """Fan every collector across every region it applies to."""
    specs = load_collectors()
    jobs: list[tuple[CollectorSpec, str]] = []
    for spec in specs:
        if spec.scope == "global":
            jobs.append((spec, GLOBAL))
        else:
            jobs.extend((spec, region) for region in regions)

    found: list[Resource] = []
    failures: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool_:
        futures = {
            pool_.submit(_run_one, spec, session, region): (spec, region)
            for spec, region in jobs
        }
        for future in as_completed(futures):
            spec, region = futures[future]
            try:
                found.extend(future.result())
            except (ClientError, BotoCoreError) as exc:
                # Expected and uninteresting: the service doesn't exist in this
                # region, or the role can't see it. Recorded, not raised.
                code = (
                    exc.response["Error"]["Code"]
                    if isinstance(exc, ClientError)
                    else type(exc).__name__
                )
                failures[f"{spec.label}@{region}"] = code
            except Exception as exc:  # noqa: BLE001 - one bad collector must not kill a scan
                failures[f"{spec.label}@{region}"] = f"{type(exc).__name__}: {exc}"
                log.warning("collector %s@%s crashed", spec.label, region, exc_info=True)

    log.info("collected %d resources, %d collector failures", len(found), len(failures))
    return found, failures


def reconcile(
    session: boto3.Session, regions: list[str], found: list[Resource]
) -> dict[str, int]:
    """What the Tagging API can see that no collector covers.

    Collector coverage is a long tail and completeness is explicitly not the
    goal, but *unknown* coverage is a different thing entirely: it turns a
    missing resource into a plausible-looking count that is quietly wrong. The
    Tagging API gives a cheap second opinion over taggable resources, so any ARN
    it returns that no collector produced is a named, countable gap.

    This is one-directional on purpose. The reverse -- things we found that the
    Tagging API missed -- is the expected case, not a gap, because untaggable
    and untagged resources are most of what this product exists to surface.
    """
    seen = {r.arn for r in found}
    gaps: dict[str, int] = {}
    for region in regions:
        try:
            tagging = client(session, "resourcegroupstaggingapi", region)
            for page in tagging.get_paginator("get_resources").paginate():
                for item in page.get("ResourceTagMappingList", []):
                    arn = item.get("ResourceARN", "")
                    if arn and arn not in seen:
                        # arn:partition:service:region:account:type/id
                        parts = arn.split(":")
                        service = parts[2] if len(parts) > 2 else "?"
                        kind = parts[5].split("/")[0] if len(parts) > 5 else "?"
                        gaps[f"{service}:{kind}"] = gaps.get(f"{service}:{kind}", 0) + 1
        except (ClientError, BotoCoreError) as exc:
            log.warning("reconcile failed in %s: %s", region, exc)
    if gaps:
        log.info("uncovered taggable resources: %s", gaps)
    return gaps


def persist(result: ScanResult) -> str:
    """Upsert into the cache and report what vanished. Returns the scan id.

    `first_seen` is deliberately excluded from the update. It is the cheapest
    source of duration in the whole system -- what makes "unattached since
    March" a storable memory instead of a recomputable fact -- and overwriting
    it on every scan would quietly destroy that.

    The diff runs before the upsert, because the previous scan's state is the
    only thing that says what changed and the upsert destroys it. Resources that
    vanished from a swept region are recorded as deletions in the change log
    first, then removed from the cache -- deleting them first would throw away
    the evidence of the deletion.
    """
    with pool().connection() as conn:
        conn.execute(
            "INSERT INTO accounts (account_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (result.account_id,),
        )

        swept = set(result.regions) | {GLOBAL}
        previous = {
            row[0]: {"region": row[1], "name": row[2], "tags": row[3], "config": row[4]}
            for row in conn.execute(
                "SELECT arn, region, name, tags, config FROM resources"
                " WHERE account_id = %s AND region = ANY(%s)",
                (result.account_id, list(swept)),
            )
        }
        current = {
            r.arn: {"region": r.region, "name": r.name, "tags": r.tags, "config": r.config}
            for r in result.resources
        }
        scan_id = conn.execute(
            "INSERT INTO scans (account_id, regions, stats) VALUES (%s, %s, %s)"
            " RETURNING scan_id",
            (
                result.account_id,
                Jsonb(result.regions),
                Jsonb(
                    {
                        "failures": result.failures,
                        "by_service": result.by_service,
                        "coverage_gaps": result.coverage_gaps,
                    }
                ),
            ),
        ).fetchone()[0]

        rows: list[tuple[Any, ...]] = [
            (
                result.account_id,
                r.arn,
                r.region,
                r.service,
                r.resource_type,
                r.name,
                Jsonb(r.tags),
                Jsonb(r.config),
                scan_id,
            )
            for r in result.resources
        ]
        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO resources (account_id, arn, region, service,"
                    " resource_type, name, tags, config, last_scan_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (account_id, arn) DO UPDATE SET"
                    "   region = excluded.region,"
                    "   service = excluded.service,"
                    "   resource_type = excluded.resource_type,"
                    "   name = excluded.name,"
                    "   tags = excluded.tags,"
                    "   config = excluded.config,"
                    "   last_seen = now(),"
                    "   last_scan_id = excluded.last_scan_id",
                    rows,
                )

        # The very first scan of an account is not a moment when every resource
        # was "created" -- it is the moment we started looking. Recording it as
        # creation would put a false origin date on the whole account.
        first_ever = not previous
        delta = compute(previous, current, swept)
        result.delta = delta

        if not first_ever and len(delta):
            change_rows = to_rows(result.account_id, str(scan_id), delta)
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO changes (account_id, arn, region, change_type,"
                    " field, old_value, new_value, actor, source, event_id,"
                    " event_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, now())",
                    change_rows,
                )

        # Evidence is written; now the cache can forget what vanished.
        result.disappeared = [d.arn for d in delta.deleted]
        if result.disappeared:
            conn.execute(
                "DELETE FROM resources WHERE account_id = %s AND arn = ANY(%s)",
                (result.account_id, result.disappeared),
            )

        conn.execute(
            "UPDATE scans SET finished_at = now() WHERE scan_id = %s", (scan_id,)
        )
        conn.commit()

    return str(scan_id)


def run(home_region: str | None = None) -> ScanResult:
    session = session_for()
    home = home_region or settings().aws_region
    account = account_id_of(session)

    regions, _signals = discover(session, home)
    resources, failures = collect(session, regions)

    result = ScanResult(
        account_id=account,
        regions=regions,
        resources=resources,
        failures=failures,
        coverage_gaps=reconcile(session, regions, resources),
    )
    scan_id = persist(result)
    result.edges = store_edges(account, extract_edges(resources))
    log.info("scan %s stored %d resources", scan_id, len(result.resources))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        outcome = run()
        print(f"\naccount   {outcome.account_id}")
        print(f"regions   {', '.join(outcome.regions)}")
        print(f"resources {len(outcome.resources)}")
        print(f"edges     {outcome.edges}")
        for service, count in outcome.by_service.items():
            print(f"  {service:<12} {count}")
        if outcome.coverage_gaps:
            print("\nuncovered taggable resources (no collector):")
            for label, count in sorted(outcome.coverage_gaps.items()):
                print(f"  {label:<40} {count}")
        d = outcome.delta
        if len(d):
            print(f"\nchanges since last scan: {len(d)}")
            for label, items in (("created", d.created), ("deleted", d.deleted),
                                 ("modified", d.modified)):
                for item in items[:10]:
                    suffix = f" [{item.field_name}]" if item.field_name else ""
                    print(f"  {label:<9} {item.arn}{suffix}")
        else:
            print("\nno changes since last scan")
        if outcome.failures:
            print(f"\ncollector failures ({len(outcome.failures)}):")
            for label, code in sorted(outcome.failures.items())[:20]:
                print(f"  {label:<40} {code}")
    finally:
        pool().close()
