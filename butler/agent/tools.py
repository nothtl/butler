"""Phase 7 / M1: adapters that expose existing subsystems as typed tools.

This module is the bridge between the old direct-call world (``Decider.resolve``,
``mcp._call_tool``, Telegram handlers) and the new :class:`ToolRegistry`. Every
handler calls an existing subsystem method — no business logic is duplicated and
no side effect is invented here. The runtime still gates and audits each call.

The registry built by :func:`build_default_registry` is intentionally a
representative, high-value subset; M2 migrates the remaining ``Decider`` intents
onto the same surface.
"""

from __future__ import annotations

from typing import Any

from .registry import Param, ToolRegistry


def P(name: str, type: str = "str", *, required: bool = False,
      default: Any = None, description: str = "",
      choices: tuple[str, ...] = ()) -> Param:
    return Param(name=name, type=type, required=required, default=default,
                 description=description, choices=choices)


def _require(container: Any, attr: str) -> Any:
    obj = getattr(container, attr, None)
    if obj is None:
        raise RuntimeError(f"subsystem '{attr}' is not available")
    return obj


def build_default_registry(container: Any) -> ToolRegistry:
    reg = ToolRegistry()

    # ------------------------------------------------------------ read tools
    reg.add(
        "day", "Show the deterministic plan for today.",
        lambda a: _require(container, "planner").plan_day(),
        [P("day_ts", "int", description="Optional day timestamp")],
        action="day", returns="plan",
    )
    reg.add(
        "week", "Show the plan for the next N days (read-only preview).",
        lambda a: _require(container, "planner").plan_week(days=int(a.get("days", 7))),
        [P("days", "int", default=7, description="Number of days")],
        action="day", returns="plan",
    )
    reg.add(
        "now", "Recommend what to work on right now.",
        lambda a: _recommend(container, a.get("query", "")),
        [P("query", "str", description="Optional free-text context")],
        action="now", returns="plan",
    )
    reg.add(
        "why", "Explain the most recent scheduling decision.",
        lambda a: _require(container, "planner").why(),
        action="why", returns="explanation",
    )
    reg.add(
        "tasks", "List active tasks.",
        lambda a: [dict(r) for r in _require(container, "db").tasks("active")],
        action="tasks", returns="rows",
    )
    reg.add(
        "context", "Return the live deterministic context snapshot.",
        lambda a: _require(container, "context").snapshot(),
        action="context", returns="context",
    )
    reg.add(
        "briefing", "Produce today's briefing.",
        lambda a: _require(container, "executive").briefing(),
        action="briefing", returns="text",
    )
    reg.add(
        "review", "Produce today's review.",
        lambda a: _require(container, "executive").review(),
        action="review", returns="text",
    )
    reg.add(
        "status", "Return the effective configuration.",
        lambda a: _require(container, "cfg").to_dict(),
        action="status", returns="config",
    )
    reg.add(
        "find", "Search the user's indexed files.",
        lambda a: _require(container, "search").combined(a.get("query", "")),
        [P("query", "str", required=True, description="Search query")],
        action="find", returns="rows",
    )
    reg.add(
        "course_list", "List tracked courses.",
        lambda a: _require(container, "courses").list_courses(),
        action="course_list", returns="rows",
    )
    reg.add(
        "grocery", "List the shopping list.",
        lambda a: [dict(r) for r in _require(container, "db").shopping(0)],
        action="grocery", returns="rows",
    )
    reg.add(
        "food_list", "List the food inventory.",
        lambda a: _require(container, "food").all(),
        action="food_list", returns="rows",
    )
    reg.add(
        "food_expiring", "List food expiring within N days.",
        lambda a: _require(container, "food").expiring(int(a.get("days", 3))),
        [P("days", "int", default=3, description="Window in days")],
        action="food_expiring", returns="rows",
    )
    reg.add(
        "routines", "Show active routines and candidates.",
        lambda a: {"active": _require(container, "routines").active(),
                   "candidates": _require(container, "routines").candidates()},
        action="context", returns="rows",
    )
    reg.add(
        "ask", "Answer a question from live state and indexed files.",
        lambda a: _answer(container, a.get("query", "")),
        [P("query", "str", required=True, description="Question")],
        action="chat", returns="text",
    )
    reg.add(
        "chat", "Conversational reply grounded in live state.",
        lambda a: _chat(container, a.get("query", "")),
        [P("query", "str", required=True, description="User message")],
        action="chat", returns="text",
    )

    # --------------------------------------------------- low-risk write tools
    reg.add(
        "add_task", "Add a task.",
        lambda a: _add_task(container, a),
        [P("title", "str", required=True),
         P("est_minutes", "int", default=60),
         P("deadline", "int", default=0),
         P("priority", "int", default=3),
         P("tags", "str", default="")],
        action="add_task", side_effect=True, returns="task_id",
    )
    reg.add("start", "Mark a task as in progress.",
            lambda a: _require(container, "planner").start(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="task_lifecycle", side_effect=True)
    reg.add("done", "Mark a task complete.",
            lambda a: _require(container, "planner").done(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="mark_done", side_effect=True)
    reg.add("skip", "Skip a task.",
            lambda a: _require(container, "planner").skip(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="skip", side_effect=True)
    reg.add("defer", "Defer a task.",
            lambda a: _require(container, "planner").defer(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="defer", side_effect=True)
    reg.add("block", "Mark a task blocked.",
            lambda a: _require(container, "planner").block_task(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="block", side_effect=True)
    reg.add("cancel", "Cancel a task.",
            lambda a: _require(container, "planner").cancel_task(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="cancel_task", side_effect=True)
    reg.add("resume", "Resume a task.",
            lambda a: _require(container, "planner").resume_task(int(a["task_id"])),
            [P("task_id", "int", required=True)],
            action="resume", side_effect=True)
    reg.add(
        "came_up", "Record an urgent task that just came up.",
        lambda a: _require(container, "planner").came_up(
            a["title"], est_minutes=int(a.get("est_minutes", 60)),
            deadline=int(a.get("deadline", 0)),
            priority=int(a.get("priority", 4))),
        [P("title", "str", required=True),
         P("est_minutes", "int", default=60),
         P("deadline", "int", default=0),
         P("priority", "int", default=4)],
        action="add_task", side_effect=True,
    )
    reg.add(
        "food_add", "Add a food item to the inventory.",
        lambda a: _require(container, "food").add(
            a["name"], quantity=float(a.get("quantity", 1.0)),
            unit=a.get("unit", "")),
        [P("name", "str", required=True),
         P("quantity", "float", default=1.0),
         P("unit", "str", default="")],
        action="food_add", side_effect=True,
    )
    reg.add(
        "food_consume", "Consume a food item.",
        lambda a: _require(container, "food").consume(a["name"]),
        [P("name", "str", required=True)],
        action="food_consume", side_effect=True,
    )

    # ------------------------------------------- consequent-external tools
    reg.add(
        "index", "Reindex the configured roots.",
        lambda a: _index(container),
        action="index", side_effect=True, returns="stats",
    )
    reg.add(
        "gcal_sync", "Reconcile Butler's tasks with Google Calendar.",
        lambda a: _require(container, "planner").sync_calendar(),
        action="gcal_sync", side_effect=True,
    )
    reg.add(
        "course_check", "Check monitored courses for new documents.",
        lambda a: _require(container, "courses").check_all(),
        action="course_monitor", side_effect=True,
    )
    reg.add(
        "course_assignments", "Extract and sync a course's assignments.",
        lambda a: _require(container, "courses").sync_page_assignments(
            a["code"], force=bool(a.get("force", False))),
        [P("code", "str", required=True),
         P("force", "bool", default=False)],
        action="course_monitor", side_effect=True,
    )
    reg.add(
        "mkdir", "Create a directory inside the managed roots.",
        lambda a: _require(container, "engine").mkdir(a["path"]),
        [P("path", "str", required=True)],
        action="mkdir", side_effect=True,
    )
    reg.add(
        "nas_ingest", "Ingest the NAS inbox into the library.",
        lambda a: _require(container, "nas").ingest_inbox(),
        action="nas_ingest", side_effect=True,
    )
    return reg


# ---------------------------------------------------------------- helpers
def _recommend(container: Any, message: str) -> Any:
    executive = getattr(container, "executive", None)
    if executive is not None and hasattr(executive, "recommend"):
        return executive.recommend(message)
    return _require(container, "planner").what_now(message)


def _answer(container: Any, query: str) -> str:
    chat = _require(container, "chat")
    ctx = getattr(container, "context", None)
    live = ctx.describe() if ctx is not None else ""
    return chat.answer(query, live_context=live)


def _chat(container: Any, message: str) -> Any:
    chat = _require(container, "chat")
    ctx = getattr(container, "context", None)
    live = ctx.describe() if ctx is not None else ""
    text = chat.converse(message, live_context=live)
    if text is None:
        return live
    return text


def _add_task(container: Any, a: dict[str, Any]) -> Any:
    return _require(container, "planner").add_task(
        a["title"], est_minutes=int(a.get("est_minutes", 60)),
        deadline=int(a.get("deadline", 0)),
        priority=int(a.get("priority", 3)),
        tags=a.get("tags", ""))


def _index(container: Any) -> dict[str, Any]:
    cfg = _require(container, "cfg")
    indexer = _require(container, "indexer")
    stats: dict[str, Any] = {}
    for root in list(cfg.roots) + list(cfg.index_roots):
        stats[root] = indexer.index_root(root)
    stats["pruned"] = indexer.prune_missing()
    return stats
