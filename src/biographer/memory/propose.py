"""Candidate edges from vector clustering -- hunches, never facts.

Design summary §6 is emphatic and this module is built around it: **vectors do
not represent edges.** Two Lambdas doing similar work sit close together in
embedding space with no relationship whatsoever, so promoting similarity to a
confirmed edge would rebuild the self-reinforcing-error failure mode by hand.

What similarity is genuinely good for is *asking a better question*. Resources
that cluster tightly while sharing no known edge are exactly the places where
the config-derived graph has holes -- because the thing that relates them is a
human intention that appears in no API response. So the agent proposes, and the
user's answer writes the edge.

This is also what makes the human layer structurally necessary rather than a
nice extra: the graph has holes only conversation can fill, and the agent can
point at them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..bedrock import embed
from ..db import pool, to_vector

log = logging.getLogger(__name__)

# Cosine distance below which two resources are "suspiciously similar". Set from
# the seeded account: resources sharing a naming prefix and purpose land under
# 0.25, while unrelated resources of the same type sit above 0.35.
CLUSTER_DISTANCE = 0.25

# Never propose more than this per pass. A wall of questions is not a
# conversation, and an agent that asks twenty things at once gets ignored.
MAX_PROPOSALS = 5


@dataclass
class Proposal:
    src_arn: str
    dst_arn: str
    src_name: str
    dst_name: str
    distance: float

    @property
    def question(self) -> str:
        return (
            f"{self.src_name} ({self.src_arn}) and {self.dst_name} ({self.dst_arn}) "
            f"look closely related, but nothing in their configuration connects them. "
            f"Are they part of the same feature? If so, what is it called?"
        )


def _describe(row: tuple) -> str:
    """The text a resource is embedded from.

    Name, type and tags -- deliberately not the whole config blob. Config is
    mostly identifiers and booleans that drag unrelated resources together in
    embedding space; intent lives in what people named things.
    """
    arn, service, kind, name, tags = row
    parts = [f"{service} {kind}", name or arn.rsplit("/", 1)[-1]]
    if tags:
        parts.extend(f"{k}={v}" for k, v in sorted(tags.items()))
    return " | ".join(parts)


def backfill_embeddings(account_id: str, limit: int = 200) -> int:
    """Embed resources that have none. Incremental and safe to re-run."""
    with pool().connection() as conn:
        rows = conn.execute(
            "SELECT arn, service, resource_type, name, tags FROM resources"
            " WHERE account_id = %s AND embedding IS NULL LIMIT %s",
            (account_id, limit),
        ).fetchall()

    for row in rows:
        vector = to_vector(embed(_describe(row), account_id))
        with pool().connection() as conn:
            conn.execute(
                "UPDATE resources SET embedding = %s, embedded_at = now()"
                " WHERE account_id = %s AND arn = %s",
                (vector, account_id, row[0]),
            )
            conn.commit()
    if rows:
        log.info("embedded %d resources", len(rows))
    return len(rows)


def candidates(account_id: str, limit: int = MAX_PROPOSALS) -> list[Proposal]:
    """Tightly clustered resource pairs with no edge between them.

    One query: the similarity join and the "no known edge" filter happen in the
    same statement, in the same store. This is the concrete form of the claim
    that a separate graph database was not needed -- vector similarity and graph
    adjacency are joined here, not across a network hop.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.arn, b.arn, a.name, b.name, a.embedding <=> b.embedding AS distance
              FROM resources a
              JOIN resources b
                ON b.account_id = a.account_id
               AND a.arn < b.arn          -- each unordered pair once
              WHERE a.account_id = %(account)s
                AND a.embedding IS NOT NULL
                AND b.embedding IS NOT NULL
                AND a.embedding <=> b.embedding < %(distance)s
                -- Only propose what the graph does not already know, in either
                -- direction. Re-asking about a known edge wastes the user's
                -- attention, which is the scarcest thing in this design.
                AND NOT EXISTS (
                      SELECT 1 FROM edges e
                       WHERE e.account_id = a.account_id
                         AND ((e.src_arn = a.arn AND e.dst_arn = b.arn)
                           OR (e.src_arn = b.arn AND e.dst_arn = a.arn)))
              ORDER BY distance ASC
              LIMIT %(limit)s
            """,
            {"account": account_id, "distance": CLUSTER_DISTANCE, "limit": limit},
        ).fetchall()

    return [
        Proposal(src_arn=r[0], dst_arn=r[1],
                 src_name=r[2] or r[0].rsplit("/", 1)[-1],
                 dst_name=r[3] or r[1].rsplit("/", 1)[-1],
                 distance=float(r[4]))
        for r in rows
    ]
