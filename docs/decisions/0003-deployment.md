# ADR-0003 — Deployment shape

**Status:** Accepted
**Date:** 2026-08-16

## Context

Design summary §8 leaves deployment open and recommends static front end on S3
and CloudFront, chat backend on Lambda, scan job on a schedule. The submission
needs a demo URL that is live, free, unauthenticated, and working through
Sept 15, 2026.

## Decision

**One Lambda, container image, with a Function URL.** It serves both the static
chat page and the chat API. Imports are heavy — boto3 plus psycopg plus the
Bedrock client — which is exactly the case where the design summary says a
container is fine.

**No API Gateway and no CloudFront.** A Function URL is already public HTTPS
with a stable hostname, which is the entire requirement. API Gateway would add a
second service to keep alive through judging for no capability this product
uses. CloudFront would add cache invalidation to a page that is a few kilobytes.

**EventBridge Scheduler** invoking a second Lambda for the scan-and-manage pass.
Scheduler rather than an EventBridge rule because it does one-shot and cron in
one API and does not need a rule-plus-target pair.

**IAM.** The Lambda execution role assumes a read-only role in the studied
account with an external ID. That role's name and ARN are deliberately absent
from this repository. Per invariant 3 the read-only boundary is
in IAM, not in code, and per invariant 9 that role ARN never appears in the
repository, the README, the video, or the Devpost entry. The seeded demo account
gets the same role. Per the answered question, both use AWS-managed
`ReadOnlyAccess` and `SecurityAudit`.

**Deployed with CDK** (Python, matching ADR-0001), per the project's AWS
guidance preferring infrastructure-as-code over CLI commands.

## Rate limiting and spend cap

Function URLs have no built-in throttling, which is the one real cost of
dropping API Gateway. Phase 14 covers it with reserved concurrency on the
Lambda plus a per-IP counter in CockroachDB, and a hard token ceiling per
request enforced in the Bedrock call. Judges get no login; the spend gets a
ceiling instead.

## Consequences

Two Lambdas, one scheduler, one table's worth of rate-limit state, and a role
per studied account. If traffic ever justified a CDN or a WAF, both slot in
front of the Function URL without touching application code.
