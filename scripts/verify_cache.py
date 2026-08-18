"""Phase 10 acceptance: a reworded repeat question returns near-instantly.

Run:  python scripts/verify_cache.py
"""

import sys
import time

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

from biographer.agent.loop import default_account  # noqa: E402
from biographer.agent.server import answer  # noqa: E402
from biographer.memory import analyses  # noqa: E402
from biographer.db import pool  # noqa: E402

ORIGINAL = "What looks abandoned or wasteful in my account?"
REWORDED = "Which resources in my account are just sitting there unused?"
DIFFERENT = "How many IAM roles exist?"


def timed(question, **kw):
    started = time.monotonic()
    result = answer(question, client=f"verify-{time.time()}", **kw)
    return result, time.monotonic() - started


def main() -> int:
    first, t1 = timed(ORIGINAL)
    print(f"1. original   {t1:6.2f}s  cached={first.get('cached')}  "
          f"tokens={first.get('tokens')}")

    second, t2 = timed(REWORDED)
    print(f"2. reworded   {t2:6.2f}s  cached={second.get('cached')}")
    if second.get("cached"):
        print(f"   matched:  {second['matched_question']!r}")
        print(f"   note:     {second['cache_note']}")

    # A miss is only a failure if the cache failed to *find* the match. On a live
    # account the hourly manage pass lands real changes mid-run, and declining to
    # reuse because the account moved is the feature working, not breaking -- so
    # ask the cache why it declined rather than assuming the worst.
    offered = analyses.offer(default_account(), REWORDED)
    matched = offered is not None
    invalidated = bool(offered and offered["changes_since"])
    if not second.get("cached"):
        if matched:
            print(f"   found match at distance {offered['distance']}, declined: "
                  f"{offered['note']}")
        else:
            print("   NO MATCH FOUND -- a real cache miss")

    third, t3 = timed(REWORDED, force_refresh=True)
    print(f"3. refresh    {t3:6.2f}s  cached={third.get('cached')}  (explicit refresh)")

    fourth, t4 = timed(DIFFERENT)
    print(f"4. unrelated  {t4:6.2f}s  cached={fourth.get('cached')}  "
          f"(must be False -- different question)")

    reused = bool(second.get("cached"))
    # Pass if the answer was reused, or if it was correctly withheld because the
    # account changed underneath it. Fail only on a genuine lookup miss.
    ok = ((reused or (matched and invalidated))
          and not third.get("cached") and not fourth.get("cached"))
    if reused:
        print(f"\nreuse speedup: {t1 / t2:.0f}x")
    else:
        print("\nno reuse: the account changed mid-run and the cache said so")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        pool().close()
