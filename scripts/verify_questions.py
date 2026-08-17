"""Runs the seven question classes from design-summary section 3 end to end.

This is the Phase 8 acceptance test: every answer must be correct and must
carry ARNs or console-lookupable identifiers.

Run:  python scripts/verify_questions.py
"""

import re

import sys

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv()

from biographer.agent.loop import ask, default_account  # noqa: E402
from biographer.bedrock import spend_summary  # noqa: E402
from biographer.db import pool  # noqa: E402

QUESTIONS = [
    ("current state", "What EC2 instances do I have?"),
    ("waste", "What looks abandoned or wasteful in my account?"),
    ("history", "What changed in my account recently, and who did it?"),
    ("relationships", "What would break if I deleted my seeded VPC?"),
    ("convention", "What do you know about my build runner?"),
    ("meta", "What do you know about my account?"),
]

IDENTIFIER = re.compile(
    r"arn:aws[^\s`,)]+|\b(?:i|vol|sg|subnet|vpc|eipalloc|igw|rtb)-[0-9a-f]{8,17}\b"
)

def main() -> int:
    account = default_account()
    failures = 0
    for label, question in QUESTIONS:
        turn = ask(account, question)
        ids = IDENTIFIER.findall(turn.answer)
        ok = bool(ids)
        if not ok:
            failures += 1
        print(f"\n{'=' * 70}\n[{label}] {question}")
        print(f"  tools: {[c['name'] for c in turn.tool_calls]}")
        print(f"  identifiers in answer: {len(ids)}  {'OK' if ok else 'MISSING (invariant 4)'}")
        print("  " + turn.answer.strip().replace("\n", "\n  ")[:900])

    s = spend_summary()
    print(f"\n{'=' * 70}\ntotal model spend: ${s['total_usd']:.4f} over {s['calls']} calls")
    print(f"question classes missing identifiers: {failures}")
    return failures


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        pool().close()
