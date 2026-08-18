# Video script — AWS Biographer

**Target runtime: 55 seconds.** The contest requires under 3 minutes, public on
YouTube or Vimeo, and it must show the CockroachDB memory layer working rather
than just a chat window.

Everything this script needs is already captured in
[`video-assets/`](video-assets/). No AWS access is required to assemble it.

---

## Assets

| File | What it is |
|---|---|
| `video-assets/architecture.png` | rendered architecture diagram, 1920px wide |
| `video-assets/chat-frame.html` | the real UI with two real answers baked in — open in a browser, screenshot at 1280×900 |
| `video-assets/answer-waste.json` | raw response, including `read_path: mcp` |
| `video-assets/answer-memory.json` | raw response showing human and agent memories side by side |
| `video-assets/memories-with-claims.txt` | live SQL output: each memory and the claim that makes it falsifiable |

## Production notes

- 1920×1080, 30fps. No music, or royalty-free only — **no copyrighted audio**.
- Narration can be text-to-speech. Keep it level and unhurried.
- On-screen text should be large enough to read on a phone.
- Do not show `.env`, any API key, or the read-only role ARN at any point.

---

## Shot list

### Shot 1 — Title card (0:00–0:06)

**Visual:** dark background (`#0e1116`), white text, centered.

> **AWS Biographer**
> An AWS account has no biographer.
> CloudTrail knows *what* changed. Nobody knows *why*.

**Narration:** "Every AWS tool gives you a snapshot. This one remembers — and
its memory checks itself."

---

### Shot 2 — It answers with real identifiers (0:06–0:20)

**Visual:** screenshot of `chat-frame.html`, first exchange. Slowly scroll the
answer. At 0:16, zoom the `read_path: mcp` line in the metadata row and hold.

**On-screen callout:** `read_path: mcp`

**Narration:** "Ask what's being wasted, and it names the volume, the elastic
IP, the identifier you can paste into the console. The agent wrote that SQL
itself and ran it through CockroachDB's Managed MCP Server."

---

### Shot 3 — One database doing four jobs (0:20–0:32)

**Visual:** `architecture.png`. Start framed on the CockroachDB block at the
bottom, then pull back to reveal the whole diagram.

**On-screen callout, appearing in sequence:**
- `identifier · structured · graph · vector`
- `fused by RRF`
- `VECTOR(1024) · no graph database`

**Narration:** "Relational filters, full-text, recursive graph traversal, and
vector similarity — one store, one query. We didn't need a graph database."

---

### Shot 4 — The part that is actually new (0:32–0:48)

**Visual:** `memories-with-claims.txt` as a terminal-styled full-screen block,
monospace, on the dark background. Highlight the `claim` column.

**On-screen callout:** `every memory carries the claim that makes it true`

**Narration:** "Here is the difference. Every memory the agent forms carries a
falsifiable claim — a declarative statement of what would have to be true. Every
hour, it re-checks all of them against the account. When a claim goes false, the
memory retires with its reason attached. It doesn't quietly rot. And the
memories a human supplied carry no claim at all, because nothing the account
does gets to overrule you."

---

### Shot 5 — Close (0:48–0:55)

**Visual:** dark card, three lines, generous spacing.

> **AWS Biographer**
> Live demo: wp7s54jbd3ztuoke4xfshum2d40ocsfk.lambda-url.us-east-1.on.aws
> github.com/sp-entertainment/aws-biographer
>
> CockroachDB Managed MCP Server · Distributed Vector Indexing
> Amazon Bedrock · Lambda · EventBridge

**Narration:** "Memory that expires itself. Built on CockroachDB and Amazon
Bedrock."

---

## Optional live beat

If you are at the machine with working AWS credentials, replace Shot 4 with a
screen recording — it is stronger, and it is the one thing no comparable tool
can show. It takes about 90 seconds to set up and runs in three commands:

```bash
aws ec2 attach-volume --region us-east-1 --volume-id vol-0ccf11a8b75598a77 --instance-id i-0ad130ebd061c9a6f --device xvdf
```

```bash
python -m biographer.manage
```

The pass prints `RETIRED 1 memories` with the reason, because the orphaned
volume is no longer orphaned and the claim that justified the memory is now
false. Nothing told the system the change happened.

Then ask the demo *"has anything you believed stopped being true?"* and it
answers from the retirement record.
