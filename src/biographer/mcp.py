"""Client for CockroachDB Cloud's Managed MCP Server -- the agent's read path.

Design summary §8 splits database access by path on purpose. The agent reads
through MCP, forming its own SQL to explore memory; the application writes
through a normal Postgres driver, because writes are deterministic code and no
model needs to compose an INSERT.

Authentication is a service account API key, not the interactive OAuth flow the
CLI uses. The scan and manage passes run in Lambda where nobody is present to
approve a browser prompt, so an OAuth-only read path would work in development
and fail in production -- which is why §2 makes proving this a gate.

Hand-rolled over urllib rather than pulling in the MCP SDK: the server is
stateless (it issues no session id), the transport is one POST returning a
single SSE frame, and that is thirty lines. An async SDK and its dependency
tree would be the larger, not smaller, thing to maintain.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

ENDPOINT = "https://cockroachlabs.cloud/mcp"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_DATABASE = "defaultdb"

# The server's read-only tools. insert_rows is deliberately excluded from the
# agent's surface: application writes go through psycopg, and handing the model
# an INSERT tool would put a language model in the write path for no benefit.
READ_TOOLS = ("select_query", "list_tables", "get_table_schema", "show_statement")


class MCPError(RuntimeError):
    pass


def _post(payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    cfg = settings()
    if not cfg.crdb_api_key or not cfg.crdb_cluster_id:
        raise MCPError("CRDB_API_KEY and CRDB_CLUSTER_ID must be set")

    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {cfg.crdb_api_key}",
            "mcp-cluster-id": cfg.crdb_cluster_id,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
    except urllib.error.HTTPError as exc:
        raise MCPError(f"MCP HTTP {exc.code}: {exc.read().decode()[:300]}") from exc

    # Responses arrive as a single SSE frame even for one-shot calls.
    for line in raw.splitlines():
        if line.startswith("data: "):
            body = json.loads(line[6:])
            if "error" in body:
                raise MCPError(str(body["error"])[:400])
            return body.get("result", {})
    raise MCPError(f"no data frame in MCP response: {raw[:200]}")


def initialize() -> dict[str, Any]:
    """Handshake. Proves the key works before anything depends on it."""
    return _post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "biographer", "version": "0.1"},
            },
        }
    )


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Invoke one MCP tool and return its text content."""
    if name not in READ_TOOLS:
        raise MCPError(f"{name} is not on the agent's read-only tool surface")

    # cluster_id travels in the mcp-cluster-id header; passing it as an argument
    # too is rejected outright ("cluster_id is set in your MCP config").
    args = dict(arguments)
    args.setdefault("database", DEFAULT_DATABASE)

    result = _post(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
    )
    chunks = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
    text = "\n".join(chunks).strip()
    if result.get("isError"):
        raise MCPError(text or "tool reported an error")
    return text


def query(sql: str) -> str:
    """Run a SELECT the agent composed itself.

    No SQL validation here. The server enforces read-only -- select_query
    rejects anything that is not a SELECT -- and duplicating that check in the
    client would be a second, weaker implementation of a rule already enforced
    where it matters.
    """
    return call_tool("select_query", {"query": sql})


def available() -> bool:
    """Can the agent actually read through MCP right now?

    Cached per process. The answer depends on a console-side role grant, not on
    anything in this codebase, so it cannot be assumed either way.
    """
    global _available
    if _available is None:
        try:
            query("SELECT 1")
            _available = True
        except MCPError as exc:
            log.warning(
                "MCP read path unavailable (%s). The service account authenticates "
                "to the Cloud API but lacks SQL access to the cluster; grant it a "
                "role with SQL privileges in the CockroachDB Cloud console. "
                "Falling back to the direct read-only connection meanwhile.",
                str(exc)[:120],
            )
            _available = False
    return _available


_available: bool | None = None


def query_or_fallback(sql: str) -> tuple[str, str]:
    """Run the agent's SQL, preferring MCP. Returns (result, path_taken).

    The fallback is a direct psycopg query with the same read-only discipline.
    It exists so a console permission gap does not take the whole product down,
    and it announces which path served every call rather than hiding the
    difference -- a read path that silently stops being the MCP read path would
    quietly invalidate the architecture this project claims.
    """
    if available():
        return query(sql), "mcp"

    from .db import pool

    # Models routinely prefix SQL with a comment or a blank line, and rejecting
    # that reads to the model as "the query was wrong" -- it then rewrites the
    # query instead of the formatting and burns a turn each time.
    stripped = re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/|\s)+", "", sql, flags=re.DOTALL)
    stripped = stripped.strip().rstrip(";")
    if not stripped.lower().startswith(("select", "with", "show", "table")):
        raise MCPError(
            "read-only path accepts SELECT, WITH, SHOW and TABLE only; "
            f"got: {stripped[:60]}"
        )
    import psycopg

    try:
        with pool().connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            rows = conn.execute(stripped).fetchall()
    except psycopg.Error as exc:
        # Hand the model a correction, not a stack trace. CockroachDB's opaque
        # "unsupported binary operator: <jsonb> ->> <string>" means ->> was
        # compared against a jsonb literal; without the hint the model rewrites
        # the whole query and burns a turn guessing.
        message = str(exc).strip().splitlines()[0]
        if "->>" in message or "jsonb" in message.lower():
            message += (
                " | hint: ->> yields TEXT, so compare it to a quoted string; "
                "use -> when you need JSONB, e.g. config->'AttachedTo' = '[]'::jsonb"
            )
        raise MCPError(message[:400]) from exc
    return json.dumps(rows, default=str)[:8000], "direct-readonly"


def healthcheck() -> dict[str, Any]:
    """Prove a scripted client can connect and read. This is the §2 gate."""
    out: dict[str, Any] = {}
    info = initialize()
    out["server"] = info.get("serverInfo", {}).get("name")
    out["protocol"] = info.get("protocolVersion")
    try:
        rows = query("SELECT count(*) AS n FROM resources")
        out["query_ok"] = True
        out["sample"] = rows[:200]
    except MCPError as exc:
        out["query_ok"] = False
        out["error"] = str(exc)[:300]
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for key, value in healthcheck().items():
        print(f"{key}: {value}")
