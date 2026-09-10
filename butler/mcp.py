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
VERSION = "1.2.0"
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
            s("courses", "List tracked courses.", {}, []),
            s("course_documents", "List downloaded materials for a course code.",
              {"code": {"type": "string"}}, ["code"]),
            s("pantry", "List current food inventory.", {}, []),
            s("expiring", "List food expiring within N days (default 3).",
              {"days": {"type": "integer"}}, []),
            s("recipe", "Suggest a meal that fits the free-time budget and ingredients.",
              {"budget_minutes": {"type": "integer"}}, []),
            s("grocery", "Deterministic grocery list (deduped shortfalls + low stock).", {}, []),
            s("recipe_search", "Search real online recipes by keyword, then persist to Library.",
              {"query": {"type": "string"}}, []),
            s("recipes", "List the Recipe Library (persisted, rate-able).", {}, []),
            s("favorites", "List favorited recipes.", {}, []),
            s("recipe_history", "Recently cooked/planned meals.", {}, []),
            s("rate_recipe", "Rate a recipe 0-5 by id or name.",
              {"recipe": {"type": "string"}, "rating": {"type": "integer"}}, []),
            s("context", "Personal context snapshot (free time, deadlines, expiring food).", {},
              []),
            s("presence", "Home presence/location from Home Assistant (if configured).", {}, []),
            s("health", "Butler subsystem health + run mode.", {}, []),
            s("day", "Compute the schedule plan for today (read-only preview).",
              {}, []),
            s("week", "Compute a read-only schedule preview for the next N days.",
              {"days": {"type": "integer"}}, []),
            s("course_sync", "Re-check tracked courses: scrape due dates and "
              "import class-time blocks from their calendar feed.", {}, []),
            s("what_now", "What Butler recommends you do right now.", {},
              []),
            s("why", "Explain why the schedule changed (last plan diff).", {}, []),
            s("tasks", "List active tasks.", {}, []),
            s("task_add", "Add a task (appears in planning / scheduling).",
              {"title": {"type": "string"}, "detail": {"type": "string"},
               "est_minutes": {"type": "integer"}, "deadline": {"type": "integer"},
               "priority": {"type": "integer"}, "tags": {"type": "string"}},
              ["title"]),
            s("task_start", "Move a task to doing (state transition).",
              {"task_id": {"type": "integer"}}, ["task_id"]),
            s("task_done", "Complete a task.",
              {"task_id": {"type": "integer"}}, ["task_id"]),
            s("task_skip", "Skip a task.",
              {"task_id": {"type": "integer"}}, ["task_id"]),
            s("task_defer", "Defer a task.",
              {"task_id": {"type": "integer"}}, ["task_id"]),
            s("task_block", "Mark a task blocked.",
              {"task_id": {"type": "integer"}}, ["task_id"]),
            s("task_cancel", "Cancel a task.",
              {"task_id": {"type": "integer"}}, ["task_id"]),
            s("came_up", "An unplanned urgent thing appeared: add it and re-plan.",
              {"title": {"type": "string"}, "est_minutes": {"type": "integer"},
               "deadline": {"type": "integer"}, "priority": {"type": "integer"}},
              ["title"]),
            s("reschedule", "Re-solve the planner for the full day.", {}, []),
            s("undo", "Undo the last schedule change.", {}, []),
            s("add_course", "Track a new course (creates dir + monitors files).",
              {"code": {"type": "string"}, "name": {"type": "string"},
               "url": {"type": "string"}, "platform": {"type": "string"},
               "semester": {"type": "string"}},
              ["code"]),
            s("drop_course", "Stop tracking a course.",
              {"code": {"type": "string"}}, ["code"]),
            s("course_check", "Check all tracked courses for new materials/assignments.",
              {}, []),
            s("brief", "Morning/standup executive briefing (tasks, deadlines, risks).",
              {}, []),
            s("review", "End-of-day executive review (what got done).", {}, []),
            s("routines_list", "Active + candidate routines.", {}, []),
            s("scan_routines", "Detect candidate routines from history.",
              {}, []),
            s("routines_confirm", "Confirm a candidate routine.",
              {"routine_id": {"type": "integer"}}, []),
            s("routines_reject", "Reject a candidate routine.",
              {"routine_id": {"type": "integer"}}, []),
            s("rewards", "List the reward library (little treats after tasks).",
              {}, []),
            s("suggest_reward", "Suggest a reward scaled to a task's effort.",
              {"task_id": {"type": "integer"},
               "est_minutes": {"type": "integer"},
               "priority": {"type": "integer"}}, []),
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
        if name == "courses":
            return {"courses": c.courses.list_courses()}
        if name == "course_documents":
            code = a.get("code", "")
            course = c.courses.course(code)
            if not course:
                return {"error": f"no course {code}"}
            return {"code": code, "documents": [dict(d) for d in c.db.course_documents(int(course["id"]))]}
        if name == "pantry":
            return {"items": c.food.all()}
        if name == "expiring":
            return {"items": c.food.expiring(int(a.get("days", 3)))}
        if name == "recipe":
            return c.chef.plan_meal(budget_minutes=int(a.get("budget_minutes", 45)))
        if name == "grocery":
            return {"items": c.chef.grocery_list()}
        if name == "recipe_search":
            return {"query": a.get("query", ""),
                    "results": c.chef.search(a.get("query", ""))}
        if name == "recipes":
            return {"count": len(c.chef.library()), "recipes": c.chef.library()}
        if name == "favorites":
            return {"recipes": c.chef.favorites()}
        if name == "recipe_history":
            return {"history": c.chef.history()}
        if name == "rate_recipe":
            ident = str(a.get("recipe", ""))
            rating = float(a.get("rating", 3))
            row = c.db.recipe_by_name(ident)
            if not row:
                return {"error": f"recipe not found: {ident}"}
            return c.chef.rate(int(row["id"]), rating)
        if name == "context":
            return c.context.snapshot()
        if name == "presence":
            snap = c.context.snapshot()
            return {"presence": snap.get("presence", {}),
                    "now": snap.get("now")}
        if name == "health":
            return {"ready": c.health.ready(), "liveness": c.health.liveness(),
                    **c.health.status()}
        if name == "day":
            return c.planner.plan_day()
        if name == "week":
            return c.planner.plan_week(days=int(a.get("days", 7) or 7))
        if name == "course_sync":
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
        if name == "what_now":
            return c.planner.what_now()
        if name == "why":
            return c.planner.why()
        if name == "tasks":
            return {"tasks": [dict(r) for r in c.db.tasks("active")]}
        if name == "task_add":
            tid = c.planner.add_task(
                a.get("title", ""), detail=a.get("detail", ""),
                est_minutes=int(a.get("est_minutes", 60)),
                deadline=int(a.get("deadline", 0)),
                priority=int(a.get("priority", 3)), tags=a.get("tags", ""))
            return {"ok": True, "task_id": tid,
                    "title": a.get("title"), "state": "todo"}
        if name == "task_start":
            return c.planner.start(int(a.get("task_id", 0)))
        if name == "task_done":
            return c.planner.done(int(a.get("task_id", 0)))
        if name == "task_skip":
            return c.planner.skip(int(a.get("task_id", 0)))
        if name == "task_defer":
            return c.planner.defer(int(a.get("task_id", 0)))
        if name == "task_block":
            return c.planner.block_task(int(a.get("task_id", 0)))
        if name == "task_cancel":
            return c.planner.cancel_task(int(a.get("task_id", 0)))
        if name == "came_up":
            return c.planner.came_up(
                a.get("title", ""), est_minutes=int(a.get("est_minutes", 60)),
                deadline=int(a.get("deadline", 0)),
                priority=int(a.get("priority", 4)))
        if name == "reschedule":
            return c.planner.reschedule()
        if name == "undo":
            return c.planner.undo()
        if name == "add_course":
            return c.courses.add_course(
                a.get("code", ""), name=a.get("name", ""), url=a.get("url", ""),
                platform=a.get("platform", ""), semester=a.get("semester", ""))
        if name == "drop_course":
            return c.courses.remove_course(a.get("code", ""))
        if name == "course_check":
            return {"updates": c.courses.check_all()}
        if name == "brief":
            return {"briefing": c.executive.briefing()}
        if name == "review":
            return {"review": c.executive.review()}
        if name == "routines_list":
            return {"active": c.routines.active(), "candidates": c.routines.candidates()}
        if name == "scan_routines":
            return c.routines.scan()
        if name == "routines_confirm":
            return c.routines.confirm(a.get("routine_id"))
        if name == "routines_reject":
            return c.routines.reject(a.get("routine_id"))
        if name == "rewards":
            from . import motivation
            return {"rewards": [dict(r) for r in c.db.rewards()]}
        if name == "suggest_reward":
            from . import motivation
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
