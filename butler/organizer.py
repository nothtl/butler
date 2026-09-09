"""Organiser + decider.

Implements the design principle: *the AI decides what makes sense, the
deterministic engine decides what is allowed, the filesystem does the work.*

Features 10 (organise this folder), 15 (classification), 16 (automatic
organisation), 12 (confirmation before bulk mutations).

A :class:`Plan` is a list of proposed operations. It is *never* applied
without an explicit confirmation — see ``apply_plan(plan, confirm=True)``.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .db import DB
from .engine import Engine, EngineError, classify_by_ext, extract_course_code


@dataclass
class PlanItem:
    action: str            # move | trash | mkdir | rename
    src: str
    dest: str = ""
    reason: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Plan:
    plan_id: str
    title: str
    items: list[PlanItem] = field(default_factory=list)
    created_at: int = field(default_factory=lambda: int(time.time()))
    confirmed: bool = False

    @property
    def summary(self) -> str:
        moves = sum(1 for i in self.items if i.action == "move")
        trash = sum(1 for i in self.items if i.action == "trash")
        mkdirs = sum(1 for i in self.items if i.action == "mkdir")
        parts = []
        if moves:
            parts.append(f"{moves} move(s)")
        if trash:
            parts.append(f"{trash} to trash")
        if mkdirs:
            parts.append(f"{mkdirs} folder(s)")
        return ", ".join(parts) if parts else "no changes"

    def describe(self) -> str:
        lines = [f"📋 {self.title}", "", f"Plan {self.summary}.", ""]
        for i in self.items:
            if i.action == "move":
                lines.append(f"    {i.src}")
                lines.append(f"  → {i.dest}   ({i.reason})")
            elif i.action == "trash":
                lines.append(f"    🗑 {i.src}   ({i.reason})")
            elif i.action == "mkdir":
                lines.append(f"    📁 {i.dest}")
        lines.append("")
        lines.append("Confirm to apply, or cancel.")
        return "\n".join(lines)


class Organizer:
    def __init__(self, cfg: Config, db: DB, engine: Engine):
        self.cfg = cfg
        self.db = db
        self.engine = engine

    # --------------------- feature 10: organise a folder ---------------------
    def plan_organize(self, folder: str) -> Plan:
        """Scan a folder and propose moves into category subdirectories,
        reusing existing folders where present."""
        folder = self.engine.require_inside(folder)
        items = self.engine.list_dir(folder)["entries"]
        plan = Plan(plan_id=uuid.uuid4().hex[:8], title=f"Organize {folder}")
        needed_dirs: list[str] = []
        for e in items:
            if e["dir"]:
                continue
            cat = classify_by_ext(e["name"])
            if cat == "Other":
                continue
            dest_dir = os.path.join(folder, cat)
            target, _ = self.engine._unique_name(dest_dir, e["name"])
            plan.items.append(PlanItem("move", e["path"], target, f"→ {cat}",
                                       {"category": cat}))
            if not os.path.isdir(dest_dir) and dest_dir not in needed_dirs:
                needed_dirs.append(dest_dir)
        for d in needed_dirs:
            plan.items.insert(0, PlanItem("mkdir", folder, d, f"create {os.path.basename(d)} folder"))
        return plan

    # --------------------- feature 16: automatic organisation ---------------------
    def plan_for_incoming(self) -> Plan:
        """Route files from the incoming drop-box into the right locations."""
        if not self.cfg.incoming_dir or not os.path.isdir(self.cfg.incoming_dir):
            return Plan(uuid.uuid4().hex[:8], "No incoming folder")
        plan = Plan(uuid.uuid4().hex[:8], "Organize incoming")
        for e in self.engine.list_dir(self.cfg.incoming_dir)["entries"]:
            if e["dir"]:
                continue
            dest_dir = self.route_file(e["path"])
            if dest_dir:
                target, _ = self.engine._unique_name(dest_dir, e["name"])
                plan.items.append(PlanItem("move", e["path"], target,
                                           self.course_or_cat(e["name"])))
        return plan

    def route_file(self, path: str) -> str:
        """Decide where a single dropped file belongs (course vs category)."""
        base = self.cfg.course_dir or self.cfg.data_dir
        name = os.path.basename(path)
        # course code in filename
        code = extract_course_code(name)
        if not code and self._has_text(path):
            code = extract_course_code(self._peek_text(path))
        if code:
            return self.course_dir_for(code)
        cat = classify_by_ext(name)
        return self.category_dir_for(cat)

    def course_dir_for(self, code: str) -> str:
        code = code.upper()
        base = self.cfg.course_dir or self.cfg.data_dir
        return os.path.join(base, code)

    def category_dir_for(self, cat: str) -> str:
        base = self.cfg.course_dir or self.cfg.data_dir
        return os.path.join(base, cat)

    # --------------------- feature 12: confirm before bulk ---------------------
    def apply_plan(self, plan: Plan, user: str = "user") -> dict[str, Any]:
        """Apply a plan with the deterministic engine. Collision-safe, logged."""
        if not plan.confirmed:
            raise EngineError("Plan must be confirmed before applying.")
        applied: dict[str, Any] = {"moves": [], "trash": [], "created": []}
        for i in plan.items:
            if i.action == "mkdir":
                self.engine.mkdir(i.dest)
                applied["created"].append(i.dest)
                self.db.log_operation(user, "mkdir", i.dest, "", "", "applied", plan.plan_id)
            elif i.action == "move":
                target = self.engine.move(i.src, os.path.dirname(i.dest))
                applied["moves"].append((i.src, target))
                self.db.log_operation(user, "move", i.src, target, i.reason,
                                      "applied", plan.plan_id)
            elif i.action == "trash":
                from .trash import Trash
                Trash(self.cfg, self.db, self.engine).trash_files(
                    [i.src], i.reason or "plan", user)
                applied["trash"].append(i.src)
                self.db.log_operation(user, "trash", i.src, "", i.reason,
                                      "applied", plan.plan_id)
        return applied

    # --------------------- helpers ---------------------
    @staticmethod
    def _has_text(path: str) -> bool:
        return os.path.splitext(path)[1].lower() in (
            ".pdf", ".txt", ".md", ".docx", ".pptx")

    def _peek_text(self, path: str) -> str:
        from .extract import extract_text
        return extract_text(path).get("text", "")
