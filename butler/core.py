"""Butler core — dependency container that wires everything together.

This is the single entrypoint used by the CLI (`main.py`), the Telegram bot,
and the remote HTTP API. It ensures there is exactly one Config, one DB
connection, one Engine (the safety layer), and the decider/organiser/search
on top.
"""

from __future__ import annotations

from typing import Any

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


class Container:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config.load()
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg)
        self.engine = Engine(self.cfg)
        self.embedder = Embedder(self.cfg.embed_model)
        self.search = Search(self.cfg, self.db, self.embedder)
        self.chat = Chat(self.cfg, self.db, self.search)
        self.organizer = Organizer(self.cfg, self.db, self.engine)
        self.planner = Planner(self)
        self.decider = Decider(self.cfg, self.db, self.engine,
                               self.organizer, self.search, self.chat,
                               planner=self.planner)
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
            if route == "/day":
                return {"ok": True, **self.planner.plan_day()}
            if route == "/now":
                return {"ok": True, **self.planner.what_now()}
            if route == "/why":
                return {"ok": True, **self.planner.why()}
            if route == "/tasks":
                return {"ok": True, "tasks": [dict(r) for r in self.db.tasks("active")]}
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
            return {"ok": False, "error": "unknown route"}
        return {"ok": False, "error": "unsupported"}

    @staticmethod
    def _plans_repr(plan: Any) -> list[dict[str, Any]]:
        return [{"action": i.action, "src": i.src, "dest": i.dest, "reason": i.reason}
                for i in plan.items]
