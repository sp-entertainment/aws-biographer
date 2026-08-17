# ADR-0002 — Schema

**Status:** Accepted
**Date:** 2026-08-16

## Context

Invariant 1 puts everything in CockroachDB: current state, change history, the
resource graph, memories, cached work, and telemetry. Design summary §5 fixes
the three memory tiers; §6 fixes the graph as an adjacency table traversed with
recursive CTEs. This ADR records the concrete tables and the reasoning behind
the parts that were not dictated.

## Decision

Every table carries `account_id` as the leading column. The demo runs one
account; multi-account is a `WHERE` clause rather than a migration later.

### Tier 1 — cache

`resources` — one row per live resource, keyed `(account_id, arn)`, overwritten
each scan. `first_seen` is preserved across overwrites; it is the cheapest
possible source of duration, and duration is what separates "this EIP is
unattached" (recompute) from "this EIP has been unattached since March" (store).

### Tier 2 — evidence

`changes` — append only, deltas only, never snapshots. Two sources feed it and
both are recorded in `source`: `cloudtrail` for backfilled history with real
actor identity, `scan` for scan-over-scan diffs. `event_id` is unique where
present so a re-run of the CloudTrail backfill cannot double-insert.

### Tier 3 — memory

`memories` — the collision key is `UNIQUE (account_id, resource_key, topic)`
where account-level memories use an empty string rather than NULL, because NULL
does not collide with NULL in a unique index and that would silently defeat the
upsert. Per design §5 the upsert is a plain key match; no semantic comparison
decides whether two memories are "the same".

Three columns exist specifically to serve invariants:

- `human_text` holds human-supplied wording verbatim and is never rewritten by
  a merge (guards summarization drift, invariant 7).
- `origin` is `human` or `agent` (enables the precedence rule behind invariant 5).
- `claim` is JSONB holding the verification spec — the boto3 call, its
  arguments, and the expected result that makes the memory true. This is what
  Phase 11 executes. Its shape is deliberately not frozen here; Phase 11 is
  real design work and gets its own ADR.

`retired_at` and `retire_reason` are nullable. Retirement is a state change, not
a delete — a retired memory is still evidence, and deleting it would lose the
fact that the agent once believed it.

### Graph

`edges` — `(account_id, src_arn, dst_arn, edge_type)` unique, indexed in both
directions so traversal is symmetric. `source` is `config`, `inferred`, or
`human`. Scans delete and rewrite only `source = 'config'` rows; `human` rows
are untouchable by any scan (invariant 7), and `inferred` rows are proposals
that have not yet been confirmed (invariant 8).

### Work reuse

`analyses` — a finished analysis plus a `VECTOR(1024)` embedding of the question
that produced it. This is the table that makes memory load-bearing rather than
decorative: it stores work, not state.

### Supporting

`accounts`, `scans`, `suppressions`, `telemetry`.

## Vectors

`VECTOR(1024)`, matching Titan Text Embeddings V2, a dimension measured from the
model rather than taken from documentation. Cosine distance (`<=>`) with
`vector_cosine_ops`, on `memories.embedding` and `analyses.question_embedding`.

Both vector indexes carry `account_id` as a prefix column. CockroachDB supports
prefix columns on vector indexes to pre-filter the search space, which turns
multi-account isolation into an index property instead of a post-filter that
would silently shrink the effective k.

The indexes are created by `db.ensure_vector_index()` rather than by a
migration, because on CockroachDB Basic they may not be creatable at all. See
ADR-0004. The `VECTOR(1024)` columns and every `<=>` query are unaffected either
way.

## Full-text

Deliberately not implemented as full-text search. Design §7 asks for a lane that
retrieves exact identifiers — ARNs, instance IDs, bucket names. For exact
identifiers, exact and prefix matching against a btree index on `arn` is both
more correct and less code than a tsvector pipeline; tokenizers mangle
identifiers. A GIN inverted index on `tags` covers tag lookup. If Phase 9
demonstrates a case these two miss, that is when tsvector earns its place.

## Consequences

Nine tables. No partitioning, no TTL policies, no archival — at hundreds to low
thousands of resources none of it pays for itself. The change log is the only
table that grows without bound and it grows at the rate the account actually
changes, which the reconnaissance measured at fourteen events in ninety days.
