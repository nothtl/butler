"""M1 acceptance: AI Butler integration spike + read-only MCP boundary.

Run:  .venv/bin/python tests/run_acceptance_mcp_readonly.py

Proves three things:
  1. The default ``full`` profile is unchanged (the historical 51 tools).
  2. The ``readonly`` profile exposes exactly the side-effect-free executive
     surface (``get_time`` ... ``get_week``) and nothing else.
  3. The wire format matches AI Butler's MCP *client* exactly (verified against
     its Go source): int ids, ``2024-11-05`` handshake, no
     ``notifications/initialized`` requirement, ``tools/list`` entries with
     ``name``/``description``/``inputSchema``, and ``tools/call`` results as
     ``content[]`` text blocks with an optional ``isError``.

The read-only handlers are also asserted to leave persistent state untouched.
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

READONLY_TOOLS = {
    "get_time", "get_context", "get_day", "plan_day", "get_schedule",
    "get_tasks", "get_courses", "get_projects", "get_project",
    "get_project_workload", "get_project_risk", "get_project_dependencies",
    "find_available_time", "get_week", "web_search", "web_research",
    "web_fetch", "knowledge_lookup", "optimize_day", "optimize_week",
    "evaluate_schedule", "find_best_slot", "memory_search",
    "memory_get_relevant", "memory_list", "memory_history",
    "get_proactive_candidates", "get_proactive_status",
    "explain_proactive_candidate",
    "get_trackers", "get_tracker", "evaluate_tracker",
    "executive_ask",
}


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def _call(server: MCPServer, method: str, params: dict | None = None,
          mid: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return server._handle(msg) or {}


def _call_tool(server: MCPServer, name: str, args: dict | None = None) -> dict:
    resp = _call(server, "tools/call", {"name": name, "arguments": args or {}})
    return resp.get("result", {})


def main() -> int:
    container = Container()
    container.planner._maybe_sync = lambda: None  # keep the test offline

    # ---------------------------------------------------------- full profile
    full = MCPServer(container, profile="full")
    full_names = {t["name"] for t in full._tools_spec()}
    check("full profile has 51 tools", len(full_names) == 51, str(len(full_names)))
    check("full profile excludes read-only tools", "get_time" not in full_names)
    check("full profile keeps mutating tools", "task_add" in full_names)

    # ------------------------------------------------------ readonly profile
    ro = MCPServer(container, profile="readonly")
    ro_names = {t["name"] for t in ro._tools_spec()}
    check("readonly profile exposes exactly the executive surface",
          ro_names == READONLY_TOOLS, f"{len(ro_names)} tools")
    check("readonly profile hides mutating tools",
          not (ro_names & {"task_add", "day", "reschedule", "organize"}))

    # env selects the profile when none is passed
    os.environ["BUTLER_MCP_PROFILE"] = "readonly"
    try:
        env_server = MCPServer(container)
        check("BUTLER_MCP_PROFILE selects readonly",
              {t["name"] for t in env_server._tools_spec()} == READONLY_TOOLS)
    finally:
        os.environ.pop("BUTLER_MCP_PROFILE", None)

    # ------------------------------------------- AI Butler wire compatibility
    init = _call(ro, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "aibutler", "version": "0.1.0"},
    }, mid=1)
    result = init.get("result", {})
    check("handshake echoes 2024-11-05",
          result.get("protocolVersion") == "2024-11-05")
    check("handshake identifies butler",
          result.get("serverInfo", {}).get("name") == "butler")
    check("handshake advertises tools capability",
          "tools" in result.get("capabilities", {}))
    check("AI Butler's missing notifications/initialized is tolerated",
          ro._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
          is None)

    tools = _call(ro, "tools/list", {}, mid=2)["result"]["tools"]
    check("tools/list entries carry name/description/inputSchema",
          all({"name", "description", "inputSchema"} <= set(t)
              for t in tools))
    check("tools/list inputSchema is an object",
          all(t["inputSchema"].get("type") == "object" for t in tools))

    # ------------------------------------------- read-only calls return data
    before_plan = container.db.latest_plan()
    before_plan_id = int(before_plan["id"]) if before_plan else None
    before_tasks = len(container.db.tasks("active"))

    resp = _call_tool(ro, "get_time")
    payload = json.loads(resp["content"][0]["text"])
    check("get_time returns content text block",
          resp["content"][0]["type"] == "text")
    check("get_time has date/time/timezone",
          payload.get("date") and payload.get("time")
          and payload.get("timezone"))
    check("get_time day window is 24h",
          payload["day_end"] - payload["day_start"] == 86400)

    ctx = json.loads(_call_tool(ro, "get_context")["content"][0]["text"])
    check("get_context returns now", "now" in ctx)

    tasks = json.loads(_call_tool(ro, "get_tasks")["content"][0]["text"])
    check("get_tasks returns tasks list", isinstance(tasks.get("tasks"), list))

    courses = json.loads(_call_tool(ro, "get_courses")["content"][0]["text"])
    check("get_courses returns courses list",
          isinstance(courses.get("courses"), list))

    projects = json.loads(_call_tool(ro, "get_projects")["content"][0]["text"])
    check("get_projects returns a projects list",
          projects.get("ok") is True
          and isinstance(projects.get("projects"), list))
    missing = json.loads(
        _call_tool(ro, "get_project", {"project": "no-such-project"})
        ["content"][0]["text"])
    check("get_project reports a missing project",
          missing.get("ok") is False and "error" in missing)
    risk = json.loads(_call_tool(ro, "get_project_risk")["content"][0]["text"])
    check("get_project_risk returns without projects",
          risk.get("ok") is True and "most_at_risk" in risk)

    free = json.loads(_call_tool(ro, "find_available_time")["content"][0]["text"])
    check("find_available_time returns intervals + total",
          isinstance(free.get("intervals"), list)
          and isinstance(free.get("total_minutes"), int))
    check("find_available_time respects min_minutes",
          all(i["minutes"] >= 600
              for i in json.loads(_call_tool(
                  ro, "find_available_time", {"min_minutes": 600}
              )["content"][0]["text"])["intervals"]))

    week = json.loads(_call_tool(ro, "get_week", {"days": 2})["content"][0]["text"])
    check("get_week honours days", week.get("count") == 2, str(week.get("count")))

    sched = json.loads(_call_tool(ro, "get_schedule")["content"][0]["text"])
    check("get_schedule matches committed-plan state",
          sched.get("committed") is (before_plan_id is not None))
    if before_plan_id is not None:
        check("get_schedule returns the committed plan",
              isinstance(sched.get("plan"), dict))
    else:
        check("get_schedule reports no committed plan",
              sched.get("plan") is None)

    preview = json.loads(_call_tool(ro, "plan_day")["content"][0]["text"])
    check("plan_day is an uncommitted preview",
          preview.get("committed") is False
          and preview.get("source") == "preview")
    day = json.loads(_call_tool(ro, "get_day")["content"][0]["text"])
    expected_source = "committed" if before_plan_id is not None else "preview"
    check("get_day source matches committed-plan state",
          day.get("source") == expected_source, day.get("source"))

    # ------------------------------------------------ read-only enforcement
    err = _call_tool(ro, "task_add", {"title": "must not exist"})
    check("mutating tool rejected on readonly profile",
          err.get("isError") is True)
    err2 = _call_tool(ro, "day")
    check("full-profile planner tool rejected on readonly profile",
          err2.get("isError") is True)

    # --------------------------------------------------- no persistent effects
    after_plan = container.db.latest_plan()
    after_plan_id = int(after_plan["id"]) if after_plan else None
    check("read-only calls left the active plan untouched",
          after_plan_id == before_plan_id)
    check("read-only calls left tasks untouched",
          len(container.db.tasks("active")) == before_tasks)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
