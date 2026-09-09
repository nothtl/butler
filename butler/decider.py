"""Intent decoding (the "AI" that decides).

A lightweight deterministic NLU that turns a natural-language Telegram message
into a structured :class:`Intent`. Design principle: the decider proposes a
:class:`Plan` / intent, the deterministic :class:`Engine` gates the actual
filesystem operations, and bulk changes always require confirmation.

Recognised intents:
  workspace | organize | find | search | resume | delete/trash | dupes |
  list | mkdir | move | trash-list | recover
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .db import DB
from .engine import Engine, EngineError, extract_course_code
from .organizer import Organizer, Plan, PlanItem
from .search import Search

# Map "Project_1A" style names to a friendly project folder (Test 1)
@dataclass
class Intent:
    kind: str
    target: str = ""
    query: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    plan: Plan | None = None


class Decider:
    def __init__(self, cfg: Config, db: DB, engine: Engine,
                 organizer: Organizer, search: Search, chat: Any = None,
                 planner: Any = None, courses: Any = None, food: Any = None,
                 chef: Any = None, nas: Any = None, context: Any = None,
                 proactive: Any = None):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.organizer = organizer
        self.search = search
        if chat is None:
            from .chat import Chat
            chat = Chat(cfg, db, search)
        self.chat = chat
        if planner is None:
            from .planner import Planner
            planner = Planner(type("_C", (), {"cfg": cfg, "db": db})())
        self.planner = planner
        # --- Phase 3 subsystems ---
        self.courses = courses
        self.food = food
        self.chef = chef
        self.nas = nas
        self.context = context
        self.proactive = proactive

    # ---------------------------------------------------------------- parse
    def parse(self, message: str) -> Intent:
        msg = message.strip()
        low = msg.lower()

        slash = re.match(r"^/(\w+)(?:\s+(.*))?$", msg)
        if slash:
            return self._slash(slash.group(1), slash.group(2) or "", msg)

        # --- planner / scheduler intents (Phase 2) ---
        if re.search(r"\b(what should|what to do|what now|what do i do|what next|whats next)\b", low):
            return Intent("now", raw=msg)
        if re.search(r"\b(my day|todays plan|today's plan|today's schedule|show todays|show today's|show.*day|what day)\b".replace("today's", "today?s"), low):
            return Intent("day", raw=msg)
        if re.search(r"\b(add( a)? task|new task|remind me to|i need to|todo:|task:|add todo)\b", low):
            return Intent("add_task", query=msg, raw=msg)
        if re.search(r"\b(came up|something came up|something has come up|urgent|interrupt)\b", low):
            return Intent("came_up", query=msg, raw=msg)
        if re.search(r"\b(why did|why was|why is)\b.*\b(move|resched|moved)\b", low):
            return Intent("why", raw=msg)
        if re.search(r"\b(undo|go back|revert)\b", low):
            return Intent("undo", raw=msg)
        for _kw, _kind in (("done", "done"), ("finish", "done"), ("complete", "done"),
                           ("skip", "skip"), ("start", "start")):
            m = re.search(rf"\b{_kw}\b\s+(.+)", low)
            if m:
                return Intent(_kind, query=m.group(1).strip(), raw=msg)

        # --- Phase 3: course intelligence ---
        if re.search(r"\b(whats going on|briefing|my context|give me context|summarize my day)\b", low):
            return Intent("context", raw=msg)
        if re.search(r"\b(add|track|monitor|subscribe|watch)\b.*\bcourse\b", low):
            return Intent("course_add", query=msg, raw=msg)
        if re.search(r"\b(check|scan|monitor|update)\b.*\bcourse(s)?\b", low):
            return Intent("course_check", raw=msg)
        if re.search(r"\bmy courses\b|\blist courses\b|\bshow courses\b", low):
            return Intent("course_list", raw=msg)
        if re.search(r"\bcourse(s)?\b.*\b(materials?|files|docs?|notes|slides)\b", low):
            code = extract_course_code(msg) or ""
            return Intent("course_docs", query=code, raw=msg)
        if re.search(r"\b(sync|digest|update|check|fetch)\b.*\b(assignments?|deadlines?|due dates)\b" +
                     r"|\b(assignment|project|assignment tasks)\b.*\b(brief|digest|sync)\b" +
                     r"|\bwhats? (due|coming up|upcoming)\b.*(assignment|deadline)", low):
            code = extract_course_code(msg) or ""
            return Intent("course_assignments", query=code, raw=msg)

        # --- Phase 4.1: presence ---
        if re.search(r"\b(where am i|where am i now|my location|where i am)\b", low):
            return Intent("where_am_i", raw=msg)
        if re.search(r"\b(around me|near me|whats around me|whats near me)\b", low):
            return Intent("around_me", raw=msg)

        # --- Phase 3: food / chef ---
        if re.search(r"\b(from the pantry|in the fridge|in my kitchen|what do i have)\b", low):
            return Intent("food_list", raw=msg)
        if re.search(r"\bexpires?\b|\bexpiring\b|about to expire|going bad|use it or lose it",
                     low):
            return Intent("food_expiring", raw=msg)
        if re.search(r"\b(add|log|record)\b.*\b(food|pantry|fridge|items?)\b|\bi bought\b|\bgot some\b", low):
            return Intent("food_add", query=msg, raw=msg)
        if re.search(r"\b(used|ate|cooked|consumed)\b.*\b\b", low):
            return Intent("food_consume", query=msg, raw=msg)
        if re.search(r"\b(shopping list|grocery list|what to buy|things to buy)\b", low):
            return Intent("grocery", raw=msg)
        if re.search(r"\b(plan|make)\b.*\b(meal|dinner|lunch|breakfast)\b|\bmeal plan\b", low):
            return Intent("meal_plan", query=msg, raw=msg)
        # recipe search is matched before the generic "recipe" so "search recipes ..." works
        if re.search(r"\b(search|find|look up|browse)\b.*\b(recipes?|dishes?)\b|\bcook\b.*\brecipes\b", low):
            return Intent("recipe_search", query=msg, raw=msg)
        if re.search(r"\b(recipe library|my recipes|collected recipes|saved recipes|recipe book)\b", low):
            return Intent("recipe_library", raw=msg)
        if re.search(r"\b(meal history|recent (meals|recipes)|last meals|cooked (recently|lately))\b", low):
            return Intent("recipe_history", raw=msg)
        if re.search(r"\b(star|favorite|favou?rite)\b.*\brecipe\b|\brate\b.*\b[0-5]\b" +
                     r"|\b(star|favorite|favou?rite)\b\s+\S", low):
            return Intent("recipe_mark", query=msg, raw=msg)
        if re.search(r"\bfavorites?\b|\bfavourites?\b|my saved|bookmarked|show.*favorite", low):
            return Intent("favorites", raw=msg)
        if re.search(r"\b(what can i (cook|make|eat|have)|recipe|dinner idea|whats for|what's for)\b", low):
            return Intent("recipe", query=msg, raw=msg)

        # --- Phase 3: NAS / inbox ---
        if re.search(r"\b(inbox|incoming)\b.*\b(organize|process|file|sort)\b" +
                     r"|\b(organize|process|file|ingest)\b.*\binbox\b" +
                     r"|\b(nas|storage)\b.*\b(ingest|organize|process)\b", low):
            return Intent("nas_ingest", raw=msg)

        if re.search(r"\b(resume|cv)\b", low):
            return Intent("resume", raw=msg)
        if re.search(r"\b(delete|trash|remove|toss|clean)\b.*duplicate", low):
            return Intent("delete_duplicates", raw=msg)
        if "duplicate" in low:
            return Intent("duplicates", raw=msg)
        if re.search(r"\b(organi[sz]e|tidy|clean up|sort)\b", low):
            return Intent("organize", target=self._folder_arg(low, msg), raw=msg)
        if re.search(r"\b(create|make|new)\b.*(workspace|project|folder|dir)", low):
            return Intent("workspace", raw=msg)
        if re.search(r"\b(find|locate|where is|search|look for)\b", low):
            q = self._query_after(msg, ["find", "locate", "search", "look for", "where is", "explore"])
            return Intent("find", query=q, raw=msg) if q else Intent("find", raw=msg)
        if re.search(r"^.*\b(tell me about|what is|what are|whats|what's|describe|summarize)\b", low):
            return Intent("ask",
                          query=self._topic_after(msg, ["tell me about", "what is", "what are",
                                                        "whats", "what's", "describe", "summarize"]),
                          raw=msg)
        if re.search(r"\b(teach|tutor|learn|quiz|lesson|study|explain)\b", low):
            return Intent("teach",
                          query=self._topic_after(msg, ["teach me about", "teach me", "tutor me about",
                                                        "learn about", "explain", "quiz me on",
                                                        "lesson on", "study"]), raw=msg)
        if re.search(r"\b(list|show|ls)\b", low):
            return Intent("list", target=self._folder_arg(low, msg), raw=msg)
        if re.search(r"\b(restore|recover|undelete)\b", low):
            return Intent("recover", query=self._digits(msg), raw=msg)
        if re.search(r"\b(waste|trash bas|empt?y)\b.*trash", low) or "empty trash" in low:
            return Intent("empty_trash", raw=msg)
        if re.search(r"\b(trash)\b", low):
            return Intent("trash_list", raw=msg)
        if "move" in low:
            return Intent("move", query=msg, raw=msg)
        if "mkdir" in low or "the folder" in low:
            return Intent("mkdir", query=msg, raw=msg)
        return Intent("help", raw=msg)

    # ---------------------------------------------------------------- slash
    def _slash(self, cmd: str, arg: str, raw: str) -> Intent:
        cmd = cmd.lstrip("/").lower()
        if cmd in ("reset", "backup", "status", "storage", "index", "jsons", "help", "start"):
            return Intent(cmd, raw=raw)
        target = _strip_quotes(arg)
        if cmd == "list":
            return Intent("list", target=target or "~/Downloads", raw=raw)
        if cmd == "find":
            return Intent("find", query=target, raw=raw)
        if cmd == "search":
            return Intent("find", query=target, params={"semantic": True}, raw=raw)
        if cmd == "resume" or cmd == "cv":
            return Intent("resume", raw=raw)
        if cmd in ("ask", "chat", "q"):
            return Intent("ask", query=target, raw=raw)
        if cmd == "teach":
            return Intent("teach", query=target, raw=raw)
        if cmd == "organize":
            return Intent("organize", target=target or "~/Downloads", raw=raw)
        if cmd == "dupes":
            return Intent("duplicates", target=target or "~/Downloads", raw=raw)
        if cmd == "mkdir":
            return Intent("mkdir", target=target, raw=raw)
        if cmd == "trash":
            return Intent("trash_list", raw=raw)
        if cmd == "recover":
            return Intent("recover", query=self._digits(raw), raw=raw)
        if cmd == "empty" or cmd == "emptytrash":
            return Intent("empty_trash", raw=raw)
        # --- planner commands (Phase 2) ---
        if cmd == "day":
            return Intent("day", raw=raw)
        if cmd == "now":
            return Intent("now", raw=raw)
        if cmd in ("tasks", "todo"):
            return Intent("tasks", raw=raw)
        if cmd in ("task", "addtask", "todo-add", "new"):
            return Intent("add_task", query=target, raw=raw)
        if cmd in ("done", "finish", "complete"):
            return Intent("done", query=target, raw=raw)
        if cmd in ("skip", "snooze"):
            return Intent("skip", query=target, raw=raw)
        if cmd in ("begin", "go"):
            return Intent("start", query=target, raw=raw)
        if cmd in ("cameup", "came_up", "urgent"):
            return Intent("came_up", query=target, raw=raw)
        if cmd == "why":
            return Intent("why", raw=raw)
        if cmd == "undo":
            return Intent("undo", raw=raw)
        if cmd in ("resched", "reschedule", "replan"):
            return Intent("reschedule", raw=raw)
        if cmd in ("calendar", "connect", "link"):
            return Intent("connect", arg, raw=raw)
        # --- Phase 3: course intelligence ---
        if cmd in ("course", "course-add", "addcourse", "track", "add_course"):
            return Intent("course_add", query=target or arg, raw=raw)
        if cmd in ("courses", "course-list", "listcourses"):
            return Intent("course_list", raw=raw)
        if cmd in ("checkcourses", "checkcourse", "monitor", "scan"):
            return Intent("course_check", raw=raw)
        if cmd in ("materials", "coursedocs", "course-docs"):
            return Intent("course_docs", query=target, raw=raw)
        # --- Phase 3: food / chef ---
        if cmd in ("pantry", "food", "inventory", "whatdohave"):
            return Intent("food_list", raw=raw)
        if cmd in ("expiring", "expire", "useit"):
            return Intent("food_expiring", raw=raw)
        if cmd in ("addfood", "add-food", "stock", "logfood"):
            return Intent("food_add", query=target or arg, raw=raw)
        if cmd in ("used", "consume", "ate"):
            return Intent("food_consume", query=target or arg, raw=raw)
        if cmd in ("grocery", "shopping", "buylist"):
            return Intent("grocery", raw=raw)
        if cmd in ("meal", "mealplan", "planmeal", "dinner", "lunch"):
            return Intent("meal_plan", query=target or arg, raw=raw)
        if cmd in ("recipe", "cook", "suggest", "whatcanicook"):
            return Intent("recipe", query=target or arg, raw=raw)
        if cmd in ("searchrecipes", "findrecipes", "recipes"):
            return Intent("recipe_search", query=target or arg, raw=raw)
        if cmd in ("favorites", "favs", "favrecipes"):
            return Intent("favorites", raw=raw)
        if cmd in ("recipelibrary", "mrecipes", "library"):
            return Intent("recipe_library", raw=raw)
        if cmd in ("mealhistory", "history", "recentmeals"):
            return Intent("recipe_history", raw=raw)
        if cmd in ("favoriterecipe", "starrecipe"):
            return Intent("recipe_mark", query=target or arg, raw=raw)
        if cmd in ("rate",):
            return Intent("recipe_mark", query=target or arg, raw=raw)
        # --- Phase 3: NAS + context + proactive ---
        if cmd in ("ingest", "processinbox"):
            return Intent("nas_ingest", raw=raw)
        if cmd in ("context", "briefing", "brief", "status"):
            return Intent("context", raw=raw)
        if cmd in ("proactive", "checks"):
            return Intent("proactive", raw=raw)
        # --- Phase 4.1: presence ---
        if cmd in ("where", "whereami", "location"):
            return Intent("where_am_i", raw=raw)
        if cmd in ("around", "nearby"):
            return Intent("around_me", raw=raw)
        return Intent("help", raw=raw)

    # ---------------------------------------------------------------- handlers
    def resolve(self, intent: Intent, user: str = "user") -> Any:
        """Run a parsed intent and return a result object / Plan."""
        k = intent.kind
        if k == "help":
            return self._help()
        if k == "workspace":
            return self._do_workspace(intent, user)
        if k == "organize":
            return self._do_organize(intent)
        if k in ("find",):
            return self._do_find(intent)
        if k == "ask":
            q = (intent.query or "").strip()
            if not q:
                return {"kind": "help"}
            return {"kind": "chat", "mode": "ask", "answer": self.chat.answer(q)}
        if k == "teach":
            q = (intent.query or "").strip()
            if not q:
                return {"kind": "help"}
            return {"kind": "chat", "mode": "teach", "answer": self.chat.teach(q)}
        if k == "resume":
            return {"results": self.search.latest_resume(), "kind": "resume"}
        if k == "duplicates":
            return self._do_dupes(intent)
        if k == "delete_duplicates":
            return self._do_delete_dupes(intent)
        if k == "trash_list":
            from .trash import Trash
            t = Trash(self.cfg, self.db, self.engine)
            return {"kind": "trash", "items": t.list()}
        if k == "recover":
            from .trash import Trash
            t = Trash(self.cfg, self.db, self.engine)
            ids = [int(x) for x in intent.query.split() if x.isdigit()]
            results = [t.recover(i, user) for i in ids] if ids else []
            return {"kind": "recover", "results": results}
        if k == "empty_trash":
            from .trash import Trash
            t = Trash(self.cfg, self.db, self.engine)
            n = t.empty(user=user)
            return {"kind": "empty_trash", "removed": n}
        if k == "list":
            return self._do_list(intent)
        if k in ("status", "storage"):
            return {"kind": "status", "config": self.cfg.to_dict()}
        if k == "index":
            from .indexer import Indexer
            from .embed import Embedder
            idx = Indexer(self.cfg, self.db, Embedder(self.cfg.embed_model))
            stats = {}
            for root in self.cfg.roots + self.cfg.index_roots:
                stats[root] = idx.index_root(root)
            stats["pruned"] = idx.prune_missing()
            return {"kind": "index", "stats": stats}
        if k == "backup":
            from .backup import Backup
            return {"kind": "backup", **Backup(self.cfg, self.db).status()}
        # --- planner / scheduler (Phase 2) ---
        if k == "day":
            return {"kind": "day", **self.planner.plan_day()}
        if k == "now":
            return {"kind": "now", **self.planner.what_now()}
        if k == "tasks":
            return {"kind": "plan_tasks", "tasks": [dict(r) for r in self.db.tasks("active")]}
        if k == "add_task":
            return {"kind": "plan_tasks", "added": self._do_add_task(intent)}
        if k == "done":
            return {"kind": "plan_tasks", **self._do_state(intent, "done")}
        if k == "skip":
            return {"kind": "plan_tasks", **self._do_state(intent, "skip")}
        if k == "start":
            return {"kind": "plan_tasks", **self._do_state(intent, "start")}
        if k == "came_up":
            return {"kind": "plan_tasks", **self._do_cameup(intent)}
        if k == "why":
            return {"kind": "plan_why", **self.planner.why()}
        if k == "undo":
            return {"kind": "plan_tasks", **self.planner.undo()}
        if k == "reschedule":
            return {"kind": "day", **self.planner.reschedule()}
        if k == "connect":
            return {"kind": "connect", "url": "https://console.cloud.google.com",
                    "note": "Set client_secret.json then run `butler calendar connect`."}
        # --- Phase 3: course / food / nas / context / proactive ---
        if k == "course_add":
            return self._do_course_add(intent)
        if k == "course_list":
            return self._do_course_list()
        if k == "course_check":
            return self._do_course_check()
        if k == "course_docs":
            return self._do_course_docs(intent)
        if k == "course_assignments":
            return self._do_course_assignments(intent)
        if k == "food_add":
            return self._do_food_add(intent)
        if k == "food_list":
            return self._do_food_list()
        if k == "food_expiring":
            return self._do_food_expiring()
        if k == "food_consume":
            return self._do_food_consume(intent)
        if k == "recipe":
            return self._do_recipe(intent)
        if k == "recipe_search":
            return self._do_recipe_search(intent)
        if k == "recipe_library":
            return self._do_recipe_library()
        if k == "favorites":
            return self._do_favorites()
        if k == "recipe_history":
            return self._do_recipe_history()
        if k == "recipe_mark":
            return self._do_recipe_mark(intent)
        if k == "meal_plan":
            return self._do_meal_plan(intent)
        if k == "grocery":
            return self._do_grocery(intent)
        if k == "nas_ingest":
            return self._do_nas_ingest()
        if k == "context":
            return self._do_context()
        if k == "proactive":
            return self._do_proactive()
        if k == "where_am_i":
            return self._do_where_am_i()
        if k == "around_me":
            return self._do_around_me()
        return {"kind": "help"}

    # ---------------------------------------------------------------- Phase 3
    def _do_course_add(self, intent: Intent) -> dict[str, Any]:
        if self.courses is None:
            return {"kind": "course_add", "ok": False, "error": "courses not configured"}
        msg = intent.query or intent.raw
        code = extract_course_code(msg) or self._first_token(msg)
        url = self._course_url(msg)
        res = self.courses.add_course(code or "", url=url)
        if res.get("need_url"):
            return {"kind": "course_add", "ok": True,
                    "question": f"Added {code}. What is the course website URL?",
                    "need_url": True, "course_id": res["course_id"]}
        return {"kind": "course_add", "ok": True,
                "course": res.get("course"), "code": code.upper()}

    def _do_course_list(self) -> dict[str, Any]:
        if self.courses is None:
            return {"kind": "course_list", "ok": False, "error": "courses not configured"}
        return {"kind": "course_list", "courses": self.courses.list_courses()}

    def _do_course_check(self) -> dict[str, Any]:
        if self.courses is None:
            return {"kind": "course_check", "ok": False, "error": "courses not configured"}
        updates = self.courses.check_all()
        return {"kind": "course_check", "updates": updates, "count": len(updates)}

    def _do_course_assignments(self, intent: Intent) -> dict[str, Any]:
        if self.courses is None:
            return {"kind": "course_assignments", "ok": False,
                    "error": "courses not configured"}
        code = (intent.query or "").strip().upper()
        codes = [code] if code else [str(c["code"]) for c in self.courses.list_courses()]
        if not codes:
            return {"kind": "course_assignments", "ok": False,
                    "error": "no courses to sync"}
        results = {c: self.courses.sync_assignments(c) for c in codes}
        total = sum(r.get("count", 0) for r in results.values())
        return {"kind": "course_assignments", "results": results, "count": total}

    def _do_course_docs(self, intent: Intent) -> dict[str, Any]:
        if self.courses is None:
            return {"kind": "course_docs", "ok": False, "error": "courses not configured"}
        code = (intent.query or "").strip()
        docs = []
        if code:
            course = self.courses.course(code)
            if course:
                docs = [dict(r) for r in self.db.course_documents(int(course["id"]))]
        return {"kind": "course_docs", "code": code.upper(), "documents": docs}

    def _do_food_add(self, intent: Intent) -> dict[str, Any]:
        if self.food is None:
            return {"kind": "food_add", "ok": False, "error": "food not configured"}
        text = self._food_note(intent)
        parsed = self.food.parse(text)["added"]
        added = [self.food.add(i["name"], i["quantity"], i["unit"])
                 for i in parsed]
        return {"kind": "food_add", "added": added, "count": len(added)}

    def _do_food_list(self) -> dict[str, Any]:
        if self.food is None:
            return {"kind": "food_list", "ok": False, "error": "food not configured"}
        return {"kind": "food_list", "items": self.food.all()}

    def _do_food_expiring(self) -> dict[str, Any]:
        if self.food is None:
            return {"kind": "food_expiring", "ok": False, "error": "food not configured"}
        return {"kind": "food_expiring", "items": self.food.expiring(3)}

    def _do_food_consume(self, intent: Intent) -> dict[str, Any]:
        if self.food is None:
            return {"kind": "food_consume", "ok": False, "error": "food not configured"}
        msg = intent.query or intent.raw
        name = self._consume_name(msg)
        res = self.food.consume(name)
        return {"kind": "food_consume", **res}

    @staticmethod
    def _consume_name(msg: str) -> str:
        low = msg.lower()
        rest = msg
        for kw in ("used", "ate", "cooked", "consumed", "used up"):
            idx = low.find(kw)
            if idx >= 0:
                rest = msg[idx + len(kw):]
                break
        rest = re.sub(r"^\s*(some|the|all|of|both|a couple|a few|couple|two|one|a|an)\s*",
                      "", rest, flags=re.I)
        rest = re.sub(r"^[0-9]+(?:\.[0-9]+)?\s*", "", rest)
        return rest.strip().lower().strip(" .")

    def _do_recipe(self, intent: Intent) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "recipe", "ok": False, "error": "chef not configured"}
        budget = self._free_budget()
        plan = self.chef.plan_meal(budget_minutes=budget)
        return {"kind": "recipe", "plan": plan, "budget_minutes": budget}

    def _do_meal_plan(self, intent: Intent) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "meal_plan", "ok": False, "error": "chef not configured"}
        msg = intent.query or intent.raw
        meal = self._meal_name(msg)
        budget = self._free_budget()
        plan = self.chef.plan_meal(budget_minutes=budget, meal=meal)
        return {"kind": "meal_plan", "plan": plan, "meal": meal,
                "budget_minutes": budget}

    def _do_grocery(self, intent: Intent) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "grocery", "ok": False, "error": "chef not configured"}
        items = self.chef.grocery_list()
        return {"kind": "grocery", "items": items}

    @staticmethod
    def _recipe_query(msg: str) -> str:
        low = msg.lower()
        for kw in ("search", "find", "look up", "recipes for", "recipe for", "whats a good"):
            idx = low.find(kw)
            if idx >= 0:
                text = msg[idx + len(kw):]
                break
        else:
            text = msg
        return re.sub(r"^[\s,:;]+", "", text).strip().strip(".").strip()

    def _do_recipe_search(self, intent: Intent) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "recipe_search", "ok": False, "error": "chef not configured"}
        query = self._recipe_query(intent.query or intent.raw)
        results = self.chef.search(query)
        return {"kind": "recipe_search", "query": query, "results": results}

    def _do_recipe_library(self) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "recipe_library", "ok": False, "error": "chef not configured"}
        lib = self.chef.library()
        return {"kind": "recipe_library", "count": len(lib), "recipes": lib}

    def _do_favorites(self) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "favorites", "ok": False, "error": "chef not configured"}
        favs = self.chef.favorites()
        return {"kind": "favorites", "count": len(favs), "recipes": favs}

    def _do_recipe_history(self) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "recipe_history", "ok": False, "error": "chef not configured"}
        hist = self.chef.history()
        return {"kind": "recipe_history", "count": len(hist), "history": hist}

    def _find_recipe(self, name: str) -> Any | None:
        if self.chef is None:
            return None
        db = getattr(self.chef, "db", None)
        if db is None:
            return None
        row = db.recipe_by_name(name)
        if row:
            return row
        if name:
            for r in db.recipes():
                if name.lower() in str(r["name"]).lower():
                    return r
        return None

    def _do_recipe_mark(self, intent: Intent) -> dict[str, Any]:
        if self.chef is None:
            return {"kind": "recipe_mark", "ok": False, "error": "chef not configured"}
        msg = (intent.query or intent.raw).strip()
        low = msg.lower()
        rating: float | None = None
        m = re.search(r"\b([0-5](?:\.\d+)?)\b", low)
        if m and "rate" in low:
            rating = float(m.group(1))
        fav = "unfavorite" not in low and "unfav" not in low
        name = msg
        if rating is not None:
            name = re.sub(r"\b[0-5](?:\.\d+)?\b", " ", name)
        name = re.sub(r"\b(favorite|favou?rite|star|rate|recipe|as|it|to|a)\b",
                      " ", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip().strip(" .")
        if not name:
            return self._do_favorites()
        row = self._find_recipe(name)
        if row is None:
            return {"kind": "recipe_mark", "ok": False, "error": "recipe not found"}
        rid = int(row["id"])
        if rating is not None:
            self.chef.rate(rid, rating)
        self.chef.set_favorite(rid, fav)
        return {"kind": "recipe_mark", "recipe_id": rid, "favorite": fav,
                "rating": rating, "ok": True}

    def _do_nas_ingest(self) -> dict[str, Any]:
        if self.nas is None:
            return {"kind": "nas_ingest", "ok": False, "error": "nas not configured"}
        res = self.nas.ingest_inbox()
        return {"kind": "nas_ingest", **res}

    def _do_context(self) -> dict[str, Any]:
        if self.context is None:
            return {"kind": "context", "ok": False, "error": "context not configured"}
        return {"kind": "context", "snapshot": self.context.snapshot()}

    def _do_proactive(self) -> dict[str, Any]:
        if self.proactive is None:
            return {"kind": "proactive", "ok": False, "error": "proactive not configured"}
        return {"kind": "proactive", "messages": self.proactive.collect()}

    def _do_where_am_i(self) -> dict[str, Any]:
        return {"kind": "where_am_i", "presence": self._presence()}

    def _do_around_me(self) -> dict[str, Any]:
        snap = self._snapshot()
        return {"kind": "around_me", "presence": snap.get("presence", {}),
                "events_today": snap.get("events_today", []),
                "free_minutes_today": snap.get("free_minutes_today", 0)}

    def _snapshot(self) -> dict[str, Any]:
        if self.context is None:
            return {}
        try:
            return self.context.snapshot()
        except Exception:  # noqa: BLE001  (presence must never break the intent)
            return {}

    def _presence(self) -> dict[str, Any]:
        return self._snapshot().get("presence", {
            "known": False, "zone": "", "status": "unknown",
            "battery": None, "available": False, "source": "home_assistant"})

    # ---- Phase 3 helpers ----
    def _free_budget(self) -> int:
        if self.context is not None:
            try:
                s = self.context.snapshot()
                free = int(s.get("free_minutes_today", 60))
                return max(20, min(free, 120))
            except Exception:  # pragma: no cover
                pass
        return 45

    @staticmethod
    def _first_token(msg: str) -> str:
        m = re.search(r"\b([A-Z]{2,4}\d{2,4})\b", msg.upper())
        return m.group(1) if m else ""

    @staticmethod
    def _course_url(msg: str) -> str:
        m = re.search(r"(https?://\S+)", msg)
        if m:
            return m.group(1).strip()
        m = re.search(r"(/\S+)", msg)  # an absolute path (offline/local feed)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _food_note(intent: Intent) -> str:
        msg = intent.query or intent.raw
        low = msg.lower()
        for kw in ("add", "log", "record", "bought"):
            idx = low.find(kw)
            if idx >= 0:
                msg = msg[idx + len(kw):]
                break
        note = msg.strip().lstrip(": ").strip()
        note = re.sub(r"^(my|to|the|in|into|onto|some)\s+", "", note, flags=re.I)
        note = re.sub(r"^(pantry|fridge|freezer|kitchen|cabinet|shelf|stock)\b\s*",
                      "", note, flags=re.I)
        note = re.sub(r"\s+(to\s+(my|the)?\s*)?(pantry|fridge|freezer|kitchen|cabinet)$",
                      "", note, flags=re.I)
        return note.strip()

    @staticmethod
    def _meal_name(msg: str) -> str:
        low = msg.lower()
        for kw in ("breakfast", "lunch", "dinner", "supper"):
            if kw in low:
                return kw
        return "supper"

    # ---------------------------------------------------------------- pieces
    def _do_workspace(self, intent: Intent, user: str) -> Plan:
        """Test 1: create a workspace for a project."""
        msg = intent.raw
        code = extract_course_code(msg)
        base = self.organizer.course_dir_for(code) if code else (self.cfg.course_dir or self.cfg.data_dir)
        proj = self._project_name(msg)
        workspace = os.path.join(base, "Projects", proj)
        plan = Plan(plan_id="ws_" + os.urandom(4).hex(),
                    title=f"Create workspace {workspace}")
        plan.items.append(PlanItem("mkdir", base, workspace, "project workspace"))
        for sub in ("Material", "Submissions", "Notes"):
            plan.items.append(PlanItem("mkdir", workspace, os.path.join(workspace, sub),
                                       "workspace subfolder"))
        return plan

    def _do_organize(self, intent: Intent) -> Plan:
        target = intent.target or "~/Downloads"
        target = os.path.abspath(os.path.expanduser(target))
        return self.organizer.plan_organize(target)

    def _do_find(self, intent: Intent) -> dict[str, Any]:
        q = intent.query.strip()
        if intent.params.get("semantic"):
            return {"kind": "find", "results": self.search.semantic(q), "query": q,
                    "mode": "semantic"}
        return {"kind": "find", "results": self.search.combined(q), "query": q, "mode": "combined"}

    def _do_dupes(self, intent: Intent) -> dict[str, Any]:
        target = intent.target or self._scan_root()
        groups = self.engine.detect_duplicates(target)
        return {"kind": "dupes", "groups": groups, "root": target}

    def _do_delete_dupes(self, intent: Intent) -> Plan:
        target = intent.target or self._scan_root()
        groups = self.engine.detect_duplicates(target)
        plan = Plan(plan_id="del_" + os.urandom(4).hex(), title=f"Trash duplicates in {target}")
        for g in groups:
            keep = g["files"][0][0]  # primary (cleanest/oldest) is kept
            for path, _size, _mtime in g["files"][1:]:
                plan.items.append(PlanItem("trash", path, "", "duplicate of " + os.path.basename(keep)))
        if not plan.items:
            plan.title = "No duplicates found"
        return plan

    def _scan_root(self) -> str:
        """Choose a managed root to scan when the user doesn't name one."""
        for root in self.cfg.roots:
            if os.path.basename(root).lower() in ("downloads", "documents", "desktop"):
                return root
        return (self.cfg.roots or [self.cfg.data_dir])[0]

    # ------------------------------------------------------------ planner
    def _do_add_task(self, intent: Intent) -> dict[str, Any]:
        msg = intent.query or intent.raw
        title = self._task_title(msg)
        est = self._int_kw(msg, "est", 60)
        prio = self._int_kw(msg, "priority", 3)
        due = self._deadline_minutes(msg)
        tid = self.planner.add_task(title, est_minutes=est, priority=prio,
                                    deadline=due)
        self.planner.reschedule()
        return {"task_id": tid, "title": title, "est_minutes": est, "priority": prio}

    def _do_state(self, intent: Intent, action: str) -> dict[str, Any]:
        task_id = self._task_id_from(intent.query or intent.raw)
        if task_id is None:
            return {"ok": False, "error": f"I can't tell which task to {action}. "
                    "Use `/tasks` to see ids, or name it."}
        if action == "done":
            return self.planner.done(task_id)
        if action == "skip":
            return self.planner.skip(task_id)
        return self.planner.start(task_id)

    def _do_cameup(self, intent: Intent) -> dict[str, Any]:
        msg = intent.query or intent.raw
        title = self._task_title(msg)
        est = self._int_kw(msg, "est", 60)
        prio = self._int_kw(msg, "priority", 4)
        return self.planner.came_up(title, est_minutes=est, priority=prio)

    def _task_id_from(self, msg: str) -> int | None:
        digits = re.findall(r"\b(\d+)\b", msg)
        if len(digits) == 1:
            tid = int(digits[0])
            if self.db.task_by_id(tid):
                return tid
        low = msg.lower()
        for r in self.db.tasks("active"):
            t = r["title"].lower()
            if t and (t in low or all(w in low for w in t.split())):
                return int(r["id"])
        return None

    def _task_title(self, msg: str) -> str:
        # drop leading verb / command words
        cleaned = re.sub(r"^(add( a)? task|new task|task:?|todo:?|remind me to|"
                         r"i need to|urgent|came up|something came up|to|do)\s*", "",
                         msg.strip(), flags=re.I)
        # drop trailing options like --est 30
        cleaned = re.sub(r"\s+--[\w-]+(\s+\d+)?", "", cleaned)
        return cleaned.strip()[:120] or "Untitled task"

    def _int_kw(self, msg: str, key: str, default: int) -> int:
        m = re.search(rf"--{key}\s+(\d+)", msg)
        if m:
            return int(m.group(1))
        return default

    def _deadline_minutes(self, msg: str) -> int:
        # accepts "--due 18:30" / "--deadline 18:30" -> unix ts for today, or
        # tomorrow if that time has already passed.
        m = re.search(r"--due(?:line)?\s+(\d{1,2}):(\d{2})", msg)
        if not m:
            return 0
        from datetime import datetime
        h, mi = int(m.group(1)), int(m.group(2))
        now_dt = datetime.now()
        dl = now_dt.replace(hour=h, minute=mi, second=0, microsecond=0)
        if dl.timestamp() < now_dt.timestamp():
            dl = dl.replace(day=dl.day + 1)
        return int(dl.timestamp())

    def _do_list(self, intent: Intent) -> dict[str, Any]:
        target = intent.target or os.path.abspath(os.path.expanduser("~/Downloads"))
        return {"kind": "list", **self.engine.list_dir(target)}

    def _help(self) -> dict[str, Any]:
        return {"kind": "help", "text": (
            "Butler — file assistant. Try:\n"
            " /list <folder>\n /find <term>   (filename+content)\n"
            " /search <term>  (semantic)\n /hybrid <term> (semantic+keyword)\n"
            " /ask <question>   /teach <topic>\n"
            " /resume\n /organize <folder>\n"
            " /dupes <folder>\n /trash  |  /recover <id>  |  /empty\n"
            " /mkdir <path>\n /index /backup /storage\n"
            " or just type naturally, e.g. \"what is paxos\", \"teach me about graphs\"."
        )}

    # ---------------------------------------------------------------- helpers
    def _topic_after(self, msg: str, phrases: list[str]) -> str:
        low = msg.lower()
        for phrase in phrases:
            idx = low.find(phrase)
            if idx >= 0:
                rest = msg[idx + len(phrase):].strip()
                rest = _strip_quotes(rest.lstrip(":=").strip())
                if rest:
                    return rest
        words = [w for w in msg.split() if not self._is_stop(w)]
        return " ".join(words)

    def _query_after(self, msg: str, verbs: list[str]) -> str:
        low = msg.lower()
        for v in verbs:
            idx = low.find(v)
            if idx >= 0:
                rest = msg[idx + len(v):].strip()
                if rest:
                    return _strip_quotes(rest.lstrip(":").strip())
        # fallback: the whole message minus command words
        rest = " ".join(w for w in msg.split() if not self._is_stop(w))
        return rest

    def _folder_arg(self, low: str, msg: str) -> str:
        # 1) explicit path (contains '/', '~', or is quoted)
        if re.search(r"[/~]", msg):
            candidate = _strip_quotes(msg)
            candidate = os.path.expanduser(candidate.split("organize", 1)[-1]
                                           if "organize" in low else candidate)
            cand = candidate.lstrip(":").strip()
            if cand:
                return os.path.abspath(os.path.expanduser(cand))
        # 2) match an existing managed root by basename (sandbox/real aware)
        for root in self.cfg.roots:
            if os.path.basename(root).lower() in low:
                return root
        # 3) common folder keywords
        folders = {
            "downloads": "~/Downloads", "documents": "~/Documents",
            "desktop": "~/Desktop", "pictures": "~/Pictures",
            "videos": "~/Videos", "music": "~/Music",
            "university": self.cfg.course_dir, "incoming": self.cfg.incoming_dir,
            "inbox": self.cfg.incoming_dir, "drop": self.cfg.incoming_dir,
        }
        for word, path in folders.items():
            if re.search(rf"\b{word}\b", low) and path:
                return os.path.abspath(os.path.expanduser(path))
        # 4) fallback: last alphabetic word as a subdir path, else first root
        words = msg.split()
        if words:
            tail = _strip_quotes(words[-1])
            if tail.lower() not in ("me", "this", "folder", "directory", "files", "in", "now"):
                return os.path.abspath(os.path.expanduser(tail))
        return self.cfg.roots[0] if self.cfg.roots else self.cfg.data_dir

    def _project_name(self, msg: str) -> str:
        stop = {"for", "my", "named", "called", "about", "of", "the", "a", "an", "in", "on"}
        for m in re.finditer(r"\b(?:project|workspace)\s+[:\-]?\s*([A-Za-z0-9_\-]{2,40})", msg, re.I):
            name = m.group(1).strip().strip(".")
            if name.lower() not in stop:
                return name
        return "Project_1A"

    @staticmethod
    def _digits(msg: str) -> str:
        return " ".join(re.findall(r"\d+", msg))

    @staticmethod
    def _is_stop(w: str) -> bool:
        return w.lower() in {
            "find", "for", "the", "search", "where", "is", "my", "locate", "look",
            "me", "please", "show", "of", "in", "a", "an", "to", "list",
            "what", "explain", "teach", "learn", "about", "on", "tell", "and", "or",
            "are", "was", "it", "this", "that", "i", "you", "us", "how", "why",
        }


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s
