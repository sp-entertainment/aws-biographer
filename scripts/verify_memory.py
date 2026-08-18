"""Live verification of the Phase 6 acceptance criteria.

Needs a cluster and Bedrock access, so it is a script rather than a unit
test. Every assertion here is one of the memory invariants.

Run:  python scripts/verify_memory.py
"""

import sys

import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()
from biographer.db import pool
from biographer.memory import store
from biographer.bedrock import spend_summary

A = sys.argv[1] if len(sys.argv) > 1 else "111122223333"
FIXTURE_TOPICS = ["build-runner", "chatter", "idle-eip"]
try:
    with pool().connection() as c:
        # Scoped to this script's own fixture topics. It used to wipe the
        # account, which destroyed real retired memories the first time it was
        # pointed at a live account -- and retirement records are the one thing
        # here that cannot be recomputed from a scan.
        c.execute("DELETE FROM memories WHERE account_id=%s"
                  " AND (topic = ANY(%s) OR topic LIKE 'build-runner#unmerged-%%')",
                  (A, FIXTURE_TOPICS)); c.commit()

    print("=== 1. explicit 'remember this' bypasses the filter ===")
    # Deliberately transient wording the durability filter would normally DROP.
    m = store.remember(A, "build-runner", "The user asked about EC2 just now.",
                       resource_key="i-0ad130ebd061c9a6f", origin=store.HUMAN, explicit=True)
    print(f"   stored={not m.dropped}  origin={m.origin}  human_text_kept={m.human_text is not None}")
    assert not m.dropped, "INVARIANT 5 VIOLATED: explicit remember was filtered"

    print("\n=== 2. agent-proposed transient fact is filtered out ===")
    d = store.remember(A, "chatter", "The user said hello.", origin=store.AGENT)
    print(f"   dropped={d.dropped}")

    print("\n=== 3. agent-proposed durable fact is kept ===")
    k = store.remember(A, "idle-eip",
                       "Elastic IP eipalloc-06afacf4141ed7de7 has been unattached since first seen.",
                       resource_key="eipalloc-06afacf4141ed7de7", origin=store.AGENT,
                       claim={"check": "ec2.describe_addresses", "expect": "AssociationId is null"})
    print(f"   dropped={k.dropped}  id={k.memory_id}")

    print("\n=== 4. collision merges rather than overwrites ===")
    m2 = store.remember(A, "build-runner",
                        "It runs nightly CI builds and must not be terminated.",
                        resource_key="i-0ad130ebd061c9a6f", origin=store.AGENT)
    print(f"   merged={m2.merged}  same_row={m2.memory_id == m.memory_id}")
    with pool().connection() as c:
        body, human, origin = c.execute(
            "SELECT body, human_text, origin FROM memories WHERE memory_id=%s", (m2.memory_id,)).fetchone()
    print(f"   body:       {body[:100]}")
    print(f"   human_text: {human}")
    print(f"   origin kept as human: {origin == 'human'}")
    assert human == "The user asked about EC2 just now.", "verbatim human text was rewritten"

    print("\n=== 5. failed merge keeps both rows (invariant 6) ===")
    real_merge = store.merge_bodies
    store.merge_bodies = lambda *a, **k: None      # simulate a timeout
    try:
        m3 = store.remember(A, "build-runner",
                            "This instance was decommissioned and replaced by a Graviton runner in June.",
                            resource_key="i-0ad130ebd061c9a6f", origin=store.HUMAN, explicit=True)
        print(f"   dropped={m3.dropped}")
    finally:
        store.merge_bodies = real_merge
    with pool().connection() as c:
        n = c.execute("SELECT count(*) FROM memories WHERE account_id=%s AND resource_key=%s",
                      (A, "i-0ad130ebd061c9a6f")).fetchone()[0]
    print(f"   conflict_kept={m3.conflict_kept}  rows now={n} (must be 2, not 1)")
    assert n == 2, "INVARIANT 6 VIOLATED: a write was lost"

    print("\n=== 6. semantic recall ===")
    for hit in store.recall(A, "which machine builds our software?", limit=3):
        print(f"   {hit['distance']:.3f}  [{hit['origin']}] {hit['topic']}: {hit['body'][:60]}")

    print("\n=== model spend so far ===")
    s = spend_summary()
    print(f"   ${s['total_usd']:.5f} over {s['calls']} calls, {s['errors']} errors")
    for p, v in s["by_purpose"].items():
        print(f"     {p:<12} {v['calls']:>3} calls  ${v['cost_usd']:.5f}")
finally:
    pool().close()
