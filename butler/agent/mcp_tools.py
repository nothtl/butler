"""Phase 7 / M2: MCP tool catalog expressed as typed :class:`Tool` entries.

Before M2 the MCP server kept two hand-maintained parallel lists — the
``_tools_spec`` JSON schema and the ``_call_tool`` dispatch ``if`` ladder — that
drifted apart. This module defines each MCP tool exactly once as a
:class:`~butler.agent.registry.Tool`; :mod:`butler.mcp` derives both the
``tools/list`` schema and the dispatch from that single definition.

MCP tools keep their historical client-facing names via ``Tool.mcp_name`` and
are given an internal ``mcp_`` prefix so they can coexist with the agent's
typed tools in one catalog without name collisions. Handlers call the exact
same subsystems ``_call_tool`` used to, so existing MCP clients see no change.
"""

from __future__ import annotations

from typing import Any

from .registry import Param, ToolRegistry
from .tools import P


def build_mcp_registry(container: Any) -> ToolRegistry:
    c = container
    reg = ToolRegistry()

    def add(name: str, desc: str, handler: Any,
            params: list[Param] | None = None, *, action: str = "",
            side_effect: bool = False) -> None:
        reg.add("mcp_" + name, desc, handler, params or [],
                action=action or name, side_effect=side_effect, mcp_name=name)

    # ------------------------------------------------------------- inspect
    add("status", "Butler storage & config overview.",
        lambda a: {"config": c.cfg.to_dict()}, action="status")
    add("search", "Hybrid (semantic+keyword+fuzzy) search over files.",
        lambda a: {"query": a.get("query", ""),
                   "results": c.search.hybrid(a.get("query", ""))},
        [P("query", "str", required=True)], action="find")
    add("find", "Filename + content keyword search.",
        lambda a: {"query": a.get("query", ""),
                   "results": c.search.combined(a.get("query", ""))},
        [P("query", "str", required=True)], action="find")
    add("resume", "Find the most recent likely resume/CV.",
        lambda a: {"results": c.search.latest_resume()}, action="resume")
    add("list", "List a directory.",
        lambda a: c.engine.list_dir(a.get("path", "~/Downloads")),
        [P("path", "str", default="~/Downloads")], action="list")
    add("ask", "Answer a question grounded in your indexed files.",
        lambda a: {"answer": c.chat.answer(a.get("query", ""))},
        [P("query", "str", required=True)], action="chat")
    add("teach", "Build a lesson/study-guide about a topic from your files.",
        lambda a: {"lesson": c.chat.teach(a.get("topic", ""))},
        [P("topic", "str", required=True)], action="teach")
    add("dupes", "Detect duplicate files (returns groups, not destructive).",
        lambda a: {"root": a.get("root", "~/Downloads"),
                   "groups": c.engine.detect_duplicates(
                       a.get("root", "~/Downloads"))},
        [P("root", "str", default="~/Downloads")], action="dupes")
    add("backup", "Backup status.", lambda a: c.backup.status(),
        action="backup")
    add("organize", "Propose an organize plan for a folder (confirmation "
        "required to apply).",
        lambda a: _organize(c, a.get("path", "~/Downloads")),
        [P("path", "str", default="~/Downloads")], action="organize",
        side_effect=True)
    add("courses", "List tracked courses.",
        lambda a: {"courses": c.courses.list_courses()}, action="course_list")
    add("course_documents", "List downloaded materials for a course code.",
        lambda a: _course_documents(c, a.get("code", "")),
        [P("code", "str", required=True)], action="course_docs")
    add("pantry", "List current food inventory.",
        lambda a: {"items": c.food.all()}, action="food_list")
    add("expiring", "List food expiring within N days (default 3).",
        lambda a: {"items": c.food.expiring(int(a.get("days", 3)))},
        [P("days", "int", default=3)], action="food_expiring")
    add("recipe", "Suggest a meal that fits the free-time budget and "
        "ingredients.",
        lambda a: c.chef.plan_meal(
            budget_minutes=int(a.get("budget_minutes", 45))),
        [P("budget_minutes", "int", default=45)], action="recipe")
    add("grocery", "Deterministic grocery list (deduped shortfalls + low "
        "stock).",
        lambda a: {"items": c.chef.grocery_list()}, action="grocery")
    add("recipe_search", "Search real online recipes by keyword, then persist "
        "to Library.",
        lambda a: {"query": a.get("query", ""),
                   "results": c.chef.search(a.get("query", ""))},
        [P("query", "str")], action="recipe_search")
    add("recipes", "List the Recipe Library (persisted, rate-able).",
        lambda a: {"count": len(c.chef.library()),
                   "recipes": c.chef.library()}, action="recipe_library")
    add("favorites", "List favorited recipes.",
        lambda a: {"recipes": c.chef.favorites()}, action="favorites")
    add("recipe_history", "Recently cooked/planned meals.",
        lambda a: {"history": c.chef.history()}, action="recipe_history")
    add("rate_recipe", "Rate a recipe 0-5 by id or name.",
        lambda a: _rate_recipe(c, a), action="recipe_mark", side_effect=True)
    add("context", "Personal context snapshot (free time, deadlines, expiring "
        "food).",
        lambda a: c.context.snapshot(), action="context")
    add("presence", "Home presence/location from Home Assistant (if "
        "configured).",
        lambda a: _presence(c), action="where_am_i")
    add("health", "Butler subsystem health + run mode.",
        lambda a: {"ready": c.health.ready(),
                   "liveness": c.health.liveness(), **c.health.status()},
        action="health")

    # ------------------------------------------------------------- planner
    add("day", "Compute the schedule plan for today (read-only preview).",
        lambda a: c.planner.plan_day(), action="day")
    add("week", "Compute a read-only schedule preview for the next N days.",
        lambda a: c.planner.plan_week(days=int(a.get("days", 7) or 7)),
        [P("days", "int", default=7)], action="day")
    add("course_sync", "Re-check tracked courses: scrape due dates and import "
        "class-time blocks from their calendar feed.",
        lambda a: _course_sync(c), action="course_monitor", side_effect=True)
    add("what_now", "What Butler recommends you do right now.",
        lambda a: c.planner.what_now(), action="now")
    add("why", "Explain why the schedule changed (last plan diff).",
        lambda a: c.planner.why(), action="why")
    add("tasks", "List active tasks.",
        lambda a: {"tasks": [dict(r) for r in c.db.tasks("active")]},
        action="tasks")
    add("task_add", "Add a task (appears in planning / scheduling).",
        lambda a: _task_add(c, a),
        [P("title", "str", required=True), P("detail", "str"),
         P("est_minutes", "int", default=60), P("deadline", "int", default=0),
         P("priority", "int", default=3), P("tags", "str")],
        action="add_task", side_effect=True)
    add("task_start", "Move a task to doing (state transition).",
        lambda a: c.planner.start(int(a.get("task_id", 0))),
        [P("task_id", "int", required=True)], action="task_lifecycle",
        side_effect=True)
    add("task_done", "Complete a task.",
        lambda a: c.planner.done(int(a.get("task_id", 0))),
        [P("task_id", "int", required=True)], action="mark_done",
        side_effect=True)
    add("task_skip", "Skip a task.",
        lambda a: c.planner.skip(int(a.get("task_id", 0))),
        [P("task_id", "int", required=True)], action="skip", side_effect=True)
    add("task_defer", "Defer a task.",
        lambda a: c.planner.defer(int(a.get("task_id", 0))),
        [P("task_id", "int", required=True)], action="defer", side_effect=True)
    add("task_block", "Mark a task blocked.",
        lambda a: c.planner.block_task(int(a.get("task_id", 0))),
        [P("task_id", "int", required=True)], action="block", side_effect=True)
    add("task_cancel", "Cancel a task.",
        lambda a: c.planner.cancel_task(int(a.get("task_id", 0))),
        [P("task_id", "int", required=True)], action="cancel_task",
        side_effect=True)
    add("came_up", "An unplanned urgent thing appeared: add it and re-plan.",
        lambda a: c.planner.came_up(
            a.get("title", ""), est_minutes=int(a.get("est_minutes", 60)),
            deadline=int(a.get("deadline", 0)),
            priority=int(a.get("priority", 4))),
        [P("title", "str", required=True), P("est_minutes", "int", default=60),
         P("deadline", "int", default=0), P("priority", "int", default=4)],
        action="add_task", side_effect=True)
    add("reschedule", "Re-solve the planner for the full day.",
        lambda a: c.planner.reschedule(), action="reschedule", side_effect=True)
    add("undo", "Undo the last schedule change.",
        lambda a: c.planner.undo(), action="undo", side_effect=True)
    add("add_course", "Track a new course (creates dir + monitors files).",
        lambda a: c.courses.add_course(
            a.get("code", ""), name=a.get("name", ""), url=a.get("url", ""),
            platform=a.get("platform", ""), semester=a.get("semester", "")),
        [P("code", "str", required=True), P("name", "str"), P("url", "str"),
         P("platform", "str"), P("semester", "str")],
        action="course_add", side_effect=True)
    add("drop_course", "Stop tracking a course.",
        lambda a: c.courses.remove_course(a.get("code", "")),
        [P("code", "str", required=True)], action="course_drop",
        side_effect=True)
    add("course_check", "Check all tracked courses for new "
        "materials/assignments.",
        lambda a: {"updates": c.courses.check_all()}, action="course_monitor",
        side_effect=True)
    add("brief", "Morning/standup executive briefing (tasks, deadlines, "
        "risks).",
        lambda a: {"briefing": c.executive.briefing()}, action="briefing")
    add("review", "End-of-day executive review (what got done).",
        lambda a: {"review": c.executive.review()}, action="review")
    add("routines_list", "Active + candidate routines.",
        lambda a: {"active": c.routines.active(),
                   "candidates": c.routines.candidates()}, action="context")
    add("scan_routines", "Detect candidate routines from history.",
        lambda a: c.routines.scan(), action="routine_change", side_effect=True)
    add("routines_confirm", "Confirm a candidate routine.",
        lambda a: c.routines.confirm(a.get("routine_id")),
        [P("routine_id", "int")], action="routine_change", side_effect=True)
    add("routines_reject", "Reject a candidate routine.",
        lambda a: c.routines.reject(a.get("routine_id")),
        [P("routine_id", "int")], action="routine_change", side_effect=True)
    add("rewards", "List the reward library (little treats after tasks).",
        lambda a: {"rewards": [dict(r) for r in c.db.rewards()]},
        action="rewards")
    add("suggest_reward", "Suggest a reward scaled to a task's effort.",
        lambda a: _suggest_reward(c, a),
        [P("task_id", "int"), P("est_minutes", "int"), P("priority", "int")],
        action="rewards")

    return reg


# ---------------------------------------------------------------- helpers
def _organize(c: Any, path: str) -> dict[str, Any]:
    plan = c.organizer.plan_organize(path)
    return {"plan_id": plan.plan_id, "title": plan.title,
            "summary": plan.summary,
            "items": [{"action": i.action, "src": i.src, "dest": i.dest}
                      for i in plan.items]}


def _course_documents(c: Any, code: str) -> dict[str, Any]:
    course = c.courses.course(code)
    if not course:
        return {"error": f"no course {code}"}
    return {"code": code,
            "documents": [dict(d) for d in
                          c.db.course_documents(int(course["id"]))]}


def _rate_recipe(c: Any, a: dict[str, Any]) -> Any:
    ident = str(a.get("recipe", ""))
    rating = float(a.get("rating", 3))
    row = c.db.recipe_by_name(ident)
    if not row:
        return {"error": f"recipe not found: {ident}"}
    return c.chef.rate(int(row["id"]), rating)


def _presence(c: Any) -> dict[str, Any]:
    snap = c.context.snapshot()
    return {"presence": snap.get("presence", {}), "now": snap.get("now")}


def _course_sync(c: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if getattr(c, "courses", None) is not None:
        for row in c.db.courses():
            code = str(row["code"])
            try:
                out[code] = c.courses.sync_page_assignments(code)
            except Exception as exc:  # noqa: BLE001
                out[code] = {"ok": False, "error": str(exc)}
    try:
        out["_calendar"] = c.planner.sync_course_events()
    except Exception as exc:  # noqa: BLE001
        out["_calendar"] = {"ok": False, "error": str(exc)}
    return {"ok": True, "courses": out}


def _task_add(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    tid = c.planner.add_task(
        a.get("title", ""), detail=a.get("detail", ""),
        est_minutes=int(a.get("est_minutes", 60)),
        deadline=int(a.get("deadline", 0)),
        priority=int(a.get("priority", 3)), tags=a.get("tags", ""))
    return {"ok": True, "task_id": tid, "title": a.get("title"),
            "state": "todo"}


def _suggest_reward(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    from .. import motivation
    est = int(a.get("est_minutes", 0))
    prio = int(a.get("priority", 0))
    tid = a.get("task_id")
    if tid:
        row = c.db.task_by_id(int(tid))
        if row:
            est = est or int(row["est_minutes"] or 0)
            prio = prio or int(row["priority"] or 0)
    importance = motivation.task_importance(est, prio)
    return {"importance": importance,
            "reward": motivation.suggest_reward(c.db, importance)}
