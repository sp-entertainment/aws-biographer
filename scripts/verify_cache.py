"""Phase 10 acceptance: a reworded repeat question returns near-instantly.

Run:  python scripts/verify_cache.py
"""

import sys
import time

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

from biographer.agent.server import answer  # noqa: E402
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

    third, t3 = timed(REWORDED, force_refresh=True)
    print(f"3. refresh    {t3:6.2f}s  cached={third.get('cached')}  (explicit refresh)")

    fourth, t4 = timed(DIFFERENT)
    print(f"4. unrelated  {t4:6.2f}s  cached={fourth.get('cached')}  "
          f"(must be False -- different question)")

    ok = second.get("cached") and not third.get("cached") and not fourth.get("cached")
    speedup = t1 / t2 if t2 else 0
    print(f"\nreuse speedup: {speedup:.0f}x")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        pool().close()
