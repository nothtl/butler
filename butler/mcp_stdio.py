"""stdio entrypoint for the Butler MCP server.

Use as the OpenClaw MCP transport:
    openclaw.json -> mcp.servers.butler = {
        "command": "python",
        "args": ["-m", "butler.mcp_stdio"],
        "cwd": "<butler repo>",
    }

Builds the same `Container` as the CLI / Telegram bot and reads JSON-RPC
messages on stdin, writing corresponding responses on stdout. Nothing is
printed to stdout except JSON-RPC responses, so the stdio channel stays clean.

Set ``BUTLER_MCP_PROFILE=readonly`` to expose only the side-effect-free
executive surface (``get_time``, ``get_day``, ``get_tasks``, ...) to an
external agent runtime such as AI Butler. The default ``full`` profile keeps
the historical 51-tool surface unchanged.
"""

from __future__ import annotations

import sys

from .core import Container
from .mcp import MCPServer


def main() -> int:
    container = Container()
    server = MCPServer(container)
    return server.run()


if __name__ == "__main__":
    sys.exit(main())
