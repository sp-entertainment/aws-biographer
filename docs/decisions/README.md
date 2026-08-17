# Decision record

Every non-obvious call made while building this, and why. Individual ADRs hold
the detail; this page is the index and the summary of decisions that did not
warrant an ADR of their own.

| ADR | Decision |
|---|---|
| [0001](0001-stack-and-layout.md) | Python 3.13, psycopg 3, no ORM, numbered SQL migrations, module layout |
| [0002](0002-schema.md) | Nine tables, three memory tiers, collision key, why no full-text search |
| [0003](0003-deployment.md) | One Lambda + Function URL, no API Gateway, no CloudFront, CDK |
| [0004](0004-cockroachdb-basic.md) | Building on the free tier; vector indexing as a runtime capability |
| [0005](0005-scan-strategy.md) | Three-signal region tiering, best-effort collectors, coverage reconciliation |
| [0006](0006-cloudtrail-backfill.md) | Write-events-only backfill, two-stage ARN resolution |
| [0007](0007-retrieval.md) | Four lanes, deterministic routing, reciprocal rank fusion |
| [0008](0008-verification.md) | Declarative claims, three verdicts, retire-never-delete |

## Decisions that did not need their own ADR

**The application's identity is separate from the studied account's.** Invoking
Bedrock, writing to CockroachDB, and reading Secrets Manager are things the
application does for itself. Routing them through the read-only role either
fails outright or, far worse, quietly grants that role powers invariant 3 says
it must not have. `aws.app_session()` and `aws.session_for()` are different
functions for that reason.

**Verification reads the cache, never AWS.** The scan that just ran already paid
for every API call. This is what makes re-checking every memory on every pass
affordable, and therefore what makes verification continuous.

**Findings bypass the durability filter.** Not an exemption — they satisfy it by
construction, being phrased with a duration from `first_seen` and shipping a
machine-checkable claim. Paying a model call to ask "is this durable?" about a
fact built to be durable is pure cost.

**Both model-dependent checks fail open.** A durability check that errors keeps
the memory; a merge that fails inserts alongside. A false positive costs one
row, a false negative silently loses knowledge.

**TLS is never downgraded to fix a path problem.** `sslmode=verify-full` failed
in Lambda because libpq looked for a certificate file that does not exist there.
The fix points `sslrootcert` at botocore's CA bundle. Lowering `sslmode` would
have been one character and traded real security for convenience.

**Suppression matches on data, not on a naming convention.** It originally
matched memories by topic prefix, and the convention had already drifted —
finding type `no-log-retention` produces topics beginning `no-retention-`, so it
silently matched nothing. The finding type now travels inside the claim.

**`first_seen` is excluded from the scan's upsert.** It is the cheapest source of
duration in the system, and duration is what separates a storable memory from a
recomputable fact.

## Things that were tried and rejected during the build

**`sslrootcert=system`** — fixed Lambda, broke Windows local development. One
code path that works everywhere beat two that each work somewhere.

**Reserved Lambda concurrency** — rejected by AWS on an account whose total
concurrency limit is 10. The account limit is itself the fan-out ceiling.

**A model call to route retrieval lanes** — the signals are regex-cheap and
unambiguous. Slower, costlier, less predictable, for no gain.

**An executable verification check** — arbitrary model-authored code with a
database behind it, for a feature whose entire value is being trustworthy.

**Provider `default_tags` on the seed stacks** — would have stamped a tag on
every resource including the ones that exist specifically to be untagged,
silently killing the untagged-resource finding.

## Deliberate simplifications, with their ceilings

- **Rate limiting is an in-process counter.** Correct for one Lambda; across N
  instances it is really a limit of N times the configured value. Move it to a
  CockroachDB table if the demo ever scales out.
- **The graph lane expands one hop.** Deeper walks from an unranked seed set
  produce noise faster than answers. `blast_radius` handles real depth questions.
- **Collector coverage is a long tail** and completeness is not the goal.
  Reconciliation names what is missing rather than pretending it is complete.
- **Claim coverage is partial.** Human annotations mostly have nothing
  machine-checkable about them and stay unverifiable forever. Inventing claims
  for them would produce confident retirements with no evidence behind them.
