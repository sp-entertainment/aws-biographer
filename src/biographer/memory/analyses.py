"""Cached analyses: storing *work*, not state.

Design summary §7 calls this what makes memory load-bearing rather than
decorative. The expensive operation in this product is not a database read, it
is the multi-region scan plus a chain of model calls -- and a rephrased repeat
of the same question re-runs all of it for an answer that has not changed.

So a completed analysis is stored with an embedding of the question that
produced it. A later question that lands close enough in embedding space is
answered from cache, with the age stated and a refresh offered.

Reuse-or-refresh is offered, never decided silently. A cached answer presented
as fresh is indistinguishable from a stale one, and this product's entire claim
is that it knows the difference.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb

from ..bedrock import embed
from ..db import pool, to_vector

log = logging.getLogger(__name__)

# Cosine distance below which two questions are treated as the same question.
# Tuned by hand against real rephrasings: "what looks abandoned?" vs "what is
# wasteful in my account?" land around 0.35, while genuinely different questions
# about the same account sit well above 0.5. Erring tight is deliberate -- a
# false match answers the wrong question, a miss only costs a re-run.
SAME_QUESTION_DISTANCE = 0.42

# Past this age the answer is offered as reuse-or-refresh with the age stated
# prominently rather than quietly served.
STALE_AFTER = dt.timedelta(hours=6)


@dataclass
class CachedAnalysis:
    analysis_id: str
    question: str
    answer: str
    distance: float
    age: dt.timedelta
    inputs: dict[str, Any]

    @property
    def is_stale(self) -> bool:
        return self.age > STALE_AFTER

    @property
    def age_text(self) -> str:
        seconds = int(self.age.total_seconds())
        if seconds < 90:
            return "just now"
        if seconds < 5400:
            return f"{seconds // 60} minutes ago"
        if seconds < 172800:
            return f"{seconds // 3600} hours ago"
        return f"{seconds // 86400} days ago"


def store(account_id: str, question: str, answer: str,
          inputs: dict[str, Any] | None = None,
          cost_usd: float | None = None) -> str:
    """Remember that this question was answered, and how."""
    vector = to_vector(embed(question, account_id))
    with pool().connection() as conn:
        row = conn.execute(
            "INSERT INTO analyses (account_id, question, question_embedding, answer,"
            " inputs, cost_usd) VALUES (%s, %s, %s, %s, %s, %s) RETURNING analysis_id",
            (account_id, question, vector, answer, Jsonb(inputs or {}), cost_usd),
        ).fetchone()
        conn.commit()
    return str(row[0])


def lookup(account_id: str, question: str) -> CachedAnalysis | None:
    """The nearest previous answer to this question, if it is near enough."""
    vector = to_vector(embed(question, account_id))
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT analysis_id, question, answer, inputs, refreshed_at,"
            " question_embedding <=> %s AS distance FROM analyses"
            " WHERE account_id = %s ORDER BY distance LIMIT 1",
            (vector, account_id),
        ).fetchone()

    if row is None or float(row[5]) > SAME_QUESTION_DISTANCE:
        return None

    refreshed = row[4]
    now = dt.datetime.now(refreshed.tzinfo) if refreshed.tzinfo else dt.datetime.now()
    return CachedAnalysis(
        analysis_id=str(row[0]),
        question=row[1],
        answer=row[2],
        distance=float(row[5]),
        age=now - refreshed,
        inputs=row[3] or {},
    )


def touch(analysis_id: str, answer: str | None = None) -> None:
    """Mark an analysis refreshed, optionally replacing its answer."""
    with pool().connection() as conn:
        if answer is None:
            conn.execute("UPDATE analyses SET refreshed_at = now()"
                         " WHERE analysis_id = %s", (analysis_id,))
        else:
            conn.execute("UPDATE analyses SET refreshed_at = now(), answer = %s"
                         " WHERE analysis_id = %s", (answer, analysis_id))
        conn.commit()


def invalidated_by_changes(account_id: str, cached: CachedAnalysis) -> int:
    """How many account changes have landed since this analysis was computed.

    An age-based staleness rule alone is wrong in both directions: an account
    that nobody touched does not go stale in six hours, and an account that had
    forty resources deleted is stale in six minutes. Counting real change-log
    entries since the analysis is the honest measure, and it is one indexed
    query.
    """
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM changes WHERE account_id = %s"
            "   AND event_time > (SELECT refreshed_at FROM analyses"
            "                      WHERE analysis_id = %s)",
            (account_id, cached.analysis_id),
        ).fetchone()
    return int(row[0])


def offer(account_id: str, question: str) -> dict[str, Any] | None:
    """Reuse-or-refresh for a repeat question, or None to compute fresh."""
    cached = lookup(account_id, question)
    if cached is None:
        return None

    changes = invalidated_by_changes(account_id, cached)
    return {
        "analysis_id": cached.analysis_id,
        "answer": cached.answer,
        "original_question": cached.question,
        "age": cached.age_text,
        "distance": round(cached.distance, 3),
        "changes_since": changes,
        # The recommendation is stated; the choice stays with the caller.
        "recommend_refresh": changes > 0 or cached.is_stale,
        "note": (
            f"Answered {cached.age_text}"
            + (f", and {changes} account change(s) have landed since"
               if changes else ", and nothing in the account has changed since")
        ),
    }
