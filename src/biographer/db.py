"""CockroachDB access: connection pool and the migration runner.

Invariant 1 -- this is the only datastore in the project. The application writes
through this module with a normal Postgres driver; the agent reads through the
Managed MCP Server. That split is architectural, not a safety boundary.
"""

from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Sequence

import psycopg
from psycopg_pool import ConnectionPool

from .config import settings

log = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Process-wide pool. Opened lazily so importing this module is cheap."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings().database_url,
            min_size=1,
            # Lambda handles one request at a time; a large pool would just hold
            # idle connections against the cluster's limit.
            max_size=4,
            kwargs={"application_name": "biographer"},
        )
    return _pool


def migrate() -> list[str]:
    """Apply pending migrations in filename order. Returns those applied.

    Each file runs in its own transaction and is recorded on success, so a
    failure part-way leaves earlier migrations applied and the failing one not.
    """
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if _MIGRATION_NAME.match(p.name))
    if not files:
        raise RuntimeError(f"no migrations found in {MIGRATIONS_DIR}")

    applied: list[str] = []
    with pool().connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version STRING PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    for path in files:
        if path.name in done:
            continue
        sql = path.read_text(encoding="utf-8")
        log.info("applying migration %s", path.name)
        with pool().connection() as conn:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,)
            )
            conn.commit()
        applied.append(path.name)

    return applied


def to_vector(values: Sequence[float]) -> str:
    """Render an embedding for a VECTOR column.

    psycopg sends a Python list as a Postgres array, which CockroachDB rejects
    with 'malformed vector literal'. VECTOR wants the pgvector text form -- a
    bracketed, comma-separated list -- so embeddings cross the wire as a string.
    """
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def ensure_vector_index() -> bool:
    """Create the vector indexes if this cluster permits them. Never fatal.

    Deliberately not a migration. On CockroachDB Basic the cluster is
    multi-tenant, and `SET CLUSTER SETTING feature.vector_index.enabled` may be
    refused; a migration that fails would be recorded as unapplied and block
    every later one. The cost of not having the index is a sequential scan over
    a table holding hundreds to low thousands of rows -- which is milliseconds,
    and identical in SQL. The `<=>` queries in the retrieval path are byte for
    byte the same either way, so nothing downstream branches on this.

    Returns True if the indexes exist afterwards.
    """
    statements = (
        "CREATE VECTOR INDEX IF NOT EXISTS memories_embedding_idx"
        " ON memories (account_id, embedding vector_cosine_ops)",
        "CREATE VECTOR INDEX IF NOT EXISTS analyses_question_idx"
        " ON analyses (account_id, question_embedding vector_cosine_ops)",
    )
    with pool().connection() as conn:
        conn.autocommit = True  # SET CLUSTER SETTING cannot run in a transaction
        try:
            conn.execute("SET CLUSTER SETTING feature.vector_index.enabled = true")
        except psycopg.Error as exc:
            # Expected on Basic if the setting is system-scoped. The index may
            # still be creatable if the feature is on by default, so carry on.
            log.info("could not set feature.vector_index.enabled: %s", exc)
        for statement in statements:
            try:
                conn.execute(statement)
            except psycopg.Error as exc:
                log.warning("vector index unavailable, falling back to scan: %s", exc)
                return False
    return True


def healthcheck() -> dict[str, object]:
    """Phase 1 acceptance test: write a row, read it back, prove vectors work.

    Returns a dict of findings rather than raising, because the point of this is
    to report what the live cluster actually does -- particularly whether vector
    indexing is available, which the docs cannot answer for a given cluster tier.
    """
    out: dict[str, object] = {}
    with pool().connection() as conn:
        out["version"] = conn.execute("SELECT version()").fetchone()[0]

        conn.execute(
            "UPSERT INTO accounts (account_id, alias, is_sandbox) VALUES (%s, %s, %s)",
            ("000000000000", "healthcheck", True),
        )
        row = conn.execute(
            "SELECT alias FROM accounts WHERE account_id = %s", ("000000000000",)
        ).fetchone()
        out["round_trip"] = row is not None and row[0] == "healthcheck"

        dim = settings().embed_dim
        # Scoped to a savepoint: a failing probe must not abort the surrounding
        # transaction, or every check after it reports a false negative.
        try:
            with conn.transaction():
                conn.execute(
                    "UPSERT INTO memories (account_id, resource_key, topic, body,"
                    " origin, embedding) VALUES (%s, %s, %s, %s, %s, %s)",
                    ("000000000000", "", "healthcheck", "probe", "agent", to_vector([0.0] * dim)),
                )
                nearest = conn.execute(
                    "SELECT topic FROM memories WHERE account_id = %s"
                    " ORDER BY embedding <=> %s LIMIT 1",
                    ("000000000000", to_vector([0.0] * dim)),
                ).fetchone()
                out["vector_query"] = nearest is not None
        except psycopg.Error as exc:
            out["vector_query"] = False
            out["vector_error"] = str(exc).strip().splitlines()[0]

        idx = conn.execute(
            "SELECT index_name FROM [SHOW INDEXES FROM memories]"
            " WHERE index_name = 'memories_embedding_idx'"
        ).fetchall()
        out["vector_index_present"] = bool(idx)

        conn.execute("DELETE FROM memories WHERE account_id = %s", ("000000000000",))
        conn.execute("DELETE FROM accounts WHERE account_id = %s", ("000000000000",))
        conn.commit()

    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        print("applied:", migrate() or "nothing pending")
        print("vector_index_created:", ensure_vector_index())
        for key, value in healthcheck().items():
            print(f"{key}: {value}")
    finally:
        # Explicit, so pool worker threads are joined before interpreter
        # shutdown rather than in __del__, where joining raises.
        pool().close()
