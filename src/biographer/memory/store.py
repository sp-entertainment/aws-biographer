"""The memory write path.

Design summary §5 fixes the rules here and they were arrived at by eliminating
alternatives, so they are stated as constraints rather than preferences:

  - An explicit "remember this" is stored unfiltered (invariant 5). An agent
    that overrules a direct instruction destroys trust, and human-supplied
    knowledge is the single highest-value memory class.
  - Writes happen immediately, never deferred to an end-of-conversation hook.
    A hook does not fire when a tab closes, and what gets lost is exactly the
    human annotation you most wanted.
  - On collision, merge inline with the cheap model. If the merge fails or times
    out, insert alongside rather than dropping (invariant 6).
  - Human-supplied text is kept verbatim in a field no merge may rewrite, and
    merge output has a length ceiling. Those two together are the guard against
    summarisation drift.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from psycopg.types.json import Jsonb

from ..bedrock import ask_cheap, embed
from ..db import pool, to_vector

log = logging.getLogger(__name__)

HUMAN = "human"
AGENT = "agent"

# Merge output ceiling. Without it, repeated merges grow a memory until it is a
# transcript, and every summarisation step drifts a little further from what was
# actually observed.
MAX_BODY_CHARS = 600

DURABILITY_SYSTEM = """You judge whether a fact about an AWS account is worth \
remembering long-term.

Answer KEEP only if the fact would still be true and useful a month from now.
Answer DROP if it is cheaply re-derivable from a live API call, or is a \
transient detail of one conversation.

"An Elastic IP is currently unattached" is DROP -- one API call recomputes it.
"This Elastic IP has been unattached since March" is KEEP -- only time reveals it.
"The user asked about EC2" is DROP.

Reply with exactly one word: KEEP or DROP."""

MERGE_SYSTEM = f"""You merge two facts about the same AWS resource into one.

Combine them if both still hold. Replace the old entirely if the underlying \
state genuinely changed. Never invent detail present in neither.

Be specific and keep identifiers. Reply with the merged fact only, no preamble, \
under {MAX_BODY_CHARS} characters."""


@dataclass
class Memory:
    account_id: str
    topic: str
    body: str
    resource_key: str = ""
    origin: str = AGENT
    human_text: str | None = None
    claim: dict[str, Any] | None = None
    memory_id: str | None = None
    merged: bool = False
    dropped: bool = False
    conflict_kept: bool = False


@dataclass
class WriteResult:
    stored: list[Memory] = field(default_factory=list)
    dropped: list[Memory] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.stored)


def is_durable(text: str, account_id: str | None = None) -> bool:
    """Would this still be true and useful a month from now?

    Deliberately not a vague "is this high signal" judgement -- that phrasing
    was rejected because it gives the model nothing to actually decide against.
    Failure is treated as KEEP: a false positive costs one row, a false negative
    silently loses knowledge.
    """
    try:
        verdict = ask_cheap(text, system=DURABILITY_SYSTEM, max_tokens=5,
                            purpose="durability", account_id=account_id)
    except Exception:  # noqa: BLE001
        log.warning("durability check failed, keeping by default", exc_info=True)
        return True
    return not verdict.strip().upper().startswith("DROP")


def merge_bodies(old: str, new: str, account_id: str | None = None) -> str | None:
    """Combine two memory bodies, or None if the merge could not be done.

    Returning None is a real outcome, not an error to swallow: the caller's
    contract on None is to keep both rows rather than lose a write.
    """
    try:
        merged = ask_cheap(
            f"OLD FACT:\n{old}\n\nNEW FACT:\n{new}\n\nMerged fact:",
            system=MERGE_SYSTEM,
            max_tokens=400,
            purpose="merge",
            account_id=account_id,
        )
    except Exception:  # noqa: BLE001
        log.warning("merge failed; will insert alongside rather than lose a write",
                    exc_info=True)
        return None
    merged = merged.strip()
    if not merged:
        return None
    return merged[:MAX_BODY_CHARS]


def remember(
    account_id: str,
    topic: str,
    body: str,
    *,
    resource_key: str = "",
    origin: str = AGENT,
    claim: dict[str, Any] | None = None,
    explicit: bool = False,
) -> Memory:
    """Write one memory, merging on collision.

    `explicit=True` means the user said "remember this". It bypasses the
    durability filter entirely -- invariant 5 admits no exceptions.
    """
    memory = Memory(account_id, topic, body, resource_key, origin, claim=claim)

    if not explicit and origin == AGENT and not is_durable(body, account_id):
        memory.dropped = True
        return memory

    # Human wording is preserved verbatim in a field no merge may rewrite.
    human_text = body if origin == HUMAN else None

    with pool().connection() as conn:
        existing = conn.execute(
            "SELECT memory_id, body, origin, human_text FROM memories"
            " WHERE account_id = %s AND resource_key = %s AND topic = %s"
            "   AND retired_at IS NULL",
            (account_id, resource_key, topic),
        ).fetchone()

        if existing is None:
            row = conn.execute(
                "INSERT INTO memories (account_id, resource_key, topic, body,"
                " human_text, origin, claim, embedding, verified_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())"
                " RETURNING memory_id",
                (account_id, resource_key, topic, body[:MAX_BODY_CHARS], human_text,
                 origin, Jsonb(claim) if claim else None,
                 to_vector(embed(body, account_id))),
            ).fetchone()
            conn.commit()
            memory.memory_id = str(row[0])
            memory.human_text = human_text
            return memory

        old_id, old_body, old_origin, old_human = existing
        merged = merge_bodies(old_body, body, account_id)

        if merged is None:
            # Invariant 6: never lose a write. The unique key is already taken,
            # so the new memory lands under a disambiguated topic and a human
            # can reconcile the two later.
            row = conn.execute(
                "INSERT INTO memories (account_id, resource_key, topic, body,"
                " human_text, origin, claim, embedding, verified_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())"
                " RETURNING memory_id",
                (account_id, resource_key, f"{topic}#unmerged-{str(old_id)[:8]}",
                 body[:MAX_BODY_CHARS], human_text, origin,
                 Jsonb(claim) if claim else None,
                 to_vector(embed(body, account_id))),
            ).fetchone()
            conn.commit()
            memory.memory_id = str(row[0])
            memory.conflict_kept = True
            return memory

        # A memory that was ever human-supplied keeps that provenance, and the
        # original human wording is never overwritten by a merge.
        conn.execute(
            "UPDATE memories SET body = %s, human_text = coalesce(%s, human_text),"
            " origin = CASE WHEN origin = 'human' OR %s = 'human' THEN 'human'"
            "               ELSE origin END,"
            " claim = coalesce(%s, claim), embedding = %s,"
            " updated_at = now(), verified_at = now()"
            " WHERE memory_id = %s",
            (merged, human_text, origin, Jsonb(claim) if claim else None,
             to_vector(embed(merged, account_id)), old_id),
        )
        conn.commit()
        memory.memory_id = str(old_id)
        memory.body = merged
        memory.merged = True
        memory.human_text = old_human or human_text
        return memory


def recall(account_id: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """Nearest live memories by cosine distance."""
    vector = to_vector(embed(query, account_id))
    with pool().connection() as conn:
        return [
            {"memory_id": str(r[0]), "topic": r[1], "body": r[2], "origin": r[3],
             "resource_key": r[4], "verified_at": r[5], "distance": float(r[6])}
            for r in conn.execute(
                "SELECT memory_id, topic, body, origin, resource_key, verified_at,"
                " embedding <=> %s AS distance FROM memories"
                " WHERE account_id = %s AND retired_at IS NULL"
                " ORDER BY distance LIMIT %s",
                (vector, account_id, limit),
            )
        ]


def retire(memory_id: str, reason: str) -> None:
    """Retire, never delete. A retired memory is still evidence of what we believed."""
    with pool().connection() as conn:
        conn.execute(
            "UPDATE memories SET retired_at = now(), retire_reason = %s"
            " WHERE memory_id = %s AND retired_at IS NULL",
            (reason, memory_id),
        )
        conn.commit()
