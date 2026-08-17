"""Persisting and traversing the resource graph.

Traversal is a recursive CTE. Depth is always bounded: blast-radius questions
are two or three hops, and an unbounded walk over a cyclic graph is how you turn
a fast query into a hang.
"""

from __future__ import annotations

import logging
from typing import Any

from .db import pool
from .scan.edges import CONFIG, Edge

log = logging.getLogger(__name__)

DEFAULT_DEPTH = 3


def store(account_id: str, edges: list[Edge], replace_config: bool = True) -> int:
    """Write edges. Config edges are replaced wholesale; human edges never are.

    Invariant 7: a scan must not delete what a person told us. The delete below
    is filtered to `source = 'config'` for exactly that reason, and widening it
    to "everything this scan didn't see" would silently destroy the human layer
    the first time a scan ran with reduced permissions.
    """
    with pool().connection() as conn:
        if replace_config:
            conn.execute(
                "DELETE FROM edges WHERE account_id = %s AND source = %s",
                (account_id, CONFIG),
            )
        if edges:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO edges (account_id, src_arn, dst_arn, edge_type,"
                    " source, confidence, note, last_seen)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, now())"
                    " ON CONFLICT (account_id, src_arn, dst_arn, edge_type)"
                    " DO UPDATE SET last_seen = now(),"
                    "   confidence = excluded.confidence,"
                    # A human assertion outranks a later machine guess about the
                    # same pair; never let a scan downgrade it.
                    "   source = CASE WHEN edges.source = 'human'"
                    "                 THEN edges.source ELSE excluded.source END",
                    [
                        (
                            account_id,
                            e.src_arn,
                            e.dst_arn,
                            e.edge_type,
                            e.source,
                            e.confidence,
                            e.note,
                        )
                        for e in edges
                    ],
                )
        conn.commit()
    return len(edges)


def neighbours(account_id: str, arn: str) -> list[dict[str, Any]]:
    """One hop in both directions."""
    with pool().connection() as conn:
        return [
            {"arn": row[0], "edge_type": row[1], "direction": row[2], "source": row[3]}
            for row in conn.execute(
                "SELECT dst_arn, edge_type, 'out', source FROM edges"
                "  WHERE account_id = %s AND src_arn = %s"
                " UNION ALL"
                " SELECT src_arn, edge_type, 'in', source FROM edges"
                "  WHERE account_id = %s AND dst_arn = %s",
                (account_id, arn, account_id, arn),
            )
        ]


def blast_radius(
    account_id: str, arn: str, depth: int = DEFAULT_DEPTH
) -> list[dict[str, Any]]:
    """What depends on this resource, walking incoming edges.

    Direction matters and is easy to get backwards. "What breaks if I delete
    this?" asks for things that point AT the resource -- the instance that uses
    the security group, not the VPC the security group lives in. Walking
    outgoing edges would confidently return everything the resource depends on,
    which is the opposite answer.
    """
    with pool().connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE walk(arn, edge_type, depth, path) AS (
                SELECT src_arn, edge_type, 1, ARRAY[dst_arn, src_arn]
                  FROM edges
                 WHERE account_id = %(account)s AND dst_arn = %(arn)s
                UNION ALL
                SELECT e.src_arn, e.edge_type, w.depth + 1, w.path || e.src_arn
                  FROM edges e
                  JOIN walk w ON e.dst_arn = w.arn
                 WHERE e.account_id = %(account)s
                   AND w.depth < %(depth)s
                   -- Cycles are real in AWS graphs; without this the walk
                   -- never terminates.
                   AND e.src_arn != ALL(w.path)
            )
            SELECT w.arn, w.edge_type, min(w.depth) AS depth,
                   r.service, r.resource_type, r.name
              FROM walk w
              LEFT JOIN resources r
                ON r.account_id = %(account)s AND r.arn = w.arn
             GROUP BY w.arn, w.edge_type, r.service, r.resource_type, r.name
             ORDER BY depth, w.arn
            """,
            {"account": account_id, "arn": arn, "depth": depth},
        ).fetchall()

    return [
        {
            "arn": r[0],
            "edge_type": r[1],
            "depth": r[2],
            "service": r[3],
            "resource_type": r[4],
            "name": r[5],
        }
        for r in rows
    ]


def assert_human_edge(
    account_id: str, src_arn: str, dst_arn: str, edge_type: str, note: str | None = None
) -> None:
    """Record a relationship a person asserted. Survives every future scan."""
    store(
        account_id,
        [Edge(src_arn, dst_arn, edge_type, source="human", note=note)],
        replace_config=False,
    )
