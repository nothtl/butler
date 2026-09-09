"""MCP parity smoke test for Butler (OpenClaw integration).

Run:  .venv/bin/python tests/run_acceptance_mcp.py

Builds the real Container, boots the MCP server in-process, and asserts:
  - the stdio JSON-RPC handshake works,
  - the tool catalog contains the planner/course/routine/executive tools,
  - a read-only call returns well-formed output and an unknown tool errors.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def main() -> int:
    server = MCPServer(Container())

    def call(method: str, params: dict | None = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            msg["params"] = params
        return server._handle(msg) or {}

    init = call("initialize", {"clientInfo": {"name": "parity-test"}})
    check("initialize", init["result"]["serverInfo"]["name"] == "butler",
          init["result"]["serverInfo"]["name"])

    tools = call("tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    for name in ("day", "what_now", "tasks", "task_add", "add_course",
                 "drop_course", "course_check", "brief", "review",
                 "routines_list", "scan_routines", "presence", "health",
                 "rewards", "suggest_reward", "context"):
        check(f"tool present: {name}", name in names)

    resp = call("tools/call", {"name": "health", "arguments": {}})
    text = resp["result"]["content"][0]["text"]
    check("health call returns JSON", json.loads(text).get("ready") is not None,
          text[:80])

    resp = call("tools/call", {"name": "nope", "arguments": {}})
    check("unknown tool isError", resp["result"].get("isError") is True)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
