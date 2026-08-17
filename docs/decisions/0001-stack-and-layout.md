# ADR-0001 — Language, stack, and module layout

**Status:** Accepted
**Date:** 2026-08-16

## Context

The handoff assigns all technical detail to the implementing agent. The project
has roughly 46 hours until the submission deadline, which makes "fewest moving
parts that satisfies the constraints" the governing criterion rather than
"best long-term architecture".

## Decision

**Python 3.13.** boto3 is the only sane way to talk to forty AWS service APIs.
Both memory-framework candidates (Memori, LangChain CockroachDB integrations)
are Python. Local dev is on 3.14; 3.13 is pinned because that is the newest
managed Lambda runtime.

**psycopg 3** for the application write path. CockroachDB speaks the Postgres
wire protocol; psycopg 3 gives native connection pooling and does not drag in
an ORM. No SQLAlchemy, no ORM at all — the schema is small enough that hand
written SQL is shorter than the mapping layer would be.

**Plain numbered `.sql` files plus a ~40 line runner** for migrations. Alembic
exists to generate diffs from ORM models; there are no ORM models. A
`schema_migrations` table and a loop over sorted filenames covers it.

**Environment variables for config,** read once into a frozen dataclass. No
pydantic-settings. Secrets come from the environment locally (`.env`, git
ignored) and from Secrets Manager references at deploy time, resolved at
runtime so they never enter application logs or agent context.

## Module layout

```
src/biographer/
  config.py        environment into a frozen dataclass
  db.py            psycopg pool, migration runner
  scan/            AWS inventory: region probing, service collectors, diffing
  memory/          the three tiers, write path, merge, verification
  agent/           Bedrock loop, tools, retrieval lanes
migrations/        NNN_name.sql, applied in order
web/               static chat front end
```

## Consequences

No dependency injection framework, no service container, no repository pattern.
Modules import each other directly. If this project outlives the contest, the
first thing to add is a real migration tool; everything else here scales fine.
