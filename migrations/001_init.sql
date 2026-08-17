-- 001_init.sql — core schema.
-- See docs/decisions/0002-schema.md for the reasoning behind these shapes.

CREATE TABLE IF NOT EXISTS accounts (
    account_id  STRING PRIMARY KEY,
    alias       STRING,
    is_sandbox  BOOL NOT NULL DEFAULT false,
    role_arn    STRING,          -- assumable read-only role; never published
    external_id STRING,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Scan bookkeeping. One row per scan run, so a diff knows what its predecessor
-- actually covered -- a region missing from a scan is not the same as a region
-- that came back empty.
CREATE TABLE IF NOT EXISTS scans (
    scan_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  STRING NOT NULL REFERENCES accounts(account_id),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    regions     JSONB NOT NULL DEFAULT '[]',   -- regions actually swept
    stats       JSONB NOT NULL DEFAULT '{}',
    error       STRING,
    INDEX scans_by_account (account_id, started_at DESC)
);

-- Tier 1: current state. A cache, not memory. Overwritten each scan.
CREATE TABLE IF NOT EXISTS resources (
    account_id    STRING NOT NULL REFERENCES accounts(account_id),
    arn           STRING NOT NULL,
    region        STRING NOT NULL,
    service       STRING NOT NULL,
    resource_type STRING NOT NULL,
    name          STRING,
    tags          JSONB NOT NULL DEFAULT '{}',
    config        JSONB NOT NULL DEFAULT '{}',  -- normalised describe output
    -- first_seen survives overwrites: the cheapest source of duration, which is
    -- what makes "unattached since March" storable and "unattached" not.
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_scan_id  UUID,
    PRIMARY KEY (account_id, arn),
    INDEX resources_by_region  (account_id, region, service),
    INDEX resources_by_service (account_id, service, resource_type),
    INVERTED INDEX resources_by_tag (tags)
);

-- Tier 2: change log. Append-only, deltas only. Never snapshots.
CREATE TABLE IF NOT EXISTS changes (
    change_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  STRING NOT NULL REFERENCES accounts(account_id),
    arn         STRING,           -- null when CloudTrail names no resource
    region      STRING,
    change_type STRING NOT NULL,  -- created | deleted | modified | api_call
    field       STRING,           -- which attribute moved, for 'modified'
    old_value   JSONB,
    new_value   JSONB,
    actor       STRING,           -- CloudTrail identity, when available
    source      STRING NOT NULL,  -- cloudtrail | scan
    event_name  STRING,
    event_time  TIMESTAMPTZ NOT NULL,
    -- Unique where present, so re-running the CloudTrail backfill is idempotent.
    event_id    STRING,
    raw         JSONB,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX changes_by_arn     (account_id, arn, event_time DESC),
    INDEX changes_by_time    (account_id, event_time DESC),
    UNIQUE INDEX changes_by_event (account_id, event_id) WHERE event_id IS NOT NULL
);

-- The resource graph. Adjacency, both directions indexed, walked with
-- recursive CTEs. Explicitly not a graph database (design summary section 6).
CREATE TABLE IF NOT EXISTS edges (
    account_id  STRING NOT NULL REFERENCES accounts(account_id),
    src_arn     STRING NOT NULL,
    dst_arn     STRING NOT NULL,
    edge_type   STRING NOT NULL,   -- uses_role | invokes | logs_to | member_of ...
    -- config edges are rewritten each scan; human edges are never touched by a
    -- scan (invariant 7); inferred edges are unconfirmed proposals (invariant 8).
    source      STRING NOT NULL,   -- config | inferred | human
    confidence  FLOAT,             -- only meaningful for 'inferred'
    note        STRING,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, src_arn, dst_arn, edge_type),
    INDEX edges_reverse (account_id, dst_arn, edge_type),
    INDEX edges_by_source (account_id, source)
);

-- Tier 3: memory. The actual memory. Embeddings live here.
CREATE TABLE IF NOT EXISTS memories (
    memory_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   STRING NOT NULL REFERENCES accounts(account_id),
    -- Collision key. Account-level memories use '' rather than NULL, because
    -- NULL does not collide with NULL in a unique index and the upsert would
    -- silently become an insert.
    resource_key STRING NOT NULL DEFAULT '',
    topic        STRING NOT NULL,
    body         STRING NOT NULL,
    -- Never rewritten by a merge. Guards summarisation drift.
    human_text   STRING,
    origin       STRING NOT NULL,        -- human | agent
    -- The claim that makes this memory true, plus the cheap way to re-check it.
    -- Shape is settled in Phase 11, which owns verification.
    claim        JSONB,
    verified_at  TIMESTAMPTZ,
    retired_at   TIMESTAMPTZ,            -- retirement is a state change, not a delete
    retire_reason STRING,
    embedding    VECTOR(1024),           -- Titan Text Embeddings V2, dimension measured
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE INDEX memories_key (account_id, resource_key, topic),
    INDEX memories_live (account_id, retired_at) WHERE retired_at IS NULL,
    INDEX memories_stale (account_id, verified_at)
);

-- Cached work, not cached state. A rephrased repeat question matches the
-- embedding and gets offered reuse-or-refresh instead of re-running the scan.
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id         STRING NOT NULL REFERENCES accounts(account_id),
    question           STRING NOT NULL,
    question_embedding VECTOR(1024),
    answer             STRING NOT NULL,
    inputs             JSONB NOT NULL DEFAULT '{}',  -- what it was computed from
    cost_usd           DECIMAL(12, 6),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    refreshed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX analyses_recent (account_id, refreshed_at DESC)
);

-- Human-supplied "stop telling me about this". Must survive rescans.
CREATE TABLE IF NOT EXISTS suppressions (
    suppression_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     STRING NOT NULL REFERENCES accounts(account_id),
    arn            STRING,          -- null means the pattern applies account-wide
    finding_type   STRING NOT NULL,
    reason         STRING,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX suppressions_lookup (account_id, finding_type, arn)
);

-- Model-call telemetry. Doubles as the spend monitor and as Product Readiness
-- evidence.
CREATE TABLE IF NOT EXISTS telemetry (
    call_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    STRING,
    model_id      STRING NOT NULL,
    purpose       STRING NOT NULL,   -- chat | merge | durability | embed | verify
    input_tokens  INT,
    output_tokens INT,
    latency_ms    INT,
    cost_usd      DECIMAL(12, 6),
    error         STRING,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX telemetry_by_time (created_at DESC)
);
