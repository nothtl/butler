"""Phase 7 acceptance tests: M2 unified Intent + registry migration.

Run:  python tests/run_acceptance_p71.py

Covers the M2 deliverables:
  UNIFIED INTENT (1-6)   REGISTRY EXTENSIONS (7-14)
  DECIDER BRIDGE (15-20) MCP CATALOG (21-28)
  TELEGRAM ROUTING (29-31)  SINGLE GATE (32-36)

TZ is pinned to UTC so all arithmetic is deterministic on any host.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

os.environ["TZ"] = "UTC"
try:
    time.tzset()  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.agent import Intent, Tool, ToolRegistry  # noqa: E402
from butler.agent.models import Intent as AgentIntent  # noqa: E402
from butler.agent.mcp_tools import build_mcp_registry  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def section(title):
    print(f"\n=== {title} ===")


def _mk(base):
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    return Container(cfg)


# =================================================================
section("Unified Intent model")
c = _mk(tempfile.mkdtemp(prefix="p71-"))
i = c.decider.parse("tasks", channel="telegram")
check(1, isinstance(i, AgentIntent), "decider emits the canonical Intent type")
check(2, i.channel == "telegram", "channel travels with the intent")
check(3, i.needs_confirmation is False, "needs_confirmation defaults false")
i.needs_confirmation = True
check(4, Intent.from_dict(i.to_dict()) == i,
      "channel + needs_confirmation round-trip through dict")
legacy = type("L", (), {"kind": "tasks", "target": "", "query": "",
                       "params": {}, "raw": "tasks", "scope": ""})()
check(5, Intent.from_legacy(legacy).kind == "tasks", "from_legacy still works")
check(6, c.decider.parse("tasks").channel == "", "channel defaults empty")

# =================================================================
section("Registry extensions")
reg = ToolRegistry()
reg.add("plan_tasks", "plan tasks", lambda a: {"ok": True}, [],
        aliases=("plan", "schedule"), mcp_name="day")
check(7, reg.get("plan").name == "plan_tasks", "alias resolves to tool")
check(8, reg.has("schedule") and reg.has("plan_tasks"), "has() is alias-aware")
check(9, reg.find_mcp("day").name == "plan_tasks", "find_mcp by mcp_name")
check(10, reg.find_mcp("missing") is None, "find_mcp unknown -> None")
reg.add("secret", "hidden", lambda a: {}, [], hidden=True)
check(11, "secret" not in [t["name"] for t in reg.schema()],
      "hidden tools excluded from schema")
check(12, "secret" not in [t["name"] for t in reg.mcp_schema()],
      "hidden tools excluded from mcp_schema")
other = ToolRegistry()
other.add("inner", "inner", lambda a: 1, [], mcp_name="inner")
reg.merge(other)
check(13, reg.has("inner"), "merge adds non-colliding tools")
collide = ToolRegistry()
collide.add("plan_tasks", "clash", lambda a: 2, [], mcp_name="clash")
reg.merge(collide, prefix="m_")
check(14, reg.get("m_plan_tasks").mcp_name == "clash",
      "merge prefixes colliding names and preserves mcp_name")

# =================================================================
section("Decider bridge equivalence")
READ_MSGS = [
    "my courses", "tasks", "expiring food", "routines", "brief", "review",
    "help", "status", "storage", "my context",
]
ok_all = True
kinds = []
for msg in READ_MSGS:
    try:
        d = c.decider.resolve(c.decider.parse(msg), user="u")
        a = c.agent.run_intent(c.decider.parse(msg), user="u")
        kinds.append((msg, c.decider.parse(msg).kind))
        if not (a.ok and a.data == d):
            ok_all = False
            print(f"    mismatch for {msg!r}: decider={d!r} agent={a.data!r}")
    except Exception as exc:  # noqa: BLE001
        ok_all = False
        print(f"    error for {msg!r}: {exc}")
check(15, ok_all, f"agent bridge matches decider for {len(READ_MSGS)} reads")
check(16, all(c.decider.parse(m).kind for m, _ in kinds),
      "every message parsed to a kind")
a = c.agent.run_intent(c.decider.parse("tasks"), user="u")
check(17, a.tool_calls and a.tool_calls[0].name == "decider",
      "deterministic intent routed through the decider bridge")
check(18, c.agent.registry.has("decider")
      and c.agent.registry.get("decider").delegated_gate,
      "bridge tool is registered with delegated_gate")
check(19, "decider" not in [t["name"] for t in c.agent.tools()],
      "bridge tool hidden from the LLM-facing schema")
check(20, isinstance(c.agent.run_tool("tasks").data, list),
      "typed tools keep their original return shape")

# =================================================================
section("MCP catalog from registry")
srv = MCPServer(c)
names = [t["name"] for t in srv._tools_spec()]
EXPECTED = {
    "status", "search", "find", "resume", "list", "ask", "teach", "dupes",
    "backup", "organize", "courses", "course_documents", "pantry", "expiring",
    "recipe", "grocery", "recipe_search", "recipes", "favorites",
    "recipe_history", "rate_recipe", "context", "presence", "health", "day",
    "week", "course_sync", "what_now", "why", "tasks", "task_add",
    "task_start", "task_done", "task_skip", "task_defer", "task_block",
    "task_cancel", "came_up", "reschedule", "undo", "add_course", "drop_course",
    "course_check", "brief", "review", "routines_list", "scan_routines",
    "routines_confirm", "routines_reject", "rewards", "suggest_reward",
}
check(21, len(names) == 51, f"MCP exposes 51 tools (got {len(names)})")
check(22, set(names) == EXPECTED, "MCP tool names unchanged from pre-M2")
check(23, all(srv._registry.find_mcp(n) is not None for n in names),
      "every MCP tool resolves to one registry definition")
json.dumps(srv._tools_spec())
check(24, True, "MCP schema is JSON-serialisable")
check(25, srv._call_tool("status", {}) == {"config": c.cfg.to_dict()},
      "MCP dispatch matches the old status handler")
check(26, "tasks" in srv._call_tool("tasks", {}),
      "MCP tasks keeps its wrapper shape")
try:
    srv._call_tool("nope_missing", {})
    check(27, False, "unknown MCP tool must raise")
except ValueError:
    check(27, True, "unknown MCP tool raises ValueError")
check(28, srv._registry.names() and all(
    n.startswith("mcp_") for n in srv._registry.names()),
    "MCP registry uses mcp_-prefixed internal names")

# =================================================================
section("Telegram NL routing")
from butler.telebot import TelegramBot  # noqa: E402

bot = object.__new__(TelegramBot)
bot.container = c
it = c.decider.parse("tasks", channel="telegram")
routed = asyncio.run(bot._resolve(it, "u", via_agent=True))
direct = c.decider.resolve(c.decider.parse("tasks"), user="u")
check(29, routed == direct, "NL via_agent returns the decider result")
it2 = c.decider.parse("tasks", channel="telegram")
slash = asyncio.run(bot._resolve(it2, "u", via_agent=False))
check(30, slash == direct, "slash path still uses the decider directly")
check(31, asyncio.iscoroutinefunction(bot._resolve),
      "_resolve is async (awaitable from _dispatch)")

# =================================================================
section("Single authoritative gate")
c2 = _mk(tempfile.mkdtemp(prefix="p71-gate-"))
mg = tempfile.mkdtemp(prefix="p71-org-")
c2.cfg.roots = [mg]
c2.engine.allowed.append(os.path.realpath(mg))
# confirmation-required action still defers (no side effect, no double gate)
d = c2.agent.run_intent(c2.decider.parse(f"organize {mg}"), user="u").data
check(32, not (isinstance(d, dict) and d.get("decision") in ("denied", "refused")),
      "bridge preserves organize confirm-deferral")
# offline mode denies an external action through the bridge too
c2.cfg.offline_mode = True
d2 = c2.agent.run_intent(
    c2.decider.parse(f"delete duplicate files in {mg}"), user="u").data
check(33, isinstance(d2, dict) and d2.get("decision") == "denied",
      "offline external denied through the bridge")
c2.cfg.offline_mode = False
# a read still works after the gate
check(34, c2.agent.run_intent(c2.decider.parse("tasks"), user="u").ok,
      "read allowed through the bridge")
# delegated tool must NOT be audited/gated a second time
calls = {"n": 0}


def _probe(a):
    calls["n"] += 1
    return {"ok": True, "n": calls["n"]}


c2.agent.registry.add("probe", "probe", _probe, [], action="organize",
                      side_effect=True, delegated_gate=True, hidden=True)
pr = c2.agent.run_tool("probe", user="u")
check(35, pr.ok and pr.results[0].decision == "allowed"
      and pr.results[0].meta.get("pending_id") is None,
      "delegated tool bypasses the runtime gate (single boundary)")
check(36, not c2.decider.parse("tasks").needs_confirmation,
      "read intent not marked confirmation-required")

print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
