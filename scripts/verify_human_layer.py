"""Phase 12 acceptance: suppression survives a scan, and the agent asks.

Run:  python scripts/verify_human_layer.py
"""

import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

from biographer.agent.loop import ask, default_account  # noqa: E402
from biographer.db import pool  # noqa: E402
from biographer.manage import run as manage_run  # noqa: E402
from biographer.memory import findings, propose  # noqa: E402


def live_topics(account, prefix):
    with pool().connection() as conn:
        return [r[0] for r in conn.execute(
            "SELECT topic FROM memories WHERE account_id=%s AND retired_at IS NULL"
            "  AND topic LIKE %s", (account, prefix + "%"))]


def main() -> int:
    account = default_account()
    failures = 0

    print("=== 1. edge proposals from vector clustering ===")
    for p in propose.candidates(account)[:3]:
        print(f"  d={p.distance:.3f}  {p.src_name}  <->  {p.dst_name}")

    print("\n=== 2. the agent asks about a cluster it cannot name ===")
    turn = ask(account, "What are you unsure about in my account? Ask me something useful.")
    print("  tools:", [c["name"] for c in turn.tool_calls])
    print("  " + turn.answer.strip().replace("\n", "\n  ")[:600])
    if "propose_relationships" not in [c["name"] for c in turn.tool_calls]:
        print("  WARNING: agent did not consult the proposals")

    print("\n=== 3. suppression ===")
    before = live_topics(account, "no-retention")
    print(f"  live 'no-retention' memories before: {len(before)}")
    findings.suppress(account, "no-log-retention",
                      reason="log retention is intentional on this account")
    after = live_topics(account, "no-retention")
    print(f"  immediately after suppressing:      {len(after)}")
    if after:
        print("  FAIL: suppression did not retire the existing memory")
        failures += 1

    print("\n=== 4. does it stay suppressed through a full scan? ===")
    outcome = manage_run(force_findings=True)
    again = live_topics(account, "no-retention")
    print(f"  after a full manage pass:           {len(again)}")
    print(f"  findings report: {outcome.findings}")
    if again:
        print("  FAIL: the finding came back")
        failures += 1
    else:
        print("  PASS: suppressed finding did not re-appear")

    return failures


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        pool().close()
