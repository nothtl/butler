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

M1 adds a second, disjoint ``readonly`` profile: a minimal, side-effect-free
executive surface for an external agent runtime (AI Butler). Tools carry a
``profile`` and :class:`~butler.agent.registry.ToolRegistry` filters both the
``tools/list`` schema and the dispatch by it, so a read-only client can never
reach a mutating tool even by guessing its name.
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
            side_effect: bool = False, profile: str = "full") -> None:
        reg.add("mcp_" + name, desc, handler, params or [],
                action=action or name, side_effect=side_effect, mcp_name=name,
                profile=profile)

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

    # --------------------------------------------- read-only executive surface
    # M1: the minimal, stable, *side-effect-free* surface an external agent
    # runtime (AI Butler) consumes first. Every handler reads live state and
    # never persists, syncs or writes: no plan commit, no calendar write, no
    # task transition. Exposed only under the "readonly" MCP profile
    # (BUTLER_MCP_PROFILE=readonly); the historical 51-tool surface is the
    # "full" profile and is untouched.
    add("get_time", "Current local time, timezone and today's day window.",
        lambda a: _get_time(c), action="now", profile="readonly")
    add("get_context", "Personal context snapshot (free time, deadlines, "
        "expiring food, presence).",
        lambda a: c.context.snapshot(), action="context", profile="readonly")
    add("get_day", "Today's plan: the committed active plan if one exists, "
        "otherwise a read-only preview.",
        lambda a: _get_day(c), action="day", profile="readonly")
    add("plan_day", "Read-only proposal for today (nothing is committed).",
        lambda a: _preview_day(c), action="day", profile="readonly")
    add("get_schedule", "The committed active schedule from the database, if "
        "any (never recomputes or writes).",
        lambda a: _get_schedule(c), action="day", profile="readonly")
    add("get_tasks", "List active tasks.",
        lambda a: {"ok": True,
                   "tasks": [dict(r) for r in c.db.tasks("active")]},
        action="tasks", profile="readonly")
    add("get_courses", "List tracked courses.",
        lambda a: {"ok": True, "courses": c.courses.list_courses()},
        action="course_list", profile="readonly")
    add("get_projects", "List projects with effort-based progress and risk.",
        lambda a: _get_projects(c, a),
        [P("status", "str", default="")], action="projects", profile="readonly")
    add("get_project", "One project by id or name, with milestones.",
        lambda a: _get_project(c, a),
        [P("project", "str", default="")], action="projects", profile="readonly")
    add("get_project_workload", "Remaining effort, progress and next tasks for "
        "a project (or all active projects).",
        lambda a: _get_project_workload(c, a),
        [P("project", "str", default="")], action="projects", profile="readonly")
    add("get_project_risk", "Deterministic, explainable risk for a project (or "
        "the most at-risk active project).",
        lambda a: _get_project_risk(c, a),
        [P("project", "str", default="")], action="projects", profile="readonly")
    add("get_project_dependencies", "Dependency DAG, blocked tasks and cycles "
        "for a project.",
        lambda a: _get_project_dependencies(c, a),
        [P("project", "str", default="")], action="projects", profile="readonly")
    add("find_available_time", "Free waking intervals for a day (minus sleep "
        "and hard events).",
        lambda a: _find_available_time(c, a),
        [P("day_offset", "int", default=0),
         P("min_minutes", "int", default=0)],
        action="now", profile="readonly")
    add("get_week", "Read-only schedule preview for the next N days.",
        lambda a: c.planner.plan_week(days=int(a.get("days", 7) or 7)),
        [P("days", "int", default=7)], action="day", profile="readonly")
    # --- M4: external knowledge (strictly read-only; page content untrusted) ---
    add("web_search", "Deterministic web search. Returns ranked source metadata "
        "(title, url, domain, retrieved_at, confidence) and never executes "
        "side effects.",
        lambda a: _web_search(c, a),
        [P("query", "str", required=True),
         P("domains", "list", default=[]),
         P("max_results", "int", default=0)],
        action="web_search", profile="readonly")
    add("web_research", "Multi-source research: search, fetch, extract and rank "
        "external evidence. Returns a ResearchResult with sources and "
        "limitations; page content is untrusted data.",
        lambda a: _web_research(c, a),
        [P("query", "str", required=True),
         P("domains", "list", default=[]),
         P("max_sources", "int", default=0)],
        action="web_research", profile="readonly")
    add("web_fetch", "Fetch and extract one http/https URL (localhost/private "
        "hosts rejected). Returns bounded, untrusted page content.",
        lambda a: _web_fetch(c, a),
        [P("url", "str", required=True)], action="web_fetch", profile="readonly")
    add("knowledge_lookup", "Answer from local Butler state only (tasks, "
        "courses, projects). Never contacts the web.",
        lambda a: _knowledge_lookup(c, a),
        [P("query", "str", required=True)],
        action="knowledge_lookup", profile="readonly")
    # --- M5: deterministic schedule optimization (strictly read-only) ---
    add("optimize_day", "Deterministically optimize one day's schedule across "
        "tasks, projects, dependencies, deadlines and soft preferences. "
        "Read-only: returns a proposal, never commits.",
        lambda a: _optimize(c, a, 1),
        [P("strategy", "str", default=""), P("day_offset", "int", default=0)],
        action="optimize_day", profile="readonly")
    add("optimize_week", "Deterministically optimize the next N days "
        "(multi-day, deadline/project aware). Read-only proposal.",
        lambda a: _optimize(c, a, int(a.get("days", 7) or 7)),
        [P("strategy", "str", default=""), P("days", "int", default=7)],
        action="optimize_week", profile="readonly")
    add("evaluate_schedule", "Evaluate schedule feasibility, deadline slack and "
        "project risk pressure over a horizon. Read-only.",
        lambda a: _evaluate(c, a),
        [P("days", "int", default=7), P("strategy", "str", default="")],
        action="evaluate_schedule", profile="readonly")
    add("find_best_slot", "Find the best feasible slot for a task. Read-only.",
        lambda a: _find_best_slot(c, a),
        [P("task", "str", default=""), P("days", "int", default=7),
         P("strategy", "str", default="")],
        action="find_best_slot", profile="readonly")
    # --- M6: long-term memory (strictly read-only) ---
    add("memory_search", "Search durable long-term memory (typed, provenance-"
        "aware). Read-only.",
        lambda a: _memory_search(c, a),
        [P("query", "str", required=True), P("types", "list", default=[]),
         P("limit", "int", default=0)],
        action="memory_search", profile="readonly")
    add("memory_get_relevant", "Bounded retrieval of memories relevant to a "
        "context (query/subject/entities/tags). Read-only.",
        lambda a: _memory_get_relevant(c, a),
        [P("query", "str", default=""), P("subject", "str", default=""),
         P("entities", "list", default=[]), P("tags", "list", default=[]),
         P("types", "list", default=[]), P("limit", "int", default=0)],
        action="memory_query", profile="readonly")
    add("memory_list", "List memories with type/state/scope filters. Read-only.",
        lambda a: _memory_list(c, a),
        [P("types", "list", default=[]), P("state", "str", default=""),
         P("active_only", "bool", default=False), P("scope", "str", default=""),
         P("limit", "int", default=50)],
        action="memory_query", profile="readonly")
    add("memory_history", "Full history for a subject/key, including superseded "
        "rows. Read-only.",
        lambda a: _memory_history(c, a),
        [P("subject", "str", default=""), P("key", "str", default=""),
         P("limit", "int", default=50)],
        action="memory_query", profile="readonly")
    # --- M7: proactive executive behavior (strictly read-only) ---
    add("get_proactive_candidates", "Deterministically generate and rank "
        "proactive candidates (deadline risk, free time, conflicts, risk "
        "increases, ...). Read-only; nothing is notified or mutated.",
        lambda a: _get_proactive_candidates(c, a),
        [P("state", "str", default=""), P("category", "str", default=""),
         P("limit", "int", default=20)],
        action="proactive_query", profile="readonly")
    add("get_proactive_status", "Proactive engine status: quiet hours, daily "
        "budget usage, suppressions and snoozes. Read-only.",
        lambda a: _get_proactive_status(c, a),
        [], action="proactive_query", profile="readonly")
    add("explain_proactive_candidate", "Evidence and triggering condition for a "
        "proactive candidate. Read-only.",
        lambda a: _explain_proactive_candidate(c, a),
        [P("key", "str", default=""), P("query", "str", default="")],
        action="proactive_explain", profile="readonly")
    # --- N2: universal trackers (strictly read-only) ---
    add("get_trackers", "List configured trackers with state and last/next "
        "check. Read-only.",
        lambda a: _get_trackers(c, a),
        [P("state", "str", default=""), P("limit", "int", default=50)],
        action="tracker_list", profile="readonly")
    add("get_tracker", "One tracker by id or name, with its last event. "
        "Read-only.",
        lambda a: _get_tracker(c, a),
        [P("tracker", "str", default="")], action="tracker_query",
        profile="readonly")
    add("evaluate_tracker", "Dry-run a tracker and report whether it would "
        "fire (no notification, no write). Read-only.",
        lambda a: _evaluate_tracker(c, a),
        [P("tracker", "str", default="")], action="tracker_evaluate",
        profile="readonly")
    add("executive_ask", "Typed executive query. Accepts a structured request "
        "object (the M2 semantic contract) or raw text; validates it, gathers "
        "request-scoped context and returns an AgentResult. Strictly read-only: "
        "mutating actions return needs_confirmation and never execute.",
        lambda a: _executive_ask(c, a),
        [P("request", "object", default={}),
         P("text", "str", default=""),
         P("include_context", "bool", default=False)],
        action="ask", profile="readonly")

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


# ------------------------------------------------- read-only (M1) helpers
def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _get_time(c: Any) -> dict[str, Any]:
    from datetime import datetime
    cfg = c.cfg
    now = cfg.now_local() if hasattr(cfg, "now_local") else datetime.now()
    ts = int(now.timestamp())
    day_start = int(c.planner._day_start_ts(ts))
    return {
        "ok": True,
        "epoch": ts,
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "timezone": getattr(cfg, "timezone", "") or "system",
        "day_start": day_start,
        "day_end": day_start + 86400,
    }


def _committed_plan(c: Any) -> tuple[Any, Any] | None:
    from .. import schedule as sch
    row = c.db.latest_plan()
    if row is None:
        return None
    return row, sch.PlanState.from_json(str(row["json"]))


def _get_day(c: Any) -> dict[str, Any]:
    committed = _committed_plan(c)
    if committed is None:
        return _preview_day(c)
    row, state = committed
    summary = c.planner._summary(state)
    summary.update({"source": "committed", "committed": True,
                    "plan_id": int(row["id"]), "created": int(row["created"])})
    return summary


def _preview_day(c: Any) -> dict[str, Any]:
    week = c.planner.plan_week(days=1)
    day = dict(week["days"][0])
    day.update({"source": "preview", "committed": False})
    return day


def _get_schedule(c: Any) -> dict[str, Any]:
    committed = _committed_plan(c)
    if committed is None:
        return {"ok": True, "committed": False, "plan": None,
                "note": "No committed active plan."}
    row, state = committed
    return {"ok": True, "committed": True, "plan_id": int(row["id"]),
            "created": int(row["created"]), "plan": state.to_dict()}


def _project_module(c: Any) -> Any:
    return getattr(c, "projects", None)


def _get_projects(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    projects = _project_module(c)
    if projects is None:
        return {"ok": False, "error": "project intelligence unavailable"}
    status = str(a.get("status", "") or "")
    rows = projects.list_projects(status)
    return {"ok": True, "count": len(rows), "projects": rows}


def _get_project(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    projects = _project_module(c)
    if projects is None:
        return {"ok": False, "error": "project intelligence unavailable"}
    ident = str(a.get("project", "") or "").strip()
    if not ident:
        return {"ok": False, "error": "project id or name is required"}
    got = projects.get_project(ident)
    if got is None:
        return {"ok": False, "error": f"project not found: {ident}"}
    return {"ok": True, "project": got}


def _get_project_workload(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    projects = _project_module(c)
    if projects is None:
        return {"ok": False, "error": "project intelligence unavailable"}
    ident = str(a.get("project", "") or "").strip()
    if ident:
        wl = projects.workload(ident)
        if wl is None:
            return {"ok": False, "error": f"project not found: {ident}"}
        return {"ok": True, "workload": wl}
    rows = [projects.workload(p["id"]) for p in projects.list_projects("active")]
    rows = [r for r in rows if r]
    return {"ok": True, "count": len(rows), "workloads": rows,
            "total_remaining_minutes": sum(int(r["remaining_minutes"])
                                           for r in rows)}


def _get_project_risk(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    projects = _project_module(c)
    if projects is None:
        return {"ok": False, "error": "project intelligence unavailable"}
    ident = str(a.get("project", "") or "").strip()
    if ident:
        risk = projects.risk(ident)
        if risk is None:
            return {"ok": False, "error": f"project not found: {ident}"}
        return {"ok": True, "risk": risk}
    worst = projects.most_at_risk()
    return {"ok": True, "most_at_risk": worst}


def _get_project_dependencies(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    projects = _project_module(c)
    if projects is None:
        return {"ok": False, "error": "project intelligence unavailable"}
    ident = str(a.get("project", "") or "").strip()
    if not ident:
        return {"ok": False, "error": "project id or name is required"}
    deps = projects.dependencies(ident)
    if deps is None:
        return {"ok": False, "error": f"project not found: {ident}"}
    return {"ok": True, "dependencies": deps}


def _web_module(c: Any) -> Any:
    return getattr(c, "web", None)


def _domain_arg(a: dict[str, Any]) -> list[str]:
    raw = a.get("domains") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(d).strip() for d in raw if str(d).strip()]


def _web_search(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    web = _web_module(c)
    if web is None:
        return {"ok": False, "error": "web knowledge unavailable"}
    query = str(a.get("query", "") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    max_results = int(a.get("max_results", 0) or 0) or None
    result = web.search(query, domains=_domain_arg(a) or None,
                        max_results=max_results)
    return result.to_dict()


def _web_research(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    web = _web_module(c)
    if web is None:
        return {"ok": False, "error": "web knowledge unavailable"}
    query = str(a.get("query", "") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    max_sources = int(a.get("max_sources", 0) or 0) or None
    result = web.research(query, domains=_domain_arg(a) or None,
                          max_sources=max_sources)
    return result.to_dict()


def _web_fetch(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    web = _web_module(c)
    if web is None:
        return {"ok": False, "error": "web knowledge unavailable"}
    url = str(a.get("url", "") or "").strip()
    if not url:
        return {"ok": False, "error": "url is required"}
    result = web.fetch(url)
    return result.to_dict()


def _knowledge_lookup(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    web = _web_module(c)
    if web is None:
        return {"ok": False, "error": "web knowledge unavailable"}
    query = str(a.get("query", "") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    result = web.knowledge_lookup(query)
    return result.to_dict()


def _optimizer_module(c: Any) -> Any:
    return getattr(c, "optimizer", None)


def _opt_strategy(c: Any, a: dict[str, Any]) -> str:
    s = str(a.get("strategy", "") or "").strip()
    if s:
        return s
    return str(getattr(c.cfg, "optimizer_default_strategy", "balanced")
               or "balanced")


def _optimize(c: Any, a: dict[str, Any], days: int) -> dict[str, Any]:
    opt = _optimizer_module(c)
    if opt is None:
        return {"ok": False, "error": "schedule optimizer unavailable"}
    offset = int(a.get("day_offset", 0) or 0)
    day_ts = int(c.planner._today()) + offset * 86400
    res = opt.optimize_from_state(days=max(1, int(days)),
                                  strategy=_opt_strategy(c, a), day_ts=day_ts)
    return {"ok": True, **res.to_dict()}


def _evaluate(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    opt = _optimizer_module(c)
    if opt is None:
        return {"ok": False, "error": "schedule optimizer unavailable"}
    days = max(1, int(a.get("days", 7) or 7))
    res = opt.optimize_from_state(days=days, strategy=_opt_strategy(c, a))
    return {"ok": True, **res.to_dict()}


def _find_best_slot(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    opt = _optimizer_module(c)
    if opt is None:
        return {"ok": False, "error": "schedule optimizer unavailable"}
    task = str(a.get("task", "") or "").strip().lower()
    if not task:
        return {"ok": False, "error": "task is required"}
    days = max(1, int(a.get("days", 7) or 7))
    res = opt.optimize_from_state(days=days, strategy=_opt_strategy(c, a))
    matches = [s for s in res.sessions
               if task in s.title.lower() or s.title.lower() in task]
    if not matches:
        return {"ok": False, "error": "no feasible slot found for that task",
                "optimization": res.to_dict()}
    return {"ok": True, "best_slot": matches[0].to_dict(),
            "optimization": res.to_dict()}


def _memory_module(c: Any) -> Any:
    return getattr(c, "memory", None)


def _str_list(a: dict[str, Any], key: str) -> list[str]:
    raw = a.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(x).strip() for x in raw if str(x).strip()]


def _memory_search(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    mem = _memory_module(c)
    if mem is None:
        return {"ok": False, "error": "memory unavailable"}
    query = str(a.get("query", "") or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    types = _str_list(a, "types") or None
    limit = int(a.get("limit", 0) or 0) or None
    rows = mem.search(query, types=types, limit=limit)
    return {"ok": True, "query": query, "count": len(rows), "memories": rows}


def _memory_get_relevant(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    mem = _memory_module(c)
    if mem is None:
        return {"ok": False, "error": "memory unavailable"}
    context = {"query": str(a.get("query", "") or ""),
               "subject": str(a.get("subject", "") or ""),
               "entities": _str_list(a, "entities"),
               "tags": _str_list(a, "tags"),
               "types": _str_list(a, "types") or None}
    limit = int(a.get("limit", 0) or 0) or None
    rows = mem.get_relevant(context, limit=limit)
    return {"ok": True, "count": len(rows), "memories": rows}


def _memory_list(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    mem = _memory_module(c)
    if mem is None:
        return {"ok": False, "error": "memory unavailable"}
    rows = mem.list(types=_str_list(a, "types") or None,
                    state=str(a.get("state", "") or ""),
                    active_only=bool(a.get("active_only", False)),
                    scope=str(a.get("scope", "") or ""),
                    limit=int(a.get("limit", 50) or 50))
    return {"ok": True, "count": len(rows), "memories": rows}


def _memory_history(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    mem = _memory_module(c)
    if mem is None:
        return {"ok": False, "error": "memory unavailable"}
    rows = mem.history(subject=str(a.get("subject", "") or ""),
                       key=str(a.get("key", "") or ""),
                       limit=int(a.get("limit", 50) or 50))
    return {"ok": True, "count": len(rows), "memories": rows}


def _proactive_engine(c: Any) -> Any:
    return getattr(c, "proactive_engine", None)


def _get_proactive_candidates(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    eng = _proactive_engine(c)
    if eng is None:
        return {"ok": False, "error": "proactive engine unavailable"}
    res = eng.run_cycle(deliver=False, persist=False)
    state = str(a.get("state", "") or "")
    category = str(a.get("category", "") or "")
    limit = int(a.get("limit", 20) or 20)
    if state or category:
        rows = eng.list_candidates(state=state, category=category, limit=limit)
    else:
        rows = res.get("candidates", [])[:limit]
    return {"ok": True, "count": len(rows), "candidates": rows,
            "suppressed": res.get("suppressed", [])}


def _get_proactive_status(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    eng = _proactive_engine(c)
    if eng is None:
        return {"ok": False, "error": "proactive engine unavailable"}
    return {"ok": True, **eng.status()}


def _explain_proactive_candidate(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    eng = _proactive_engine(c)
    if eng is None:
        return {"ok": False, "error": "proactive engine unavailable"}
    key = str(a.get("key", "") or "").strip()
    query = str(a.get("query", "") or "").strip().lower()
    if key:
        ex = eng.explain(key)
        if ex is not None:
            return {"ok": True, "explanation": ex}
    # pure preview (no writes): generate in memory and explain the match
    res = eng.run_cycle(deliver=False, persist=False)
    cands = res.get("candidates", [])
    if not key and query:
        for row in cands:
            hay = f"{row['key']} {row['title']} {row['summary']}".lower()
            if query in hay:
                key = row["key"]
                break
    for row in cands:
        if row.get("key") == key:
            return {"ok": True, "explanation": eng.explain_candidate(row)}
    return {"ok": False, "error": "no candidate matched"}


def _tracker_engine(c: Any) -> Any:
    return getattr(c, "trackers", None)


def _get_trackers(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    eng = _tracker_engine(c)
    if eng is None:
        return {"ok": False, "error": "tracker engine unavailable"}
    state = str(a.get("state", "") or "")
    limit = int(a.get("limit", 50) or 50)
    rows = eng.list(state=state, limit=limit)
    return {"ok": True, "count": len(rows),
            "trackers": [t.to_dict() for t in rows]}


def _find_tracker(c: Any, ident: str) -> Any:
    eng = _tracker_engine(c)
    if eng is None:
        return None
    if ident.isdigit():
        return eng.get(int(ident))
    for row in eng.list(limit=200):
        if ident.lower() in (row.name or "").lower() \
                or ident.lower() in (row.target_ref or "").lower():
            return row
    return None


def _get_tracker(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    eng = _tracker_engine(c)
    if eng is None:
        return {"ok": False, "error": "tracker engine unavailable"}
    ident = str(a.get("tracker", "") or "").strip()
    if not ident:
        return {"ok": False, "error": "tracker id or name is required"}
    t = _find_tracker(c, ident)
    if t is None:
        return {"ok": False, "error": f"tracker not found: {ident}"}
    return {"ok": True, **eng.why(t.id)}


def _evaluate_tracker(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    eng = _tracker_engine(c)
    if eng is None:
        return {"ok": False, "error": "tracker engine unavailable"}
    ident = str(a.get("tracker", "") or "").strip()
    if not ident:
        return {"ok": False, "error": "tracker id or name is required"}
    t = _find_tracker(c, ident)
    if t is None:
        return {"ok": False, "error": f"tracker not found: {ident}"}
    res = eng.evaluate(t, dry_run=True)
    return {"ok": True, "evaluation": res.to_dict()}


def _executive_ask(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    from .service import ExecutiveService
    svc = getattr(c, "_executive_service", None)
    if svc is None:
        svc = ExecutiveService(c)
        try:
            c._executive_service = svc
        except Exception:  # noqa: BLE001 — container may use __slots__
            pass
    result = svc.ask(request=a.get("request") or None,
                     text=str(a.get("text", "") or ""),
                     include_context=bool(a.get("include_context", False)),
                     read_only=True)
    return result.to_dict()


def _find_available_time(c: Any, a: dict[str, Any]) -> dict[str, Any]:
    from datetime import datetime
    from .. import schedule as sch
    day_offset = int(a.get("day_offset", 0) or 0)
    min_minutes = int(a.get("min_minutes", 0) or 0)
    ts = int(c.planner._today()) + day_offset * 86400
    events = c.planner._day_events(ts)
    gaps = sch.free_intervals(c.planner.day_start, c.planner.day_end,
                              int(c.cfg.sleep_start), int(c.cfg.sleep_end),
                              events)
    intervals = []
    for start, end in gaps:
        if end - start < min_minutes:
            continue
        intervals.append({"start_min": start, "end_min": end,
                          "start": _hhmm(start), "end": _hhmm(end),
                          "minutes": end - start})
    return {
        "ok": True,
        "date": datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
        "day_start": c.planner.day_start,
        "day_end": c.planner.day_end,
        "total_minutes": sum(i["minutes"] for i in intervals),
        "intervals": intervals,
        "events": [e.to_dict() for e in events],
    }
