# ADR-0005 — Tiered scanning and collector coverage

**Status:** Accepted
**Date:** 2026-08-16

## Context

Seventeen enabled regions times roughly forty service APIs is thousands of
calls per scan. Design summary §4 forbids brute-forcing it and names two holes
in the cheap alternatives: free resources never appear in cost data, and the
Tagging API only sees taggable resources.

## Decision

### Three signals, unioned

A region earns a full sweep if **any** of three signals fires:

1. **Cost** — one Cost Explorer call grouped by `REGION`, covering all regions at
   once because the API bills per request. Catches billable resources including
   untagged ones. `NoRegion` is Cost Explorer's bucket for global and
   unattributed charges and is explicitly excluded; it is not a region.
2. **Tags** — a single Tagging API page per region. Catches tagged resources
   including free ones.
3. **Canaries** — three cheap calls chosen specifically to catch what the other
   two miss, which is the free *and* untagged case: a non-default security
   group, a non-default VPC, or any log group. Somebody built something here by
   hand.

Union, never intersection. A false positive costs one wasted region sweep. A
false negative loses resources silently and reports a count that looks
plausible and is wrong — which is the worst failure an inventory tool has.

The home region is always swept regardless of signal. It is where a
mostly-empty account puts its first resource.

Measured on the real account: **1 of 17 regions swept.**

### Collectors are best-effort by contract

A collector that raises is recorded in `scans.stats.failures` and stepped over.
A scan that aborted on the first `AccessDenied` would be useless against exactly
the accounts this product exists to study — invariant 2 says nothing is enabled,
and invariant 3 says the role is deliberately limited.

Twenty-four collectors across sixteen services. Coverage is a long tail and
completeness is explicitly not the goal.

### Reconciliation makes the gap visible

Unknown coverage is a different problem from incomplete coverage. After each
scan, every ARN the Tagging API returns that no collector produced is counted
and stored in `scans.stats.coverage_gaps`. On the real account this immediately
named three: `lightsail:StaticIp`, `lightsail:KeyPair`,
`payments:payment-instrument`.

Deliberately one-directional. Resources we found that the Tagging API missed are
the expected case, not a gap — untagged and untaggable resources are most of
what this product exists to surface.

## Consequences

`first_seen` is excluded from the upsert's update clause and verified by test to
survive rescans. It is the cheapest source of duration in the system, and
duration is what separates a storable memory ("unattached since March") from a
recomputable fact ("unattached").

Deletions are detected but **not** applied here. `persist()` returns the
disappeared ARNs; Phase 4 writes their change-log entries before anything is
removed, because deleting first would discard the evidence of the deletion.

The three named coverage gaps are left open. Lightsail static IPs and key pairs
are cheap to add when the seeded demo account needs them; the payments
instrument is account metadata, not a resource, and should stay uncollected.
