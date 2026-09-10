"""Butler core — dependency container that wires everything together.

This is the single entrypoint used by the CLI (`main.py`), the Telegram bot,
and the remote HTTP API. It ensures there is exactly one Config, one DB
connection, one Engine (the safety layer), and the decider/organiser/search
on top.
"""

from __future__ import annotations

from typing import Any

from . import affinity
from .backup import Backup
from .chat import Chat
from .config import Config
from .db import DB
from .decider import Decider, Intent
from .embed import Embedder
from .engine import Engine
from .indexer import Indexer
from .organizer import Organizer
from .planner import Planner
from .search import Search
from .trash import Trash
from .course import CourseIntelligence
from .projects import ProjectIntelligence
from .web import WebKnowledge
from .optimizer import ScheduleOptimizer
from .memory import Memory
from .proactive_engine import ProactiveEngine
from .topics import TopicStore
from .food import Chef, FoodInventory
from .nas import FileManager
from .house import HomeAssistant
from .context import ContextEngine
from .proactive import Proactive
from .timeline import Timeline
from .routines import Routines
from .foodplan import FoodPlanner
from .executive import Executive
from .audit import Audit
from .idempotency import Idempotency
from .safety import SafetyPolicy
from .retry import RetryPolicy
from .health import Health
from .recovery import Recovery


class Container:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config.load()
        self.cfg.ensure_dirs()
        # Install user-overridable affinity tables (audit: no hardcoded mapping).
        affinity.configure(
            keywords=self.cfg.affinity_keywords or None,
            zone_keywords=self.cfg.affinity_zone_keywords or None,
            unknown_category=self.cfg.affinity_unknown_category,
        )
        self.db = DB(self.cfg)
        # --- Settings + reward library (motivation). Seed the default treats. ---
        from . import motivation
        motivation.seed_rewards(self.db)
        # --- Phase 6: reliability, safety & recovery (built early so every
        # subsystem can use the audit / safety / idempotency / retry gates) ---
        self.audit = Audit(self.db)
        self.idempotency = Idempotency(self.db)
        self.retry = RetryPolicy(self.cfg)
        self.safety = SafetyPolicy(self)
        self.health = Health(self)
        self.recovery = Recovery(self)
        self.health.beat("db", note="container started")
        self.timeline = Timeline(self.cfg, self.db)
        self.routines = Routines(self)
        self.engine = Engine(self.cfg)
        self.embedder = Embedder(self.cfg.embed_model)
        self.search = Search(self.cfg, self.db, self.embedder)
        self.chat = Chat(self.cfg, self.db, self.search)
        self.organizer = Organizer(self.cfg, self.db, self.engine)
        self.planner = Planner(self)
        # --- Phase 3 subsystems ---
        self.courses = CourseIntelligence(self)
        # --- M3: project intelligence (needs planner geometry for risk) ---
        self.projects = ProjectIntelligence(self)
        # --- M4: web & external knowledge (read-only, provider abstraction) ---
        self.web = WebKnowledge(self)
        # --- M5: deterministic multi-day schedule optimization (read-only) ---
        self.optimizer = ScheduleOptimizer(self)
        # --- M6: long-term memory + learning (durable, provenance-aware) ---
        self.memory = Memory(self)
        # --- N1: durable topic profiles (context/view over shared domain data) ---
        self.topics = TopicStore(self)
        self.food = FoodInventory(self)
        self.chef = Chef(self)
        # NAS dirs must be inside the managed roots for the deterministic engine.
        if self.cfg.nas_enabled and self.cfg.nas_dir:
            self.cfg.roots = list(self.cfg.roots) + [self.cfg.nas_dir]
            self.cfg.index_roots = list(self.cfg.index_roots) + [self.cfg.nas_dir]
        self.nas = FileManager(self)
        self.ha = HomeAssistant(self.cfg)
        self.context = ContextEngine(self)
        self.foodplan = FoodPlanner(self)
        self.executive = Executive(self)
        self.proactive = Proactive(self)
        # --- M7: deterministic proactive executive engine (candidates + policy) ---
        self.proactive_engine = ProactiveEngine(self)
        self.decider = Decider(self.cfg, self.db, self.engine,
                               self.organizer, self.search, self.chat,
                               planner=self.planner, courses=self.courses,
                               food=self.food, chef=self.chef, nas=self.nas,
                               context=self.context, proactive=self.proactive,
                               timeline=self.timeline, routines=self.routines,
                               foodplan=self.foodplan, executive=self.executive,
                               container=self)
        # --- Phase 7 / M1: typed agent runtime (additive; never required) ---
        try:
            from .agent import build_runtime
            self.agent = build_runtime(self)
        except Exception:  # noqa: BLE001 — the legacy paths must keep working
            self.agent = None
        self.trash = Trash(self.cfg, self.db, self.engine)
        self.indexer = Indexer(self.cfg, self.db, self.embedder)
        self.backup = Backup(self.cfg, self.db)

    # ------------------------------------------------------------------ remote
    def remote_route(self, method: str, route: str, args: dict[str, Any]) -> Any:
        if method == "GET":
            if route in ("", "/status", "/storage"):
                return {"ok": True, "config": self.cfg.to_dict()}
            if route == "/list":
                return {"ok": True, **self.engine.list_dir(args.get("path", "~/Downloads"))}
            if route == "/find":
                q = args.get("q", args.get("query", ""))
                return {"ok": True, "results": self.search.combined(q)}
            if route == "/search":
                q = args.get("q", args.get("query", ""))
                return {"ok": True, "results": self.search.semantic(q), "mode": "semantic"}
            if route == "/hybrid":
                q = args.get("q", args.get("query", ""))
                return {"ok": True, "results": self.search.hybrid(q), "mode": "hybrid"}
            if route == "/resume":
                return {"ok": True, "results": self.search.latest_resume()}
            if route == "/ask":
                q = args.get("q", args.get("query", ""))
                return {"ok": True, "answer": self.chat.answer(q)}
            if route == "/teach":
                q = args.get("q", args.get("topic", ""))
                return {"ok": True, "lesson": self.chat.teach(q)}
            if route == "/dupes":
                root = args.get("root", "~/Downloads")
                return {"ok": True, "groups": self.engine.detect_duplicates(root)}
            if route == "/trash":
                return {"ok": True, "items": self.trash.list()}
            if route == "/backup":
                return {"ok": True, **self.backup.status()}
            if route == "/health":
                return {"ok": self.health.ready(), **self.health.status()}
            if route == "/audit":
                action = args.get("action", "")
                actor = args.get("actor", "")
                lim = int(args.get("limit", 50))
                return {"ok": True,
                        "entries": self.audit.recent(limit=lim, action=action, actor=actor)}
            if route == "/undo":
                return {"ok": True, **self.recovery.undo(args.get("id", ""))}
            if route == "/day":
                return {"ok": True, **self.planner.plan_day()}
            if route == "/now":
                return {"ok": True, **self.planner.what_now()}
            if route == "/why":
                return {"ok": True, **self.planner.why()}
            if route == "/tasks":
                return {"ok": True, "tasks": [dict(r) for r in self.db.tasks("active")]}
            if route == "/context":
                return {"ok": True, "context": self.context.snapshot()}
            if route == "/briefing":
                return {"ok": True, "briefing": self.executive.briefing()}
            if route == "/review":
                return {"ok": True, "review": self.executive.review()}
            if route == "/routines":
                return {"ok": True, "active": self.routines.active(),
                        "candidates": self.routines.candidates()}
            if route == "/courses":
                return {"ok": True, "courses": self.courses.list_courses()}
            if route == "/documents":
                code = args.get("code", "")
                course = self.courses.course(code) if code else self.courses.list_courses()[:1]
                docs = []
                if course:
                    docs = [dict(r) for r in self.db.course_documents(int(course["id"]))]
                return {"ok": True, "documents": docs}
            if route == "/food":
                return {"ok": True, "items": self.food.all()}
            if route == "/expiring":
                return {"ok": True, "items": self.food.expiring(int(args.get("days", 3)))}
            if route == "/grocery":
                return {"ok": True, "items": [dict(r) for r in self.db.shopping(0)]}
            if route == "/nas":
                return {"ok": True, **self.nas.list_root()}
        elif method == "POST":
            if route == "/organize":
                plan = self.organizer.plan_organize(args.get("path", "~/Downloads"))
                return {"ok": True, "plan_id": plan.plan_id, "title": plan.title,
                        "summary": plan.summary, "items": self._plans_repr(plan)}
            if route == "/trash":
                paths = args.get("paths", [])
                moved = self.trash.trash_files(paths, args.get("reason", "api"))
                return {"ok": True, "moved": moved}
            if route == "/recover":
                ids = args.get("ids", [])
                return {"ok": True, "results": [self.trash.recover(i) for i in ids]}
            if route == "/mkdir":
                return {"ok": True, "path": self.engine.mkdir(args.get("path", ""))}
            if route == "/task":
                tid = self.planner.add_task(
                    args.get("title", ""), est_minutes=int(args.get("est_minutes", 60)),
                    deadline=int(args.get("deadline", 0)),
                    priority=int(args.get("priority", 3)),
                    tags=args.get("tags", ""))
                return {"ok": True, "task_id": tid}
            if route == "/done":
                return {"ok": True, **self.planner.done(int(args.get("task_id", 0)))}
            if route == "/skip":
                return {"ok": True, **self.planner.skip(int(args.get("task_id", 0)))}
            if route == "/start":
                return {"ok": True, **self.planner.start(int(args.get("task_id", 0)))}
            if route == "/cameup":
                return {"ok": True, **self.planner.came_up(
                    args.get("title", ""), est_minutes=int(args.get("est_minutes", 60)),
                    deadline=int(args.get("deadline", 0)),
                    priority=int(args.get("priority", 4)))}
            if route == "/undo":
                return self.planner.undo()
            if route == "/reschedule":
                return {"ok": True, **self.planner.reschedule()}
            if route == "/connect":
                return {"ok": False, "error": "Run `butler calendar connect` in a terminal."}
            if route == "/course/add":
                return {"ok": True, **self.courses.add_course(
                    args.get("code", ""), name=args.get("name", ""),
                    url=args.get("url", ""), platform=args.get("platform", ""),
                    semester=args.get("semester", ""))}
            if route == "/course/check":
                return {"ok": True, "updates": self.courses.check_all()}
            if route == "/food/add":
                added = self.food.parse(args.get("note", ""))["added"]
                return {"ok": True, "added": [self.food.add(i["name"], i["quantity"], i["unit"])
                                              for i in added]}
            if route == "/food/consume":
                return {"ok": True, **self.food.consume(args.get("name", ""))}
            if route == "/recipe":
                budget = int(args.get("budget_minutes", 45))
                return {"ok": True, **self.chef.plan_meal(budget_minutes=budget)}
            if route == "/nas/ingest":
                return {"ok": True, **self.nas.ingest_inbox()}
            if route == "/proactive":
                return {"ok": True, **self.proactive.run()}
            if route == "/routines/scan":
                return {"ok": True, **self.routines.scan()}
            if route == "/routines/confirm":
                return self.routines.confirm(args.get("id"))
            if route == "/routines/reject":
                return self.routines.reject(args.get("id"))
            if route == "/routines/forget":
                return self.routines.forget(args.get("id"))
            if route == "/db/backup":
                return {"ok": True, **self.recovery.db_backup(args.get("note", ""))}
            if route == "/db/restore":
                return {"ok": True, **self.recovery.db_restore(args.get("path", ""))}
            if route == "/mode":
                return {"ok": True, **self.health.set_mode(
                    degraded=bool(args.get("degraded")),
                    offline=bool(args.get("offline")))}
            return {"ok": False, "error": "unknown route"}
        return {"ok": False, "error": "unsupported"}

    @staticmethod
    def _plans_repr(plan: Any) -> list[dict[str, Any]]:
        return [{"action": i.action, "src": i.src, "dest": i.dest, "reason": i.reason}
                for i in plan.items]
