# Contributing

**This repository is not accepting pull requests.**

It was built as a submission for the CockroachDB × AWS *Build with Agentic
Memory* contest, and it is published so the work can be read and judged, not
maintained as a community project. Pull requests will be closed unread — not
out of rudeness, but because leaving them open would imply a review process
that does not exist here.

## What is welcome

- **Read the code.** Start with [README.md](README.md), then
  [docs/decisions/](docs/decisions/), where every non-obvious call is written
  down with its reasoning.
- **Fork it.** MIT licensed. Take any part of it and do what you like.
- **Open an issue** if you spot something factually wrong — a bug in the
  reasoning, a claim the code does not actually support. Corrections are
  useful even when patches are not.

## If you are looking for the interesting parts

- `src/biographer/memory/verify.py` — the falsifiable-claim system, which is
  the idea the whole project rests on.
- `src/biographer/retrieval.py` — four retrieval lanes fused with Reciprocal
  Rank Fusion.
- `src/biographer/mcp.py` — the agent's read path through CockroachDB's
  Managed MCP Server, with an announced fallback.

Thanks for looking.
