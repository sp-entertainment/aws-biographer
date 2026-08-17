# ADR-0006 — CloudTrail backfill

**Status:** Accepted
**Date:** 2026-08-16

## Context

CloudTrail Event History is on by default in every AWS account, retains roughly
ninety days of management events with actor identity, costs nothing, and then
AWS deletes it. Design summary §4 calls it the most important unlock in the
design, and its deletion is the entire argument for the memory layer.

Invariant 2 is why this reads Event History via `lookup_events` rather than
creating a trail. A trail is something the user would have to turn on.

## Decision

**Write events only.** `lookup_events` is filtered on `ReadOnly=false`.
Read-only events are the overwhelming majority of CloudTrail volume and none of
them changed anything; storing them would bury real history under console
polling noise. Measured on the real account: 25 write events across ninety days,
against thousands of reads.

**Idempotent by construction.** `changes.event_id` is uniquely indexed per
account, so re-running the backfill inserts nothing rather than duplicating the
past. The index is partial (`WHERE event_id IS NOT NULL`), which means the
`ON CONFLICT` clause must repeat that predicate — without it Postgres cannot
match the target to a constraint and the insert fails outright.

**Two-stage ARN resolution.** CloudTrail's `Resources` array is authoritative
but frequently empty. When it is, identifiers are extracted from the raw record
with a pattern anchored on AWS id prefixes (`i-`, `vol-`, `sg-`, `arn:aws…`) and
matched against ARNs already in the inventory cache.

Only identifiers already known are accepted. An unknown id is not evidence, and
synthesising an ARN from one would breach invariant 4 — the identifiers in an
answer must be real enough to paste into the console.

**`requestParameters` is retained in `raw`.** Several services name their target
nowhere else. Lightsail's `DeleteInstance` carries an empty `Resources` array
and the instance name in the request body; without keeping it, the record of a
deletion loses the identity of what was deleted — which is precisely the
information the change log exists to preserve.

## Measured on the real account

25 write events, oldest 2026-08-16. **6 resolved to an ARN, 19 not.**

The unresolved 19 are not a resolution failure and the number should not be
optimised. They break down as service-plane events with no AWS resource at all
(Bedrock model agreements, marketplace agreements, CloudShell sessions) and
Lightsail operations on resources that no longer exist. Once the seeded stacks
apply, EC2, S3, Lambda and IAM events will carry proper identifiers and resolve.

The account has no meaningful past — every event dates from today. This was
reported before Phase 1 and is why the account is being seeded.

## Consequences

The backfill is a one-shot rescue of whatever window exists the first time the
agent runs. Thereafter the manage pass (Phase 11) keeps the log current, and the
90-day cliff stops mattering, because the change log outlives what AWS retains.

`benbot`, a Lightsail instance deleted before this tool existed, is now
permanently recorded with its actor and timestamp while being entirely absent
from AWS. That is the product's claim demonstrated on real data.
