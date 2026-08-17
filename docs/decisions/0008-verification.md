# ADR-0008 — The manage pass and memory verification

**Status:** Accepted
**Date:** 2026-08-17

## Context

Design summary §5 marks this OPEN, calls it "the differentiating feature of the
whole submission", and notes it has never been specced. The handoff repeats it:
decide what a verification check contains, how it executes, what retires versus
refreshes, and how retirement surfaces to the user.

The argument the whole submission rests on: staleness is unsolved in agent
memory because facts about people cannot be cheaply re-checked. In AWS they can.

## Decision 1 — A claim is declarative data, not code and not a model call

Every memory may carry a `claim`: one of a closed set of kinds
(`resource_exists`, `resource_absent`, `config_equals`, `config_absent`,
`untagged`, `edge_exists`), a resource ARN, a config path, and an expected
value.

The alternatives were both worse. An executable check authored by a model is
arbitrary code with a database behind it, and no sandbox makes that a good trade
for a feature whose entire value is being trustworthy. A model call per memory
per pass would put a language model in the loop of a job that must run on a
schedule over every memory, which is precisely the cost the "mostly dumb"
instruction exists to avoid.

A closed enum is auditable, can only read, and cannot be turned into an
injection.

## Decision 2 — Verification reads the cache, not AWS

The scan immediately preceding verification already made every API call.
Re-asking AWS would double the cost of the most expensive operation in the
product to learn something the current-state cache already knows.

This is the decision that makes the feature viable. Because a check is one
indexed query, *every* live memory can be re-checked on *every* pass. Verification
is continuous rather than sampled, and the manage pass on a quiet account
invokes no model at all.

## Decision 3 — Three verdicts, not two

`HOLDS` refreshes `verified_at`. `FALSE` retires. The third verdict is the one
that matters:

**`UNVERIFIABLE` changes nothing.** A claim whose resource is missing from a
region the last scan did not sweep, whose kind is unrecognised, or which has no
claim at all comes back unverifiable — explicitly not false.

Absence of evidence is not evidence of absence. Without this verdict, one
permission error, one throttled scan, or one tiered sweep that skipped a quiet
region would retire the entire memory base. That failure is far worse than a
stale memory and much harder to notice, because the system would look like it
was working.

Memories with no claim — which includes most human annotations — are
permanently unverifiable, and that is correct. They are the most valuable
memories in the system and nothing about them is machine-checkable.

## Decision 4 — Retire, never delete

Retirement sets `retired_at` and `retire_reason`. The row stays.

A retired memory is still evidence of what the agent believed and why it stopped
believing it, which is exactly what design summary §5's self-reinforcing-error
failure mode requires in order to be escapable. An agent that deletes its
mistakes cannot be audited for them.

## Decision 5 — Retirement surfaces three ways

A `retired_memories` tool the agent can consult, a `/retired` HTTP endpoint, and
the manage pass printing every retirement with its reason. The user does not
have to ask, and does not have to know the feature exists to benefit from it.

## The findings layer

Verification is only interesting if memories exist to verify. `memory/findings.py`
turns scan observations into memories that carry their falsifying claim.

Each is phrased with a duration drawn from `first_seen` — the column the scan
deliberately never overwrites. That is the §5 durability test made concrete:
"this Elastic IP is unattached" is re-derivable and not worth storing; "this
Elastic IP has been unattached for 3 hours" is only knowable over time.

`FINDING_MIN_AGE_MINUTES` gates how long a condition must persist first. It is
configurable because the right value depends on the account's age: an account
with months of history wants hours or days, while a demo account seeded this
morning would produce nothing at all under that threshold and look broken.

Findings bypass the durability filter — not as an exemption, but because they
satisfy it by construction. Paying a model call to ask "is this durable?" about
a fact built to be durable is pure cost.

## Verified end to end

An EBS volume was attached to an instance through the AWS CLI. Nothing told the
system what had changed. The next manage pass:

```
retired memory orphaned-volume-biographer-shadow-orphaned-vol-tagged (agent):
  AttachedTo is now ['i-0ad130ebd061c9a6f']

verified 15 memories (11 refreshed, 1 retired, 3 unverifiable)
```

Exactly one memory retired — the one the change falsified. Three stayed
unverifiable (the human annotations, correctly). Asked afterwards whether
anything it believed had stopped being true, the agent answered from the
retirement record with the ARN and the timestamp.

## Consequences

Claim coverage is a long tail: only findings generated by `findings.py` and
memories the agent chooses to attach a claim to are machine-checkable. Human
annotations mostly are not, and stay unverifiable forever. That is honest rather
than ideal — the alternative is inventing claims for facts that have none, which
would produce confident retirements with no evidence behind them.
