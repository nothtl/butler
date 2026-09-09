"""Model Context Protocol server for Butler (feature 22).

Exposes Butler's filesystem intelligence to any MCP client over stdio using a
minimal JSON-RPC 2.0 implementation (no external SDK needed). A client can
initialize, list tools, and call them; the tools talk to the same `Container`
as the CLI / Telegram bot, so remote access stays on one code path.

Safety mirrors the rest of Butler: mutating tools return *plans* for
confirmation rather than applying anything implicitly.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

log = logging.getLogger("butler.mcp")

SERVER_NAME = "butler"
VERSION = "1.0.0"
PROTOCOL = "2024-11-05"


class MCPServer:
    def __init__(self, container: Any):
        self.container = container

    # ------------------------- tool schema -------------------------
    def _tools_spec(self) -> list[dict[str, Any]]:
        def s(name: str, desc: str, props: dict, required: list[str]) -> dict[str, Any]:
            return {
                "name": name,
                "description": desc,
                "inputSchema": {"type": "object", "properties": props, "required": required},
            }

        return [
            s("status", "Butler storage & config overview.", {}, []),
            s("search", "Hybrid (semantic+keyword+fuzzy) search over files.",
              {"query": {"type": "string"}}, ["query"]),
            s("find", "Filename + content keyword search.",
              {"query": {"type": "string"}}, ["query"]),
            s("resume", "Find the most recent likely resume/CV.", {}, []),
            s("list", "List a directory.",
              {"path": {"type": "string"}}, []),
            s("ask", "Answer a question grounded in your indexed files.",
              {"query": {"type": "string"}}, ["query"]),
            s("teach", "Build a lesson/study-guide about a topic from your files.",
              {"topic": {"type": "string"}}, ["topic"]),
            s("dupes", "Detect duplicate files (returns groups, not destructive).",
              {"root": {"type": "string"}}, []),
            s("backup", "Backup status.", {}, []),
            s("organize", "Propose an organize plan for a folder (confirmation required to apply).",
              {"path": {"type": "string"}}, []),
        ]

    # ------------------------- tool invocation -------------------------
    def _call_tool(self, name: str, args: dict[str, Any]) -> Any:
        c = self.container
        a = args or {}
        if name == "status":
            return {"config": c.cfg.to_dict()}
        if name == "search":
            return {"query": a.get("query", ""), "results": c.search.hybrid(a.get("query", ""))}
        if name == "find":
            return {"query": a.get("query", ""), "results": c.search.combined(a.get("query", ""))}
        if name == "resume":
            return {"results": c.search.latest_resume()}
        if name == "list":
            return c.engine.list_dir(a.get("path", "~/Downloads"))
        if name == "ask":
            return {"answer": c.chat.answer(a.get("query", ""))}
        if name == "teach":
            return {"lesson": c.chat.teach(a.get("topic", ""))}
        if name == "dupes":
            return {"root": a.get("root", "~/Downloads"), "groups": c.engine.detect_duplicates(a.get("root", "~/Downloads"))}
        if name == "backup":
            return c.backup.status()
        if name == "organize":
            plan = c.organizer.plan_organize(a.get("path", "~/Downloads"))
            return {"plan_id": plan.plan_id, "title": plan.title, "summary": plan.summary,
                    "items": [{"action": i.action, "src": i.src, "dest": i.dest}
                              for i in plan.items]}
        raise ValueError(f"unknown tool: {name}")

    # ------------------------- json-rpc -------------------------
    def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg.get("method", "")
        mid = msg.get("id")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            client_info = params.get("clientInfo", {})
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                },
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None  # no response to notifications
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"tools": self._tools_spec()}}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            try:
                data = self._call_tool(name, args)
            except Exception as exc:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0", "id": mid,
                    "result": {"isError": True,
                               "content": [{"type": "text", "text": f"error: {exc}"}]},
                }
            text = json.dumps(data, indent=2, default=str)
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}]}}
        if mid is not None:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        return None

    def run(self) -> int:
        """Read JSON-RPC messages from stdin and write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            try:
                out = self._handle(msg)
            except Exception as exc:  # noqa: BLE001
                out = {"jsonrpc": "2.0", "id": msg.get("id"),
                       "error": {"code": -32603, "message": str(exc)}}
            if out is not None:
                sys.stdout.write(json.dumps(out) + "\n")
                sys.stdout.flush()
        return 0
