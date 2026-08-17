# ADR-0004 — Building on CockroachDB Basic

**Status:** Accepted
**Date:** 2026-08-16

## Context

The cluster is CockroachDB Basic. Standard was ruled out on cost. Basic is
multi-tenant and free-tier, which constrains three things the design touched:
cluster settings, connection count, and cold starts.

The sharp edge is vector indexing. `CREATE VECTOR INDEX` is documented as
requiring `SET CLUSTER SETTING feature.vector_index.enabled = true`, and on a
multi-tenant cluster a SQL user may not be permitted to change cluster settings
whose scope is the system virtual cluster rather than the application. Whether
`feature.vector_index.enabled` is application-scoped could not be confirmed from
documentation and is not knowable without the live cluster.

## Outcome, verified 2026-08-16

**Vector indexing works on Basic.** Against the live cluster (CockroachDB CCL
v26.2.5), `SET CLUSTER SETTING feature.vector_index.enabled = true` was accepted
and both `CREATE VECTOR INDEX` statements succeeded. `SHOW INDEXES` confirms
`memories_embedding_idx` present, and a cosine query returns. The contest's
Distributed Vector Indexing requirement is satisfied on the free tier.

The defensive structure below is kept anyway. It cost one function, it is what
made the failure legible rather than fatal while this was unknown, and it means
a future cluster that refuses the setting degrades instead of breaking.

One real incompatibility did surface: psycopg sends a Python list as a Postgres
array, which CockroachDB rejects with `malformed vector literal`. Embeddings must
cross the wire as pgvector text (`[1.0,2.5]`). That is `db.to_vector()`, and
every write to a `VECTOR` column goes through it.

## Decision

**Do not make the schema depend on the vector index existing.**

The index was a migration (`002_vector_indexes.sql`); it is now
`db.ensure_vector_index()`, called after `migrate()`, which attempts the setting
and the DDL, logs whatever it is refused, and returns a boolean. It is never
fatal.

This costs almost nothing, because **an unindexed cosine query is the same SQL
as an indexed one.** `ORDER BY embedding <=> $1 LIMIT k` runs either way; without
the index it is a sequential scan. At the scale this product operates on --
design summary §6 puts it at hundreds to low thousands of resources, and the
memory and analysis tables are smaller than the resource table -- an exact scan
over a few thousand 1024-dimension vectors is milliseconds. No query in the
retrieval path branches on whether the index exists.

There is a real trade here and it is worth naming: without the index, the search
is exact rather than approximate. That is a *better* result, more slowly. The
crossover where it stops being acceptable is far above anything this account
will produce.

**Insurance for the contest tool requirement.** The submission needs at least two
of CockroachDB's four tools used meaningfully, and the plan named Managed MCP
Server plus Distributed Vector Indexing. If the index turns out to be
uncreatable on Basic, the second tool becomes the **ccloud CLI** (setup
checklist A5, previously optional), used for cluster and service-account
provisioning. That keeps the requirement satisfied without inventing work. The
MCP Server is unaffected by any of this and remains the primary.

## Other Basic constraints accepted

**Connections.** Pool stays at `max_size=4`. A Lambda handles one request at a
time; a larger pool would only hold idle connections against the tenant limit.

**Scale to zero.** Basic suspends an idle cluster, so the first query after a
quiet period pays a resume cost. This lands on judges hitting a cold demo. Phase
14 covers it with the same EventBridge schedule that runs the manage pass --
that job touches the database regularly enough to keep it warm, at no extra
infrastructure.

**Storage and request units.** The free allowance is far above what a few
thousand resources, their change log, and their embeddings consume. Not a
constraint in practice; the change log is the only unbounded table and the
account produced fourteen write events in ninety days.

## Consequences

Nothing was cut. The single structural change is that vector indexing became a
runtime capability rather than a schema assumption, which is where it belonged
anyway given that no documentation can tell you what a specific cluster permits.
`ensure_vector_index()` returning `False` is a logged warning and a slower scan,
not a broken product.
