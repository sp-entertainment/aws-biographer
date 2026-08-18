"""The manage pass: scan, diff, record findings, verify, retire.

Design summary §5 says this should be **mostly dumb** -- no model invocation
unless something actually moved. That is honoured literally here:

  - The scan and diff are pure code.
  - Verification is pure SQL against the cache the scan just refreshed.
  - Findings are written only when the account changed, or when a periodic
    sweep is explicitly asked for.

On a quiet account this pass costs one multi-region scan and a handful of
indexed queries, and invokes no model at all. That is what makes it affordable
to run on a schedule, and running it on a schedule is what makes memory
verify itself instead of decaying quietly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .aws import account_id_of, session_for
from .db import pool
from .memory import findings, propose
from .memory.verify import VerificationRun, verify_all
from .scan.cloudtrail import backfill
from .scan.runner import run as run_scan

log = logging.getLogger(__name__)


@dataclass
class ManageResult:
    account_id: str
    resources: int = 0
    changes: int = 0
    edges: int = 0
    cloudtrail_events: int = 0
    findings: dict[str, int] = field(default_factory=dict)
    embedded: int = 0
    proposals: int = 0
    verification: VerificationRun | None = None
    woke_the_model: bool = False

    def summary(self) -> str:
        v = self.verification
        return (
            f"account {self.account_id}: {self.resources} resources, "
            f"{self.changes} changes, {self.edges} edges | "
            f"verified {v.checked if v else 0} memories "
            f"({v.refreshed if v else 0} refreshed, {v.retired if v else 0} retired, "
            f"{v.unverifiable if v else 0} unverifiable) | "
            f"model woken: {self.woke_the_model} | "
            f"embedded {self.embedded}, {self.proposals} edge proposals"
        )


def run(*, force_findings: bool = False, backfill_history: bool = False) -> ManageResult:
    """One full pass."""
    session = session_for()
    account = account_id_of(session)
    result = ManageResult(account_id=account)

    scan = run_scan()
    result.resources = len(scan.resources)
    result.changes = len(scan.delta)
    result.edges = scan.edges

    if backfill_history:
        history = backfill(session, account, scan.regions)
        result.cloudtrail_events = history.inserted

    # The only model calls in this pass live behind findings.record(), and only
    # a real diff opens that door. A quiet account costs nothing.
    if result.changes or force_findings:
        result.woke_the_model = True
        result.findings = findings.record(account)
    else:
        log.info("nothing moved; not waking the model")

    # Embeddings for the clustering that proposes candidate edges. Only
    # resources that lack one are embedded, so this converges to zero cost.
    result.embedded = propose.backfill_embeddings(account)
    result.proposals = len(propose.candidates(account))

    # Verification always runs. It is pure SQL against the cache the scan just
    # refreshed, so it is nearly free -- and skipping it on a quiet account
    # would be exactly backwards, since a memory can be made false by a change
    # this scan is the first thing to notice.
    result.verification = verify_all(account)
    return result


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """EventBridge Scheduler entry point."""
    outcome = run(
        force_findings=bool(event.get("force_findings")),
        backfill_history=bool(event.get("backfill")),
    )
    v = outcome.verification
    return {
        "statusCode": 200,
        "body": json.dumps({
            "account_id": outcome.account_id,
            "resources": outcome.resources,
            "changes": outcome.changes,
            "findings": outcome.findings,
            "verified": v.checked if v else 0,
            "retired": v.retired if v else 0,
            "woke_the_model": outcome.woke_the_model,
        }),
    }


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        outcome = run(
            force_findings="--findings" in sys.argv,
            backfill_history="--backfill" in sys.argv,
        )
        print("\n" + outcome.summary())
        if outcome.findings:
            print(f"findings: {outcome.findings}")
        retirements = outcome.verification.retirements if outcome.verification else []
        if retirements:
            print(f"\nRETIRED {len(retirements)} memories:")
            for item in retirements:
                print(f"  [{item['origin']}] {item['topic']}")
                print(f"      was: {item['body'][:110]}")
                print(f"      why: {item['reason']}")
    finally:
        pool().close()
