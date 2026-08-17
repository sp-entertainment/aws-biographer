# Follow-ups for Ben

Everything below needs a human. Ordered by whether it blocks the submission.

---

## Blocking the submission

### 1. Grant the CockroachDB service account SQL access — **the one real gap**

The Managed MCP Server authenticates fine with the service account key: the
handshake and tool discovery both succeed. But every data-plane tool
(`select_query`, `list_tables`, `get_table_schema`, `show_statement`) returns
`unauthorized`. The service account can reach the Cloud API and cannot reach the
cluster's SQL layer.

**Why it matters:** the submission claims Managed MCP Server as one of its two
CockroachDB tools, and "the agent reads through MCP" is an architectural claim in
the README. Right now the agent falls back to a direct read-only connection and
says so on every answer (`read_path: direct-readonly`). That is honest, but it is
not the claim.

**Fix:** CockroachDB Cloud Console → Access Management → Service Accounts →
the account whose key is in `.env` → give it a role with SQL access to the
`biographer` cluster (Cluster Admin, or whatever the console offers that includes
SQL). No code change is needed — `mcp.available()` re-probes and the read path
flips back to MCP by itself.

**Verify:** `python -m biographer.mcp` should print `query_ok: True`.

Note the second CockroachDB tool, Distributed Vector Indexing, is confirmed
working on Basic, so the two-tool requirement is met either way. This is about
the strength of the claim, not its existence.

### 2. Devpost submission

Not started. Needs: repo URL, demo URL, text description, video link, which
CockroachDB tools and how, which AWS services and how.

- Repo: https://github.com/sp-entertainment/aws-biographer
- Demo: https://wp7s54jbd3ztuoke4xfshum2d40ocsfk.lambda-url.us-east-1.on.aws
- CockroachDB tools: Managed MCP Server (agent read path), Distributed Vector
  Indexing (memory and analysis-cache embeddings, `VECTOR(1024)`, cosine,
  account-prefixed)
- AWS services: Bedrock, Lambda, EventBridge Scheduler, IAM, KMS, Secrets
  Manager, CloudWatch Logs, plus the read-only APIs of everything it inventories

Create the draft now and fill it in — do not leave it to the last hour.

### 3. Video, under three minutes

Must show the CockroachDB memory layer working, not just a chat window. Split
screen: conversation on one side, live SQL against CockroachDB on the other.

The one beat that no comparable tool can show is memory retiring itself. It is
reproducible in about ninety seconds:

```bash
python -m biographer.manage --findings
```

```bash
aws ec2 detach-volume --region us-east-1 --volume-id vol-0ccf11a8b75598a77
```

```bash
python -m biographer.manage
```

The third command prints `RETIRED 1 memories` with the reason. Then ask the
demo "has anything you believed stopped being true?" and it answers from the
retirement record. No copyrighted music.

### 4. Confirm the licence shows in GitHub's About sidebar

`LICENSE` is MIT via GitHub's standard text, copyright Stone Pack Entertainment
LLC. Check the sidebar actually displays "MIT License"; if it does not, re-add
through GitHub's "Add file → Create new file → LICENSE" template chooser.

---

## Cost and cleanup

### 5. Seeded resources cost roughly $7.50/month

Two Terraform stacks, both fully managed, nothing orphaned:

```bash
cd seed/shadow && AWS_PROFILE=seed terraform destroy
```

```bash
cd seed/managed && AWS_PROFILE=seed terraform destroy
```

Keep them alive through judging (Sept 15, 2026), then destroy both. The demo
needs them to have anything to talk about.

### 6. Watch model spend

The deployed demo is public and unauthenticated. Controls in place: 500-character
question cap, 10 requests per minute per IP, and a $5.00 total spend ceiling that
returns a polite message instead of an answer once crossed.

Current spend is visible at `/spend` on the demo URL and in the page header.
**The ceiling is a total, not a daily reset** — if the demo gets traffic, raise
`DAILY_SPEND_CEILING_USD` in `src/biographer/agent/server.py` and redeploy, or it
will stop answering.

### 7. Rotate the credentials that passed through a chat transcript

The CockroachDB SQL password was pasted into chat once (and has already been
rotated once since). The `biographer-dev` access key was created without ever
being printed. After judging, rotate both as routine hygiene.

---

## Known gaps, none blocking

### 8. Lambda concurrency is 10 account-wide

Reserved concurrency had to be removed from the chat function because reserving
any of it drops the account's unreserved pool below the minimum AWS requires.
If you request a concurrency quota increase, re-add
`reserved_concurrent_executions` in `infra/app.py`.

### 9. Three resource types have no collector

Reconciliation reports them every scan rather than hiding them:
`lightsail:StaticIp`, `lightsail:KeyPair`, `payments:payment-instrument`. The
first two are cheap to add if the demo ever needs Lightsail. The third is account
metadata, not a resource, and should stay uncollected.

### 10. Cost figures are near zero because the account is new

Cost Explorer lags roughly 24 hours and the seeded resources were created on
17 August. The cost attribution path works and is tested; it simply has almost
nothing to report yet. It will read normally within a day.

### 11. `FINDING_MIN_AGE_MINUTES` is set to 5 locally

The default is 30 and the right production value is hours or days. It is low
because the demo account is hours old and nothing would qualify otherwise. Worth
raising before anyone points this at a real account.

### 12. Terraform drift was scoped in but not built

You chose "narrow feature" over "cut entirely". The groundwork exists — two
stacks with separate state, `seed/managed` deliberately being the IaC-covered
half so coverage has a real numerator — but no analyzer reads the state file.
This is the one item from the agreed scope that did not ship. It was the lowest
value against the judging criteria and it lost to Phases 9 and 11, where the
memory score lives.

---

## Reproducing everything

```bash
pip install -e ".[dev]" && pytest
```

```bash
python -m biographer.db && python -m biographer.scan.runner && python -m biographer.scan.cloudtrail
```

Acceptance scripts, each printing PASS/FAIL:

```bash
python scripts/verify_memory.py && python scripts/verify_cache.py && python scripts/verify_human_layer.py && python scripts/verify_questions.py
```

Redeploy after any code change:

```bash
python scripts/build_lambda.py && cd infra && AWS_PROFILE=seed CDK_DEFAULT_ACCOUNT=111122223333 CDK_DEFAULT_REGION=us-east-1 npx aws-cdk@latest deploy
```
