"""Model Context Protocol server for Butler (feature 22).

Exposes Butler's filesystem intelligence to any MCP client over stdio using a
minimal JSON-RPC 2.0 implementation (no external SDK needed). A client can
initialize, list tools, and call them; the tools talk to the same `Container`
as the CLI / Telegram bot, so remote access stays on one code path.

Safety mirrors the rest of Butler: mutating tools return *plans* for
confirmation rather than applying anything implicitly.

Phase 7 / M2: the tool catalog is no longer hand-maintained here. Each tool is
defined once in :mod:`butler.agent.mcp_tools` as a typed ``Tool``; this server
derives both the ``tools/list`` schema and the dispatch from that registry, so
the schema and the implementation can never drift apart.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from .agent.mcp_tools import build_mcp_registry

log = logging.getLogger("butler.mcp")

SERVER_NAME = "butler"
VERSION = "1.4.0"
PROTOCOL = "2024-11-05"
DEFAULT_PROFILE = "full"


class MCPServer:
    def __init__(self, container: Any, profile: str | None = None):
        self.container = container
        self.profile = profile or os.environ.get(
            "BUTLER_MCP_PROFILE", DEFAULT_PROFILE)
        self._registry = build_mcp_registry(container)

    # ------------------------- tool schema -------------------------
    def _tools_spec(self) -> list[dict[str, Any]]:
        return self._registry.mcp_schema(self.profile)

    # ------------------------- tool invocation -------------------------
    def _call_tool(self, name: str, args: dict[str, Any]) -> Any:
        tool = self._registry.find_mcp(name, profile=self.profile)
        if tool is None:
            raise ValueError(
                f"unknown tool for profile {self.profile!r}: {name}")
        # MCP calls stay lenient (no schema validation) to preserve the exact
        # behaviour existing clients relied on before M2.
        return tool.handler(dict(args or {}))

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
