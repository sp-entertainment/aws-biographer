"""HTTP front for the chat agent, plus the Lambda entry point.

Deliberately stdlib. The whole surface is three routes -- serve a page, answer a
question, report spend -- and a framework would add a dependency, a build step,
and a cold-start cost for routing that fits in twenty lines. ADR-0003 puts this
behind a Lambda Function URL with no API Gateway in front, so there is no
framework-shaped hole to fill either.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..bedrock import spend_summary
from ..db import pool
from ..memory import analyses
from ..memory.verify import recent_retirements
from .loop import ask, default_account

log = logging.getLogger(__name__)

def _web_dir() -> pathlib.Path:
    """Locate web/ in both layouts.

    In the repo this file is src/biographer/agent/server.py and web/ is three
    levels up. In the Lambda bundle the package sits at the task root, so it is
    only two. Hard-coding either index serves a 500 in the other environment,
    which is the kind of bug that only appears after deploying.
    """
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "web"
        if (candidate / "index.html").is_file():
            return candidate
    raise RuntimeError(f"web/ not found above {here}")


WEB_DIR = _web_dir()

# Spend controls for a public, unauthenticated demo. Judges get no login, so the
# ceiling has to sit somewhere; these are the cheapest useful ones.
MAX_QUESTION_CHARS = 500
RATE_LIMIT_PER_MINUTE = 10
# A lifetime total, not a daily reset: spend_summary() sums the whole telemetry
# table with no date filter. The name used to say "daily" and the docs had to
# keep correcting it. Overridable by environment variable so the ceiling can be
# raised from the Lambda console during judging without a rebuild.
SPEND_CEILING_USD = float(os.environ.get("SPEND_CEILING_USD", "40.00"))

_hits: dict[str, list[float]] = {}


def _rate_limited(client: str) -> bool:
    """Fixed-window per-IP limiter.

    The counter is in-process, and this function has no reserved concurrency
    (see infra/app.py -- the account's total limit is 10, and reserving any of
    it drops the unreserved pool below the minimum AWS allows). So the real
    ceiling is this limit times the number of live instances, up to ten. That
    is loose on purpose: the spend ceiling below is the control that actually
    bounds cost, and this one only stops a single client hammering the demo.

    ponytail: move to a CockroachDB table if a true global limit is ever needed.
    """
    now = time.time()
    window = [t for t in _hits.get(client, []) if now - t < 60]
    window.append(now)
    _hits[client] = window
    return len(window) > RATE_LIMIT_PER_MINUTE


def _over_budget() -> bool:
    try:
        return spend_summary()["total_usd"] >= SPEND_CEILING_USD
    except Exception:  # noqa: BLE001
        # A telemetry outage must not become an outage of the whole demo.
        return False


def answer(question: str, client: str = "local",
           force_refresh: bool = False) -> dict[str, Any]:
    """One question in, one answer out. Shared by the HTTP and Lambda paths."""
    question = (question or "").strip()
    if not question:
        return {"error": "ask me something about your AWS account"}
    if len(question) > MAX_QUESTION_CHARS:
        return {"error": f"question is limited to {MAX_QUESTION_CHARS} characters"}
    if _rate_limited(client):
        return {"error": "rate limit reached, try again in a minute"}
    if _over_budget():
        return {"error": "this demo has reached its model spend ceiling for now"}

    account = default_account()

    # Reuse-or-refresh, never silent reuse. A cached answer presented as fresh
    # is indistinguishable from a stale one, and knowing the difference is this
    # product's whole claim.
    if not force_refresh:
        cached = analyses.offer(account, question)
        if cached and not cached["recommend_refresh"]:
            return {
                "answer": cached["answer"],
                "cached": True,
                "cache_note": cached["note"],
                "matched_question": cached["original_question"],
                "analysis_id": cached["analysis_id"],
                "tools": [],
                "tokens": {"in": 0, "out": 0},
            }

    turn = ask(account, question)
    analyses.store(account, question, turn.answer,
                   inputs={"tools": [c["name"] for c in turn.tool_calls]})
    return {
        "answer": turn.answer,
        "cached": False,
        "tools": [c["name"] for c in turn.tool_calls],
        "read_path": turn.read_path,
        "tokens": {"in": turn.input_tokens, "out": turn.output_tokens},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            page = (WEB_DIR / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
        elif self.path == "/retired":
            body = json.dumps(recent_retirements(default_account(), 20), default=str)
            self._send(200, body.encode(), "application/json")
        elif self.path == "/spend":
            self._send(200, json.dumps(spend_summary()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ask":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        client = self.headers.get("X-Forwarded-For", self.client_address[0])
        result = answer(payload.get("question", ""), client,
                        force_refresh=bool(payload.get("refresh")))
        self._send(200, json.dumps(result).encode(), "application/json")

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.client_address[0], fmt % args)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda Function URL entry point."""
    path = event.get("rawPath", "/")
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    source_ip = event.get("requestContext", {}).get("http", {}).get("sourceIp", "unknown")

    if method == "GET" and path in ("/", "/index.html"):
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "text/html; charset=utf-8"},
            "body": (WEB_DIR / "index.html").read_text(encoding="utf-8"),
        }
    if method == "GET" and path == "/retired":
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps(recent_retirements(default_account(), 20),
                                   default=str)}
    if method == "GET" and path == "/spend":
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps(spend_summary())}
    if method == "POST" and path == "/ask":
        payload = json.loads(event.get("body") or "{}")
        return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
                "body": json.dumps(answer(payload.get("question", ""), source_ip,
                                          bool(payload.get("refresh"))))}
    return {"statusCode": 404, "body": "not found"}


def serve(port: int = 8080) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    log.info("http://localhost:%d", port)
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    finally:
        pool().close()


if __name__ == "__main__":
    serve()
