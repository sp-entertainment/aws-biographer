"""The chat agent: a Bedrock tool loop over memory, inventory, and history.

Invariant 4 is the hardest thing to get right here and it is a prompt problem as
much as a code problem: every answer that references a resource must carry an
ARN or a console-lookupable identifier. The tools therefore return identifiers
in every row, and the system prompt is explicit that an answer without them is
wrong.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .. import graph, mcp
from ..bedrock import converse
from ..db import pool
from ..memory import store
from ..retrieval import search

log = logging.getLogger(__name__)

MAX_TURNS = 6

SYSTEM = """You are the AWS Account Biographer. You answer questions about one \
AWS account by consulting durable memory and live inventory held in CockroachDB.

RULES, in order of importance:

1. Every statement about a resource MUST include its ARN or an identifier the \
user can paste into the AWS console. An answer without identifiers is wrong, \
however well written. When you list several resources, give each one's identifier.

2. Never invent an ARN, an identifier, a date, or a cost. If a tool did not \
return it, say you do not have it.

3. Prefer what you remember. Memories marked origin=human came from the user \
and outrank anything you infer.

4. Say when something is uncertain, stale, or unverified rather than smoothing \
over it.

5. Be concise. Lead with the answer, then the evidence.

You cannot modify anything in AWS. You are read-only there by design; say so if \
asked to change something."""

TOOLS: list[dict[str, Any]] = [
    {
        "toolSpec": {
            "name": "search_memory",
            "description": (
                "Search everything known about the account: durable memories, "
                "current inventory, and the resource graph. Routes across "
                "identifier, structured, vector and graph lanes automatically. "
                "Use this first for almost any question."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The user's question, or a focused rephrasing of it"},
                    "limit": {"type": "integer", "description": "Max results, default 10"},
                },
                "required": ["question"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "run_sql",
            "description": (
                "Run a read-only SELECT against the memory database to answer "
                "something the search tool cannot. Tables: resources(arn, region, "
                "service, resource_type, name, tags, config, first_seen, last_seen), "
                "changes(arn, change_type, field, old_value, new_value, actor, "
                "event_name, event_time, source), memories(topic, body, origin, "
                "human_text, verified_at, retired_at, resource_key), edges(src_arn, "
                "dst_arn, edge_type, source), analyses, suppressions. Always filter "
                "by account_id. This is CockroachDB: use config->>'Key' for text and "
                "config->'Key' for nested JSONB, and compare ->> results to "
                "text, never to a jsonb literal. tags is JSONB; untagged is "
                "tags = '{}'::jsonb."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "blast_radius",
            "description": (
                "What depends on a resource, i.e. what breaks if it is deleted. "
                "Walks the resource graph inward from the given ARN."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "arn": {"type": "string"},
                    "depth": {"type": "integer", "description": "Default 3"},
                },
                "required": ["arn"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "remember",
            "description": (
                "Store a durable fact about the account. Use when the user says "
                "to remember something, or tells you an account convention or a "
                "resource's purpose. Phrase 'body' as a STANDALONE sentence that "
                "makes sense with no conversation context. Set explicit=true when "
                "the user directly asked you to remember it."
            ),
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Short kebab-case subject, e.g. 'build-runner'"},
                    "body": {"type": "string", "description": "The fact, as a standalone sentence"},
                    "resource_key": {"type": "string", "description": "Resource id this is about, or empty for account-level"},
                    "explicit": {"type": "boolean"},
                },
                "required": ["topic", "body"],
            }},
        }
    },
]


@dataclass
class Turn:
    answer: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    read_path: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_hint: float = 0.0


def _run_tool(account_id: str, name: str, args: dict[str, Any]) -> tuple[str, str | None]:
    """Execute one tool. Returns (result_text, read_path_used)."""
    if name == "search_memory":
        hits, plan = search(account_id, args["question"], limit=args.get("limit", 10))
        return (
            json.dumps(
                {
                    "route": plan.why,
                    "results": [
                        {
                            # Identifier first in every row: invariant 4 is far
                            # easier for the model to honour when the identifier
                            # is impossible to miss.
                            "identifier": h.identifier,
                            "kind": h.kind,
                            "title": h.title,
                            "detail": h.detail,
                            "lanes": sorted(h.lanes),
                            "origin": h.payload.get("origin"),
                            # The AWS identifier a memory is about. Without it a
                            # memory hit offers only a UUID, which is useless to
                            # paste into a console -- and invariant 4 then fails
                            # on exactly the human annotations that matter most.
                            "about_resource": h.payload.get("resource_key") or None,
                            "first_seen": str(h.payload.get("first_seen") or ""),
                        }
                        for h in hits
                    ],
                },
                default=str,
            )[:12000],
            None,
        )

    if name == "run_sql":
        result, path = mcp.query_or_fallback(args["sql"])
        return result[:12000], path

    if name == "blast_radius":
        rows = graph.blast_radius(account_id, args["arn"], args.get("depth", 3))
        return json.dumps(rows, default=str)[:12000], None

    if name == "remember":
        memory = store.remember(
            account_id,
            args["topic"],
            args["body"],
            resource_key=args.get("resource_key", ""),
            origin=store.HUMAN if args.get("explicit") else store.AGENT,
            explicit=bool(args.get("explicit")),
        )
        return (
            json.dumps({
                "stored": not memory.dropped,
                "merged": memory.merged,
                "kept_alongside_existing": memory.conflict_kept,
                "memory_id": memory.memory_id,
                "reason": "not durable enough to keep" if memory.dropped else None,
            }),
            None,
        )

    return json.dumps({"error": f"unknown tool {name}"}), None


def ask(account_id: str, question: str,
        history: list[dict[str, Any]] | None = None) -> Turn:
    """One user question, run to a final answer through the tool loop."""
    messages: list[dict[str, Any]] = list(history or [])
    messages.append({"role": "user", "content": [{"text": question}]})

    turn = Turn(answer="")
    for _ in range(MAX_TURNS):
        reply = converse(messages, system=SYSTEM, tools=TOOLS, purpose="chat",
                         account_id=account_id)
        turn.input_tokens += reply.input_tokens
        turn.output_tokens += reply.output_tokens

        if not reply.tool_uses:
            turn.answer = reply.text
            return turn

        assistant_content: list[dict[str, Any]] = []
        if reply.text:
            assistant_content.append({"text": reply.text})
        for use in reply.tool_uses:
            assistant_content.append({"toolUse": use})
        messages.append({"role": "assistant", "content": assistant_content})

        results: list[dict[str, Any]] = []
        for use in reply.tool_uses:
            try:
                output, path = _run_tool(account_id, use["name"], use["input"])
                if path:
                    turn.read_path = path
            except Exception as exc:  # noqa: BLE001
                # A failed tool must come back as a result the model can react
                # to. Raising here would strand the user with no answer at all.
                output = json.dumps({"error": str(exc)[:400]})
                log.warning("tool %s failed", use["name"], exc_info=True)
            turn.tool_calls.append({"name": use["name"], "input": use["input"]})
            results.append({
                "toolResult": {
                    "toolUseId": use["toolUseId"],
                    "content": [{"text": output}],
                }
            })
        messages.append({"role": "user", "content": results})

    turn.answer = (
        "I ran out of investigation steps before reaching a confident answer. "
        "Try narrowing the question to one resource or one region."
    )
    return turn


def default_account() -> str:
    with pool().connection() as conn:
        row = conn.execute(
            "SELECT account_id FROM accounts ORDER BY created_at LIMIT 1"
        ).fetchone()
    if not row:
        raise RuntimeError("no account has been scanned yet")
    return row[0]
