# ADR-0007 — Four-lane retrieval, deterministic routing, RRF fusion

**Status:** Accepted
**Date:** 2026-08-16

## Context

Design summary §7 names this the largest remaining gap and leaves three things
open: how the agent picks lanes, how results are ranked and deduplicated across
lanes, and how recency is weighted. It explicitly forbids defaulting to top-k
vector search. This maps onto a full judging criterion.

## Decision 1 — Routing is deterministic, not a model call

The signals that decide which lane applies are regex-cheap and unambiguous: does
the question contain an ARN or an AWS id, a region name, a service name, or
waste vocabulary. A model call to decide what a regex already knows would be
slower, cost money on every question, and be less predictable. The model reasons
about results; it does not route.

The identifier pattern is anchored on real AWS service prefixes rather than a
loose `\w+-\w+`, because an unanchored pattern fires on ordinary prose —
"in-flight requests" would route to the identifier lane and return nothing.

The vector lane always runs. It is one embedding call and it is the only lane
that can answer a question phrased in words the schema does not contain.
Skipping it to save a fraction of a cent is how retrieval gets brittle.

## Decision 2 — Fusion is Reciprocal Rank Fusion

Lane scores are not comparable. Cosine distance is a float in one range, a SQL
predicate match is a boolean, a graph result is an integer hop count.
Normalising them against one another needs tuning constants that nothing in this
data justifies.

RRF needs only *ranks*: `score += weight / (k + rank)`, with `k = 60` from the
original paper. It fuses incomparable lanes without inventing a shared scale, in
about fifteen lines. A result appearing in several lanes accumulates
contributions from each, so cross-lane agreement is itself evidence.

Three adjustments apply after fusion, in descending order of magnitude:

- **Lane weights.** Identifier 3.0, structured 2.0, graph 1.5, vector 1.0. An
  exact identifier match means the user named the thing; embeddings are
  genuinely bad at identifiers and must never outrank one.
- **Provenance.** Human-supplied memory gets ×1.15. Deliberately small —
  provenance breaks ties, it does not rewrite ranking.
- **Recency.** An unverified memory gets ×0.9, which is how staleness enters
  ranking rather than being a separate mechanism.

None of these can promote a result no lane returned.

## Decision 3 — The graph lane answers memory blindness

Design summary §5 lists memory blindness as a documented failure mode: the fact
you needed was the eleventh result. The answer is not a better embedding. The
graph lane expands one hop from seeds the identifier and structured lanes
already found, so a fact two edges from the right node is reachable regardless
of where it sits in a similarity ranking.

Depth is one. Deeper walks from an unranked seed set produce plausible-looking
noise faster than answers; `graph.blast_radius` exists for when the user
actually asked a depth question.

## Two bugs this design surfaced, both fixed

**Global resources were invisible to region queries.** S3, IAM, CloudFront, and
Route53 are stored with `region = 'global'`, so "untagged s3 buckets in
us-east-1" filtered every bucket out and returned nothing. A named region now
also admits global resources, because a user asking about a region means them
too.

**Waste has degrees, and treating it as one predicate buried the findings.** A
bare `tags = '{}'` OR-clause matched every AWS-managed default — service-linked
roles, default subnets, the default security group — and those outranked the
genuinely orphaned resources. Waste is now ranked by specificity: an unattached
Elastic IP or detached volume (billing hourly, definitely a finding) above a
stopped instance, above a log group with no retention, above merely untagged.
AWS-created defaults are excluded outright; nobody abandoned them.

## Verified

- Exact identifier question retrieves by identifier, not similarity: the EIP's
  memory and the resource itself rank 1 and 2, both via the identifier lane.
- "What looks abandoned?" returns the two orphaned volumes and the unattached
  Elastic IP first.
- "Untagged s3 buckets in us-east-1" returns all five seeded buckets.
- "Which machine builds our software?" — no identifier, no service, no region —
  routes to vector alone and returns the human annotation about the build runner.

## Consequences

Routing is inspectable: `Plan.why` states which lanes ran and on what evidence,
which makes a wrong answer debuggable rather than mysterious. The cost is that
routing cannot handle a question whose intent is implied rather than stated —
if that shows up in practice, a cheap-model classifier slots in behind the same
`route()` interface without touching fusion.
