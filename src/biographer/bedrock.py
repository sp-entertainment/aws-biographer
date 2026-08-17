"""Bedrock: embeddings, chat, and the telemetry that doubles as a spend monitor.

A strong model reasons; a cheap model does merges and durability checks. That
split is not decoration -- merge and filter run on every memory write, and
paying reasoning-model prices for them would make the memory layer the most
expensive part of the product.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from .aws import BOTO_CONFIG, app_session
from .config import settings
from .db import pool

log = logging.getLogger(__name__)

# Published us-east-1 on-demand rates, USD per 1K tokens. Used only for the
# spend monitor; being a little stale is fine, being absent is not, because a
# public demo with no cost visibility is how a contest entry ends up costing
# real money.
PRICING = {
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": (0.003, 0.015),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (0.001, 0.005),
    "amazon.titan-embed-text-v2:0": (0.00002, 0.0),
}


@dataclass
class Reply:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    stop_reason: str | None = None
    tool_uses: list[dict[str, Any]] | None = None


def _client(service: str = "bedrock-runtime"):
    # app_session, not session_for: invoking a model is something the
    # application does, not something it reads from the studied account.
    return app_session().client(service, config=BOTO_CONFIG)


def _record(
    model_id: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    error: str | None = None,
    account_id: str | None = None,
) -> None:
    """Telemetry is best-effort: never fail a user's request to record a metric."""
    rate_in, rate_out = PRICING.get(model_id, (0.0, 0.0))
    cost = (input_tokens / 1000) * rate_in + (output_tokens / 1000) * rate_out
    try:
        with pool().connection() as conn:
            conn.execute(
                "INSERT INTO telemetry (account_id, model_id, purpose, input_tokens,"
                " output_tokens, latency_ms, cost_usd, error)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (account_id, model_id, purpose, input_tokens, output_tokens,
                 latency_ms, cost, error),
            )
            conn.commit()
    except Exception:  # noqa: BLE001
        log.debug("telemetry write failed", exc_info=True)


def embed(text: str, account_id: str | None = None) -> list[float]:
    """One embedding. Dimension is asserted, not assumed.

    A silent dimension change would be accepted by the insert and corrupt every
    similarity result afterwards, so it fails loudly here instead.
    """
    cfg = settings()
    started = time.monotonic()
    body = json.dumps({"inputText": text[:8000]})
    resp = _client().invoke_model(modelId=cfg.model_embed, body=body)
    payload = json.loads(resp["body"].read())
    vector = payload["embedding"]
    elapsed = int((time.monotonic() - started) * 1000)

    _record(cfg.model_embed, "embed", payload.get("inputTextTokenCount", 0), 0,
            elapsed, account_id=account_id)

    if len(vector) != cfg.embed_dim:
        raise ValueError(
            f"{cfg.model_embed} returned {len(vector)} dimensions, "
            f"schema expects {cfg.embed_dim}"
        )
    return vector


def converse(
    messages: list[dict[str, Any]],
    system: str | None = None,
    model_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 2000,
    purpose: str = "chat",
    account_id: str | None = None,
) -> Reply:
    """One Converse turn. Returns text plus any tool-use blocks."""
    cfg = settings()
    model = model_id or cfg.model_strong
    kwargs: dict[str, Any] = {
        "modelId": model,
        "messages": messages,
        # A hard ceiling per request is the cheapest spend control there is,
        # and a public unauthenticated demo needs one.
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.2},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    if tools:
        kwargs["toolConfig"] = {"tools": tools}

    started = time.monotonic()
    try:
        resp = _client().converse(**kwargs)
    except ClientError as exc:
        _record(model, purpose, 0, 0, int((time.monotonic() - started) * 1000),
                error=str(exc)[:500], account_id=account_id)
        raise
    elapsed = int((time.monotonic() - started) * 1000)

    usage = resp.get("usage", {})
    blocks = resp["output"]["message"].get("content", [])
    text = "".join(b["text"] for b in blocks if "text" in b)
    tool_uses = [b["toolUse"] for b in blocks if "toolUse" in b]

    _record(model, purpose, usage.get("inputTokens", 0), usage.get("outputTokens", 0),
            elapsed, account_id=account_id)

    return Reply(
        text=text,
        input_tokens=usage.get("inputTokens", 0),
        output_tokens=usage.get("outputTokens", 0),
        latency_ms=elapsed,
        stop_reason=resp.get("stopReason"),
        tool_uses=tool_uses or None,
    )


def ask_cheap(prompt: str, system: str | None = None, max_tokens: int = 500,
              purpose: str = "merge", account_id: str | None = None) -> str:
    """One-shot call to the cheap model."""
    return converse(
        [{"role": "user", "content": [{"text": prompt}]}],
        system=system,
        model_id=settings().model_cheap,
        max_tokens=max_tokens,
        purpose=purpose,
        account_id=account_id,
    ).text.strip()


def spend_summary() -> dict[str, Any]:
    """Total model spend. Doubles as Product Readiness evidence."""
    with pool().connection() as conn:
        total, calls, errors = conn.execute(
            "SELECT coalesce(sum(cost_usd), 0), count(*),"
            " count(*) FILTER (WHERE error IS NOT NULL) FROM telemetry"
        ).fetchone()
        by_purpose = {
            row[0]: {"calls": row[1], "cost_usd": float(row[2] or 0)}
            for row in conn.execute(
                "SELECT purpose, count(*), sum(cost_usd) FROM telemetry"
                " GROUP BY purpose ORDER BY sum(cost_usd) DESC"
            )
        }
    return {
        "total_usd": float(total),
        "calls": calls,
        "errors": errors,
        "by_purpose": by_purpose,
    }
