# Implementation Handoff — AWS Account Agent

**You are the implementing agent for this project.** Read `design-summary.md` first — it holds the intent, the reasoning, and the rejected paths. This document tells you how to execute: what order to build in, what to verify before you build, what you decide yourself, and what you must stop and ask about.

---

## 0. Mission

Build an agent that answers natural-language questions about an AWS account by investigating live resources, and that accumulates durable knowledge about that account over time. It is a submission to the CockroachDB × AWS "Build with Agentic Memory" contest.

**You own all technical detail.** Schemas, table definitions, index syntax, library selection, module layout, error handling, framework choices — make those calls. The design document deliberately does not specify them. Where it does state a constraint, that constraint is load-bearing and reversing it breaks the project.

---

## 1. Invariants — do not violate these

These are the decisions that break the submission if reversed. If a design choice you're making conflicts with one of these, the choice is wrong.

1. **CockroachDB is the only memory store.** No second datastore. No Neo4j, no Redis, no Postgres, no local SQLite for "just this one thing." If you feel pressure to add one, that's a signal the design needs to change, not the storage.

2. **Zero-enablement.** The agent must work against an AWS account with nothing turned on — no AWS Config, no CloudTrail trails, no CUR pipeline, no installed agents. If a feature requires the user to enable something, redesign the feature or drop it.

3. **Read-only against AWS.** The agent must never create, modify, or delete an AWS resource in the account it studies. Enforce it at the IAM layer, not by convention in code. This says nothing about CockroachDB — write there freely.

4. **Every answer referencing a resource includes its ARN or console-lookupable identifier.** No exceptions. This is the difference between a demo and a tool.

5. **An explicit "remember this" is never filtered, judged, or overruled.** It gets stored. Filtering applies only to memories the agent proposes on its own.

6. **Never lose a write.** If a memory merge fails or times out, insert the new memory alongside the old rather than dropping it.

7. **Human-supplied knowledge survives rescans.** Annotations, groupings, and human-asserted edges must never be deleted by a scan that doesn't see them. Config-derived data is overwritten; human data is not.

8. **Edges are facts; vectors are hunches.** Vector similarity may propose a candidate relationship. It may never write a confirmed edge without human confirmation.

9. **Never publish an assumable IAM role ARN.** Judges use the hosted app. A published ARN is an open door into the account.

---

## 2. Verify before you build

Do these first. Each one can invalidate downstream work if assumed wrong.

- **CockroachDB vector index syntax and dimensionality.** CockroachDB uses its own indexing, not pgvector's. Confirm the actual DDL against the live cluster and the current docs before anything depends on embeddings. Confirm the embedding model's output dimension from the model, not from documentation you half-remember.
- **Managed MCP Server connectivity from a non-interactive context.** Service account API keys, not interactive OAuth. Prove a scripted client can connect and run a query before building the agent's read path on it.
- **CloudTrail event history in the target account.** Query it. Confirm events return and note how far back they go. **If it comes back empty, stop and report — the design changes materially.**
- **Cost Explorer availability** and whether resource-level granularity is offered, and at what cost. The design assumes it is *not* usable and falls back to service/region/tag-level attribution. Confirm before building either path.
- **Memory framework gate.** See §5.

Report the results of all of these before proceeding past Phase 1.

---

## 3. Build phases

Phases are dependency-ordered. Within a phase, order is yours. Each phase has an acceptance test — meet it before moving on.

### Phase 1 — Foundation
Cluster connection, schema, migrations, config, secrets handling, local dev loop.

*Accept when:* you can write a row and read it back from application code, and the vector index syntax is confirmed working against the live cluster.

### Phase 2 — Inventory
Resource scan into the current-state store. Start with a single region and a handful of services, then generalize.

Implement the tiered scan: use cost data to find regions with spend, probe candidates cheaply in parallel, full sweep only on regions showing life, one pass for global services. Two known holes to handle — free resources don't appear in cost data, and cheap tag-based probes miss non-taggable resources. Cheap probes cannot be the only gate.

*Accept when:* a scan of the real account completes without brute-forcing all regions, and the resource count is plausible against what the human reported.

### Phase 3 — The free past
Backfill CloudTrail's default event-history window into the change log. Attach actor identity where available.

Do this early. It's the only source of history that exists before the tool has been running, and the demo depends on it.

*Accept when:* the change log contains real historical events with attributed actors, and you can answer "when did X change and who did it?"

### Phase 4 — Diffing
Scan-over-scan comparison producing change-log entries. Deltas only, never full snapshots.

*Accept when:* running two scans across a deliberate change produces exactly one meaningful change record, and running two scans with no change produces none.

### Phase 5 — Relationships
Edge extraction into an adjacency structure in CockroachDB. Both directions indexed. Traversal via recursive queries.

Start with the highest-value edge types and stop when you have solid coverage of the common cases — this is a long tail and completeness is not the goal. Every edge carries its source: derived from config, inferred from convention, or supplied by a human. Config edges are overwritten each scan. Human edges are not.

*Accept when:* you can answer "what breaks if I delete this?" with a depth-limited traversal in a single query, and a rescan does not destroy a human-asserted edge.

### Phase 6 — Memory
Embeddings, the three memory tiers, the write path with inline merge, the verbatim human field, the durability filter.

Re-read §5 of the design summary before implementing this. The rules there about capture, merge, and collision are specific and were arrived at by eliminating alternatives.

*Accept when:* an explicit "remember this" is stored unfiltered; a conflicting write merges rather than overwrites; a deliberately failed merge results in two rows, not one lost one.

### Phase 7 — Agent read path
The agent queries its own memory through the Managed MCP Server, forming its own SQL. Application writes go through a normal driver.

*Accept when:* a transcript shows the agent composing and running its own query against memory to answer a question.

### Phase 8 — Chat
The agent loop, tool use, front end. ARN-bearing answers.

*Accept when:* the seven question classes in §3 of the design summary all return correct, ARN-bearing answers against the real account.

### Phase 9 — Retrieval
Four lanes: structured filters, full-text on identifiers, vector on concepts, graph traversal from a seed. See §7 of the design summary. **The lane-selection and ranking strategy is unspecified and is yours to design** — this maps directly onto a full judging criterion, so treat it as design work, not plumbing. Do not default to top-k vector search and call it done.

*Accept when:* a question containing an exact ARN retrieves by identifier rather than by embedding similarity, and a vague conceptual question retrieves sensibly.

### Phase 10 — Work reuse
Cache completed analyses with an embedding of the question that produced them. On a rephrased repeat, match and offer reuse-or-refresh instead of re-running the pipeline.

*Accept when:* a reworded repeat of an expensive question returns near-instantly with an explicit choice to refresh.

### Phase 11 — The manage pass
Scheduled scan, diff, and **verification**. Mostly dumb — no model invocation unless something moved. Each memory carries a claim and a cheap re-check; the pass re-verifies, refreshes the timestamp, or retires the memory.

**This is unspecced and it is the differentiating feature of the submission.** Design it properly: what a verification check contains, how it executes, what retires versus refreshes, and how retirement surfaces to the user.

*Accept when:* a memory made false by a real account change is automatically retired, visibly, without a human asking.

### Phase 12 — The human layer
Annotations, suppressions, human-asserted edges, feature groupings. Candidate-edge proposal from vector clustering, surfaced as questions the agent asks.

*Accept when:* a suppression stops a finding from re-appearing on the next scan, and the agent proactively asks about a cluster it can't name.

### Phase 13 — Cost
Attribution against inventory using whatever granularity the verification step in §2 established as available.

### Phase 14 — Production
Telemetry on model calls (latency, tokens, cost). Bedrock spend cap. Rate limiting on the chat endpoint. Multi-account wiring — the schema should already support it.

---

## 4. Decisions you own vs. decisions you escalate

**You own, without asking:** all schema and index design; library and framework selection within the constraints; module structure; deployment shape (Lambda versus container, front-end hosting, scheduling mechanism); error handling and retry strategy; which edge types to extract and how many; retrieval ranking; prompt design; test strategy.

**Stop and ask before:** adding a second datastore; adding any dependency on an AWS feature the user must enable; anything that writes to the studied AWS account; changing what "read-only" means; dropping a phase entirely; anything that would put memory outside CockroachDB.

**Report, don't decide:** the results of every check in §2. If CloudTrail history is empty, or the MCP server can't be reached non-interactively, or the vector index behaves differently than documented — surface it rather than working around it silently.

---

## 5. Memory framework — resolve early

Do not build a general-purpose memory engine from scratch. Extraction, classification, merging, decay, and retrieval ranking are solved elsewhere and are the genuinely hard parts.

**Rejected: mem0.** No CockroachDB provider; its Postgres path emits pgvector-specific index DDL that CockroachDB will not accept.

**Candidate: Memori.** Apache-2.0, self-hostable, Python SDK, documented CockroachDB backend with an example in its repo. Marked alpha by its own metadata.

**Fallback: LangChain's CockroachDB integrations.** Official vector store and chat history providers. Building blocks rather than a memory system, but low risk.

**The gate.** Before committing to any framework, prove it (a) connects to the cluster, (b) creates its schema successfully, (c) round-trips a memory, and (d) supports arbitrary metadata per memory — resource identifier, region, verified timestamp. If (d) fails, the verification story doesn't work and the framework isn't worth adopting.

**What no framework does for you.** They all extract memory from conversation transcripts. That's roughly a third of the need here. Scan deltas, cached analyses, edges, and suppression rules arrive from a scheduled job, not a chat. Expect to own the machine-side layer regardless — that part is a few tables and an upsert, not a memory engine.

---

## 6. Blocked on human input

Do not guess these. Ask, and proceed on other phases while waiting.

- **Terraform drift: narrow feature or cut entirely?** Outstanding across multiple rounds. Determines whether IaC coverage ships at all.
- **Whether broad read-only IAM access is acceptable for a public demo**, or a tighter custom policy is required.
- **Entrant identity** — personal or through an LLC. Affects repository ownership and submission paperwork; everything downstream must stay consistent.
- **All values from the reconnaissance list** in the setup checklist: account ID, CloudTrail history depth, resource count, active regions, Cost Explorer status, spend by service, Terraform state location.

---

## 7. Definition of done

**The product works** when all seven question classes return correct, ARN-bearing answers against a real account with nothing enabled in it.

**The memory is load-bearing** when: a human annotation changes future answers; a suppression persists across scans; a memory retires itself when the account changes; and a rephrased repeat question is answered from cache.

**The submission is complete** when:
- Public repository, license detected and visible in GitHub's About sidebar
- At least two CockroachDB tools used meaningfully — not merely initialized
- At least one AWS service in the running architecture
- Demo URL live, free, unauthenticated, and working
- Video under three minutes that shows the CockroachDB memory layer working, not just a chat window
- README leads with the differentiator: every comparable tool is a stateless snapshot; this one remembers, and its memory verifies itself
- All code written new for this submission; anything reused is disclosed

**Judging weights, for prioritization when trading off:** Agentic Memory Design, Implementation Quality, Real-World Impact, Product Readiness, and Creativity & Originality are equally weighted. Memory design is a full fifth on its own — a feature that makes memory more clearly load-bearing beats a feature that adds surface area.

---

## 8. Things already tried and rejected

Do not spend effort re-deriving these. Full reasoning is in `design-summary.md` §12.

Full state snapshots per scan. AWS Config, CloudTrail trails, or CUR pipelines as data sources. A scratchpad reconciled at end of conversation. Provisional/confirmed status columns on memories. Blind overwrite on collision. Deferring merges to the scheduled job. Any filter that can overrule an explicit "remember this." mem0 as the memory framework. Neo4j or any separate graph database. Vectors as a representation of relationships.

---

## 9. Positioning — read before writing the README

The comparable tools are numerous and mature. Cost audit is crowded: AWS's own FinOps Agent, the aws-samples cost analyzer, CloudAudit, aws-mcp-audit. Terraform drift is crowded: driftctl, Firefly, StackGuardian, AlertD.

**None of them have persistent memory.** They are all stateless snapshots. That is the entire differentiator, and it should be the first thing a judge reads and the first thing the video demonstrates.

The sharper version of the claim: staleness is the unsolved problem in agent memory, because facts about people cannot be cheaply re-verified. In AWS, every conclusion is verifiable with an API call. So this agent's memory checks itself and retires what has become false. Nothing else in this space makes that argument.

*AWS forgets after 90 days. The agent doesn't.*
