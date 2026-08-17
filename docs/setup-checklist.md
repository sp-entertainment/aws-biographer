# AWS Account Biographer — Human Setup Checklist

Deadline: **Aug 18, 2026, 5:00 PM EDT**

Work top to bottom. Anything marked ⏳ has waiting time — start it before moving on.
Anything marked 📋 produces a value you need to write down and hand to the build agent.

---

## Block A — Accounts & access (do first, ~45 min)

### A1. ⏳ Enable Bedrock model access
1. Sign in to the AWS Console.
2. Switch region to **us-east-1** (top-right region picker). Use this region for everything.
3. Search for **Bedrock** in the top search bar, open it.
4. Left sidebar → bottom → **Model access**.
5. Click **Modify model access** (or **Enable specific models**).
6. Check these:
   - **Anthropic → Claude Sonnet** (any current version)
   - **Anthropic → Claude Haiku** (cheap fallback)
   - **Amazon → Titan Text Embeddings V2**
7. Submit. Anthropic models may ask for a use-case form — fill it in ("internal developer tool for AWS account inventory Q&A").
8. Wait for status to show **Access granted**. Move on to A2 while waiting.

📋 Record: region used, and whether access is granted or pending.

### A2. Register on Devpost
1. Go to https://cockroachdb-ai.devpost.com/
2. Click **Join hackathon**.
3. Create or sign in to a Devpost account.
4. Accept the Devpost Terms and the AWS Event Terms when prompted.

📋 Record: Devpost username.

### A3. Create CockroachDB Cloud account + cluster
1. Go to https://cockroachlabs.cloud/signup
2. Sign up (no credit card needed).
3. Create a cluster:
   - Plan: **Basic / free tier**
   - Cloud provider: **AWS**
   - Region: **us-east-1** (match your AWS region — cuts latency)
   - Name it something like `biographer`
4. When prompted, create a **SQL user**. Save the generated password immediately — it is shown once.
5. Click **Connect** → copy the **connection string** (starts with `postgresql://`).

📋 Record: connection string, SQL username, password. Put them somewhere safe — these go into the build agent's `.env`, never into the public repo.

### A4. Enable the Managed MCP Server
1. In the CockroachDB Cloud Console, select your cluster.
2. Find **MCP Server** (usually under the cluster's Connect or Tools section).
3. Enable it and copy the **MCP config snippet**.
4. Sanity test: paste that snippet into Claude Code or Cursor's MCP config and ask it "list the tables in this database." It should answer (with zero tables — that's fine).

📋 Record: the MCP config snippet, and confirmation the sanity test worked.

### A5. Install and authenticate the ccloud CLI
1. Install: https://www.cockroachlabs.com/docs/cockroachcloud/ccloud-get-started
   - macOS: `brew install cockroachdb/tap/ccloud`
2. `ccloud auth login`
3. Create a **service account** (Console → Access Management → Service Accounts), give it a limited role, generate an API key.

📋 Record: service account API key. (This is optional — third CRDB tool. Skip if time is tight.)

---

## Block B — Account reconnaissance (~30 min)

Run these from a terminal with your AWS credentials active. All are read-only.

### B1. Confirm CLI works and identify the account
```bash
aws sts get-caller-identity
```
📋 Record: account ID, and the identity you're using.

### B2. Check CloudTrail event history exists
```bash
aws cloudtrail lookup-events --max-results 5
```
📋 Record: did it return events? How far back does the oldest one go?
This is the agent's free 90-day past. If it's empty, tell me — it changes the design.

### B3. Count your resources
```bash
aws resourcegroupstaggingapi get-resources \
  --query 'length(ResourceTagMappingList)'
```
📋 Record: the number. Sets embedding cost and scan time.

### B4. List regions you actually use
```bash
aws ec2 describe-regions --query 'Regions[].RegionName' --output text
```
Then, for each region you think you use, spot check:
```bash
aws resourcegroupstaggingapi get-resources --region us-east-1 \
  --query 'length(ResourceTagMappingList)'
```
📋 Record: which regions have real resources in them. If it's 1–3, the demo will be fast.

### B5. Check Cost Explorer status
1. Console → **Billing and Cost Management** → **Cost Explorer**.
2. If it prompts you to enable it, note that (enabling is free and takes ~24h to backfill).
3. Go to **Cost Explorer → Preferences** (or Billing → Cost Management Preferences).
4. Look for a **resource-level granularity** or **hourly/resource data** toggle.

📋 Record: is Cost Explorer on? Does the resource-level toggle exist, what does it say it costs, and is it already enabled?
This determines whether per-resource cost answers are possible without extra setup.

### B6. Test the cost API
```bash
aws ce get-cost-and-usage \
  --time-period Start=2026-07-01,End=2026-08-01 \
  --granularity MONTHLY \
  --metrics BlendedCost \
  --group-by Type=DIMENSION,Key=SERVICE
```
📋 Record: did it work, and roughly what does your monthly spend look like by service?

### B7. Locate your Terraform state (optional feature)
Find where your tfstate files live — S3 bucket + key, or Terraform Cloud.

📋 Record: bucket name and path, or "none / not applicable."

---

## Block C — Project setup (~30 min)

### C1. Decide the entrant
Choose: **you personally** or **Stone Pack Entertainment LLC**.
- Personal is simpler (W-9 as an individual).
- LLC means you're named Representative and the prize pays to the business.

📋 Record: which one. Everything else must stay consistent with it.

### C2. Create the GitHub repo
1. New repo, **Public**.
2. Add a **LICENSE** file — MIT or Apache 2.0, using GitHub's license picker so it's detected.
3. Verify: the repo's **About** sidebar on the right shows the license name. If it doesn't show, the picker didn't detect it — re-add via GitHub's "Add file → Create new file → LICENSE" flow which offers a template chooser.
4. Add a placeholder README.
5. Important: this repo must contain **only new code written during the submission period**. Don't paste in prior projects. If you reuse anything pre-existing, it has to be disclosed in the submission.

📋 Record: repo URL.

### C3. Create the read-only IAM role
1. Console → **IAM** → **Roles** → **Create role**.
2. Trusted entity: **AWS account** → *this account* (for local dev), and check **Require external ID**, generate a random string for it.
3. Permissions: attach **`ReadOnlyAccess`** and **`SecurityAudit`** (both AWS-managed).
4. Name it something identifiable. Do not commit the name or ARN.
5. Create.

📋 Record: role ARN and external ID.

Note: `ReadOnlyAccess` includes read access to data in some services (S3 object reads, etc.). If that makes you uncomfortable for a public demo, we can write a tighter custom policy later — but start here so nothing is blocked during the build.

### C4. Decide on the demo sandbox (recommended)
The submission form requires a functional demo URL, and judges won't deploy into their own account.
1. Create a **separate AWS account** (or reuse a scratch one).
2. Later, we'll seed it with deliberately messy resources so the demo has something to find.
3. Create the same read-only role there.

📋 Record: sandbox account ID, or "skipping sandbox."

---

## Block D — Submission logistics (do near the end, but set up now)

### D1. Video
- Needs a YouTube or Vimeo account, video set to **Public** or **Unlisted-but-publicly-viewable**.
- Under 3 minutes.
- Must show the CockroachDB memory layer actually working — not just a chat window.
- No copyrighted music.

### D2. Draft submission early
Devpost lets you save drafts. Create the draft on day one and fill it in as you go — do not leave it to the last hour.

Required fields: repo URL, demo URL, text description, video link, which CockroachDB tools you used and how, which AWS services you used and how. Optional but worth doing: architecture diagram.

### D3. Keep it live
The demo must stay accessible, free, and unrestricted to judges until **Sept 15, 2026**. Budget for a month of hosting.

---

## Report back with

From Block A: region, Bedrock status, CRDB connection string obtained (y/n), MCP snippet working (y/n)
From Block B: account ID, CloudTrail events (y/n + oldest date), resource count, active regions, Cost Explorer status + resource-granularity toggle wording, monthly spend by service, tfstate location
From Block C: entrant choice, repo URL, role ARN created (y/n), sandbox decision

Plus the one open question: **Terraform drift — narrow scope, or cut entirely?**
