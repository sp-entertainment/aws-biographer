# AWS Account Agent — Design Summary

**Contest:** CockroachDB × AWS "Build with Agentic Memory" (Devpost)

This is an intent-level design. It states what to build and why decisions were made the way they were. **Exact schemas, table definitions, index syntax, API signatures, and library choices are deliberately left to the implementing agent** — make those calls yourself, guided by the principles here.

Sections marked **DECIDED** are settled. Sections marked **OPEN** need a judgment call; make one and note it. Section 12 lists ideas already rejected — do not revive them.

---

## 1. What this is

An agent that answers natural-language questions about an AWS account by investigating live resources, and that accumulates durable knowledge about that account over time.

Three claims differentiate it from a crowded field of AWS audit tools:

**Zero-enablement.** It works against an account with nothing turned on. No AWS Config, no CloudTrail trails, no Cost and Usage Report pipeline, no installed agents. It reads only what AWS already exposes by default. This is a hard constraint, not a preference — if a feature requires the user to enable something, redesign the feature.

**Persistent memory.** Every comparable tool is a stateless snapshot. This one remembers: conclusions, human annotations, account conventions, and a change history that outlives what AWS itself retains.

**Verifiable memory.** Staleness is the unsolved problem in agent memory, because facts about people can't be cheaply re-checked. In AWS, every conclusion is verifiable with an API call. Each memory carries the claim that makes it true and a cheap way to re-check it. Staleness becomes a measured, displayed property rather than a silent decay.

Pitch line: *AWS forgets after 90 days. The agent doesn't.*

---

## 2. Contest requirements

- **CockroachDB must be the persistent memory layer.** No other datastore holds memory.
- **At least two of four CockroachDB tools**, used meaningfully: Managed MCP Server, Distributed Vector Indexing, ccloud CLI, Agent Skills Repo. The plan uses MCP Server and Vector Indexing; ccloud optionally as a third.
- **At least one AWS service.** Plan uses Bedrock, Lambda, S3, CloudFront, EventBridge, IAM.
- **Public repo** with the license detected and visible in GitHub's About sidebar.
- **Functional demo URL**, live and unrestricted to judges through Sept 15, 2026.
- **Video under three minutes** showing the CockroachDB memory layer actually working. No copyrighted music.
- **New code only.** Anything pre-existing must be disclosed.

**Judging: five equally weighted criteria** — Agentic Memory Design, Implementation Quality, Real-World Impact, Product Readiness, Creativity & Originality.

Agentic Memory Design is a full fifth of the score on its own. Decisions that make memory *load-bearing* beat additional features.

---

## 3. The product — DECIDED

Conversational chat. Natural language in and out. **Every answer that references a resource must include ARNs or identifiers the user can paste into the AWS console.** This is non-negotiable — it's the difference between a demo and a tool.

**Read-only applies to AWS only.** The agent must never modify resources in the account it studies, enforced at the IAM layer. This constraint has nothing to do with CockroachDB — the agent writes to CRDB freely.

Question classes to support:

- **Current state** — "what EC2 instances do I have?"
- **Cost attribution** — "I'm spending $10 a month, on what?"
- **History** — "when did this get detached, and by whom?"
- **Waste** — "what looks abandoned?" (requires duration, not a snapshot)
- **Relationships** — "what breaks if I delete this?"
- **Convention** — "which region is prod?" (only knowable from the user)
- **Meta** — "what do you know about my account?"

Schema should support multiple accounts from the start; the demo runs one.

---

## 4. Data sources — DECIDED

Four surfaces, all available with zero setup:

- **boto3 describe/list calls** — current state, full detail.
- **Resource Groups Tagging API** — fast broad inventory, but taggable resources only.
- **CloudTrail Event History** — roughly 90 days of management events with actor identity, **on by default in every account.** This is the free past, and it's the most important unlock in the design. AWS then deletes it, which is precisely the argument for the memory layer.
- **Cost Explorer API** — spend by service, region, tag, and time. Requires a one-time opt-in and costs about a cent per request.

**Scanning strategy.** Seventeen default regions times roughly forty service APIs is thousands of calls. Don't brute-force it. Use cost data to identify regions with spend, probe candidate regions cheaply with the Tagging API in parallel, then run the full sweep only against regions that show signs of life, plus one pass for global services (IAM, S3, Route53, CloudFront, WAF). Watch two holes: free resources don't show up in cost data, and the Tagging API misses non-taggable resources — so cheap probes can't be the only gate.

**Cost attribution — OPEN.** Per-resource billing normally requires Cost Explorer resource-level granularity (opt-in, extra cost) or a CUR-to-Athena pipeline (real setup, 24-hour delay). Both violate zero-enablement. Fall back to service, region, and tag-level cost, and have the agent attribute that against its own inventory and reason about the split. "You have three t4g.nano instances in us-east-1 and nine dollars of EC2 spend there" is a useful answer without exact per-ARN billing. Verify current AWS behavior before building — this area may have changed.

---

## 5. Memory design — DECIDED

This is the heart of the project. Get it right.

### Three tiers

**Current state** — one record per resource, overwritten each scan. This is a cache, not memory. It exists so the agent doesn't rescan to answer trivial questions.

**Change log** — append-only, deltas only. One record per real change. Not snapshots of everything at every point in time — that was considered and rejected as noise that grows without bound. The change log survives because it's the *evidence trail* behind conclusions, answering "how do you know?", and because consolidated memory drifts when nothing raw sits behind it.

**Conclusions** — the actual memory. Embeddings live here.

### What earns a place in memory

The test: **would this be gone if I forgot it?**

- Cheap and re-derivable (an EIP is currently unattached) → recompute, don't store.
- Only knowable over time (unattached *since March*; not invoked in 90 days) → store.
- Supplied by the human (that untagged instance is the build runner) → highest value, always store.

For anything the agent proposes to remember on its own, the durability check is: **would this still be true and useful a month from now?** Do not substitute vague criteria like "high signal."

### How memories get captured

**Explicit requests bypass all filtering.** When the user says "remember this," it gets stored. An agent that overrules a direct instruction destroys trust, and this is the single highest-value memory class.

**Implicit capture is filtered.** The main model decides what's a candidate and phrases it as a **standalone sentence** — that step needs conversation context. Everything downstream operates on that self-contained text and can use a cheaper model.

### How memories get written

**Write immediately.** Never defer to an end-of-conversation hook — the hook won't fire when a tab closes, and what gets lost is exactly the human annotation you most wanted.

**On collision, merge inline with an LLM.** Combine old and new, or replace when the underlying state genuinely changed. Use a cheap model. **If the merge fails or times out, insert the new memory alongside the old — never lose a write.**

Two guards against drift over repeated merges: keep human-supplied text verbatim in a field that is never rewritten, and put a length ceiling on merge output.

Memories need a stable identity for collision detection — something like account plus resource plus topic, with account-level memories using an empty resource. Keep it a plain upsert on a key; don't do semantic comparison to decide whether two memories are "the same." Track whether each memory came from a human or the agent; it costs nothing and enables precedence rules later.

### The manage pass — OPEN, and important

A scheduled job that scans, diffs, and writes changes. It should be **mostly dumb** — no LLM invocation unless something actually moved. Wake the model only on a real diff.

This job is also where **verification** happens: re-run each memory's cheap check, refresh its verified timestamp, or retire it. This is the differentiating feature of the whole submission and it has never been specced. Design it. Decide what a verification check looks like in practice, how it executes, what retires versus refreshes, and how retired memories surface to the user.

### Failure modes to design against

These are documented failures in production agent memory systems and all apply here:

- **Staleness** — the verification design addresses this directly.
- **Self-reinforcing error** — an agent concludes something is broken and thereafter ignores it, while the real cause was elsewhere. Memories carrying their evidence, plus verification that can retire them, is the mitigation.
- **Contradiction oscillation** — merge logic must converge, not flip-flop.
- **Semantic versus causal mismatch** — vector similarity finds related text, not causally relevant facts.
- **Memory blindness** — the fact you needed was the eleventh result. Top-k alone is insufficient; structured filters and graph traversal both help.
- **Summarization drift** — verbatim human text and length ceilings guard this.

---

## 6. The resource graph — DECIDED

Represent relationships between AWS resources: which Lambda uses which role, which Step Function invokes which Lambda, which log group belongs to what, which security group attaches where. Plus **abstract groupings that only the user can supply** — "these eight resources are the checkout feature."

**Build it in CockroachDB, not a graph database.** An adjacency table with both directions indexed, traversed with recursive CTEs, handles this well at the scale involved (hundreds to low thousands of resources). Blast-radius and feature-membership questions are shallow — depth two or three. That outruns a network hop to a separate graph store, and it removes a service that would otherwise need to stay alive through the judging period.

"We didn't need a graph database" is a stronger architectural claim here than adding one. Graph traversal, vector similarity, full-text search, and relational filters in one store, one query, one transaction.

**Edges carry a source.** Config-derived edges are certain and get overwritten each scan. Inferred edges (naming conventions like a log group path matching a function name) are probabilistic. Human-supplied edges are certain and **must survive rescans** — never let a scan delete what the user told you.

**Vectors do not represent edges.** Similarity is not connection: two Lambdas doing similar work sit near each other in embedding space with no relationship between them. Vectors have a different and genuinely useful job here — **proposing candidate edges.** Resources that cluster tightly but share no known edge become questions the agent asks: "these four look related, are they one feature?" The user's answer writes a confirmed edge.

**Edges are facts. Vectors are hunches.** Never promote a hunch to an edge without confirmation, or you rebuild the self-reinforcing-error failure mode by hand.

This also makes the human layer structurally necessary rather than a nice extra. The graph has holes only conversation can fill, and the agent visibly needs the user to fill them. That's a demo beat.

---

## 7. Retrieval — OPEN, largest remaining gap

Four lanes, all against CockroachDB:

- **SQL** for structured filters — region, service, cost, date ranges.
- **Full-text** for exact identifiers — ARNs, instance IDs, bucket names. Embeddings are genuinely bad at identifiers; don't use them for this.
- **Vector** for fuzzy conceptual questions — "what looks abandoned?"
- **Graph traversal** from a seed node found by any of the above. This directly addresses memory blindness: you don't need the eleventh vector result if you can walk two edges from the right node.

Unresolved: how the agent picks lanes (a router versus running all lanes and merging), how results are ranked and deduplicated across lanes, and how recency is weighted. This maps onto a full judging criterion — spend real design effort here rather than defaulting to top-k vector search.

**Cache completed analyses.** The expensive operation in this product is the multi-region scan. Store finished analyses with an embedding of the question that produced them; when a rephrased version of the same question arrives, match it and offer **reuse or refresh** rather than re-running the pipeline. This is what makes memory load-bearing instead of decorative — it isn't storing state, it's storing *work*.

---

## 8. Architecture — DECIDED where noted

**CockroachDB access is split by path:**

- **The agent reads through the Managed MCP Server**, forming its own SQL to explore memory. This is far more compelling than using MCP during development only, and it satisfies the tool requirement meaningfully. Use service account API keys for autonomous (Lambda) access, not interactive OAuth.
- **The application writes through a normal Postgres driver.** Writes are deterministic code; no LLM needs to form an INSERT.

The rationale is architectural, not a safety boundary. It happens to match the MCP server's read-only default, which means less configuration.

**Components:** a chat front end; a chat backend running the agent loop against Bedrock, with a strong model for reasoning and a cheap one for merges and durability checks; a scheduled scan-and-manage job; CockroachDB holding everything.

**Deployment shape — OPEN.** Recommended: static front end on S3 and CloudFront, chat backend on Lambda, scan job on a schedule. Pick based on cold-start tolerance and library weight; a container is fine if imports are heavy.

**IAM.** A read-only role in each studied account, assumable only by the application's execution role, with an external ID. **Never publish an assumable role ARN** — judges use the hosted app, and a published ARN is an open door.

---

## 9. Memory framework — OPEN

Do not build a general-purpose memory engine from scratch. Extraction, classification, merging, decay, and retrieval ranking are the genuinely hard parts and they're solved elsewhere.

**mem0 is rejected.** No CockroachDB provider; its Postgres path uses pgvector-specific index DDL that CockroachDB won't accept. Bad bet on the one component the contest requires.

**Memori is the candidate.** Apache-2.0, self-hostable, Python SDK, with a documented CockroachDB backend and an example in its repo. Marked alpha by its own metadata.

**LangChain's CockroachDB integrations are the fallback** — official vector store and chat history providers. Building blocks rather than a memory system, but low risk.

**Gate before committing:** prove the framework connects to the cluster, creates its schema, and round-trips a memory. Confirm it supports arbitrary metadata per memory — resource identifier, region, verified timestamp — or the verification story doesn't work and it isn't worth adopting.

**What no framework will do for you.** They all extract memory from *conversation transcripts*. That's roughly a third of the need here. Scan deltas, cached analyses, edges, and suppression rules arrive from a scheduled job, not a chat. So: framework owns the conversational layer if the gate passes; your own tables own the machine layer. The second part is a few tables and an upsert, not a physics engine.

---

## 10. Demo strategy — DECIDED

Two artifacts, two accounts, deliberately.

**The video uses the real account**, redacted. Months of genuine CloudTrail, real mess, real spend. A seeded account cannot carry the impact story.

**The demo URL uses a seeded sandbox**, safe to expose publicly. State plainly in the README that it's seeded.

**Seeding.** Keep total cost under about ten dollars for the judging period. Include deliberate mess: an untagged nano instance, an unattached Elastic IP, unattached small volumes, several inconsistently named untagged buckets, security groups open to the world, never-invoked functions, a hand-built VPC, and a small Terraform stack managing *some* of it so IaC coverage has a real numerator. Avoid anything with a meaningful per-hour floor — NAT gateways and managed databases especially.

**Seed by hand through the console, across multiple sittings.** This produces genuine CloudTrail events with real identity attached. A single scripted burst yields a suspiciously flat history that undercuts the entire history story. Cost data lags about a day, so billable resources must exist well before any cost answer gets demoed.

**Hardening — OPEN.** Bedrock spend cap, rate limiting on the chat endpoint, no login for judges but no unbounded spend either. Telemetry on model calls doubles as the spend monitor and as evidence for Product Readiness.

---

## 11. Video — OPEN

Under three minutes, and it must show the CockroachDB memory layer working rather than just a chat window. Split screen — conversation on one side, live queries against CockroachDB on the other — makes the memory visible.

Suggested arc: show the mess; ask a question and get ARNs back; annotate something and watch the write land; ask a history question the change log answers; watch a memory retire itself on re-verification; ask a rephrased repeat question and get an instant cached answer.

Storyboard it rather than improvising.

---

## 12. Rejected — do not revive

**Concepts:** an SMS/phone-number agent; game-related agents (station historian, roguelike, persistent GM, economy agent); family ops, LLC compliance, institutional memory, field service, and pet health agents; a bottom-up "self-administering memory system."

**Designs:** full state snapshots per scan; AWS Config, CloudTrail trails, or CUR pipelines as data sources; a scratchpad reconciled at end of conversation; provisional/confirmed status columns; blind overwrite on collision; deferring merges to the nightly job; any filter that can overrule an explicit "remember this"; mem0 as the memory framework; Neo4j or any separate graph database; vectors as a representation of relationships.

**Competitive context.** Cost audit is crowded — AWS's own FinOps Agent, the aws-samples cost analyzer, CloudAudit, aws-mcp-audit. Terraform drift is crowded — driftctl, Firefly, StackGuardian, AlertD. **None of them have persistent memory.** That is the differentiator; lead with it in the README and the video.

---

## 13. Build order

Dependency-ordered.

1. CockroachDB schema and connection; verify vector index syntax against the live cluster before building on it.
2. Resource scan into the state cache — one region first, then the tiered logic.
3. CloudTrail backfill of the default history window. Do this early; it's the free past and gives the demo something to show.
4. Scan-over-scan diffing into the change log.
5. Edge extraction into the adjacency table, starting with the highest-value edge types.
6. Embeddings over memory bodies and cached questions.
7. MCP read path — the agent querying its own memory with SQL it wrote.
8. Chat agent with the tool loop, returning ARN-bearing answers.
9. Memory write path with inline merge and the verbatim human field.
10. Chat front end.
11. Analysis cache with reuse-or-refresh.
12. The manage pass — verification, timestamps, retirement.
13. Annotations, suppressions, and human-supplied edges and groupings.
14. Candidate-edge proposal from vector clustering.
15. Cost attribution.
16. Telemetry.
17. Hardening — spend cap, rate limiting.
18. Multi-account wiring.

Items 1 through 10 are a working product. Items 11 through 14 are what make memory load-bearing, which is where the Agentic Memory Design score lives.

---

## 14. Human tasks — for Ben, not the agent

Detailed steps are in `setup-checklist.md`.

**Accounts and access:** enable Bedrock model access (a strong model, a cheap model, and an embedding model) — this has approval latency, so start it first; register on Devpost; create the CockroachDB Cloud cluster in the same region as the AWS work; enable and sanity-test the Managed MCP Server; install and authenticate ccloud with a service account.

**Reconnaissance — values the agent needs reported back:** account ID; whether CloudTrail event history returns events and how far back (**if it's empty, the design changes**); total resource count; which regions actually hold resources; Cost Explorer status and the exact wording of the resource-level granularity option; monthly spend by service; where Terraform state lives, if anywhere.

**Project setup:** decide the entrant — personally or through Stone Pack Entertainment LLC, which affects tax paperwork and who's named Representative, and everything downstream must stay consistent; create the public repo and confirm the license shows in the About sidebar; create the read-only IAM role with an external ID; create and hand-seed the sandbox account.

**Decisions only Ben can make:** whether broad read-only access is acceptable for a public demo or a tighter custom policy is wanted; and the one question outstanding across several rounds — **Terraform drift: narrow feature, or cut entirely?** This determines whether IaC coverage ships at all.

**Submission:** create the Devpost draft early and fill it in progressively; set up the video hosting account; budget hosting through Sept 15, 2026.
