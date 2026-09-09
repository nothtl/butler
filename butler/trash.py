"""Trash & recovery (feature 7).

Files are moved to the Butler trash directory, never permanently deleted.
``recover`` restores them to their original location; ``empty`` purges items
older than the retention policy. Everything is recorded in the `trash` table.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import Config
from .db import DB
from .engine import Engine, EngineError


class Trash:
    def __init__(self, cfg: Config, db: DB, engine: Engine):
        self.cfg = cfg
        self.db = db
        self.engine = engine
        self.dir = cfg._effective_trash()

    # background: the trash dir is inside state dir; engine.allowed includes it
    def _rel(self, inner: str) -> str:
        return os.path.relpath(inner, self.dir)

    def trash_files(self, paths: list[str], reason: str = "",
                    user: str = "user") -> list[str]:
        """Move files into trash. Returns the trashed absolute paths."""
        self.cfg.ensure_dirs()
        batch = time.strftime("%Y%m%d-%H%M%S")
        moved: list[str] = []
        for p in paths:
            p = os.path.realpath(p)
            if not os.path.exists(p) or os.path.isdir(p):
                # refuse directories for now (would need recursive)
                continue
            if p.startswith(self.dir):
                continue
            size = os.path.getsize(p)
            dest_dir = os.path.join(self.dir, batch)
            os.makedirs(dest_dir, exist_ok=True)
            name = os.path.basename(p)
            # disambiguate inside trash
            dest, _ = self.engine._unique_name(dest_dir, name)
            shutil.move(p, dest)
            rel = self._rel(dest)
            self.db.add_trash(p, name, rel, size, reason)
            self.db.delete_chunks_for_path(p)
            moved.append(dest)
            self.db.log_operation(user, "trash", p, dest, reason)
        return moved

    def list(self, include_restored: bool = False) -> list[dict[str, Any]]:
        items = self.db.trash_items(include_restored)
        out = []
        for r in items:
            abs_path = os.path.join(self.dir, r["trashed_rel"])
            out.append({
                "id": r["id"],
                "name": r["name"],
                "orig_path": r["orig_path"],
                "abs_path": abs_path,
                "size": r["size"],
                "reason": r["reason"],
                "trashed_at": r["trashed_at"],
                "restored": r["restored_at"] is not None,
                "exists": os.path.exists(abs_path),
            })
        return out

    def recover(self, tid: int, user: str = "user") -> str:
        row = self.db.one("SELECT * FROM trash WHERE id=? AND restored_at IS NULL", (tid,))
        if not row:
            raise EngineError(f"No trashed entry with id {tid}.")
        abs_path = os.path.join(self.dir, row["trashed_rel"])
        if not os.path.exists(abs_path):
            raise EngineError("Trashed file is missing.")
        orig = row["orig_path"]
        # restore to original location, collision-safe
        parent = os.path.dirname(orig)
        os.makedirs(parent, exist_ok=True)
        dest, _ = self.engine._unique_name(parent, row["name"])
        shutil.move(abs_path, dest)
        self.db.restore_trash(tid)
        self.db.log_operation(user, "restore", orig, dest, row["reason"])
        return dest

    def empty(self, older_than_days: int | None = None, user: str = "user") -> int:
        """Permanently purge trashed items (older than the retention policy)."""
        ret = older_than_days if older_than_days is not None else self.cfg.trash_retention_days
        cutoff = time.time() - ret * 86400
        items = self.db.query(
            "SELECT * FROM trash WHERE restored_at IS NULL AND trashed_at < ?",
            (cutoff,),
        )
        removed = 0
        for r in items:
            abs_path = os.path.join(self.dir, r["trashed_rel"])
            if os.path.exists(abs_path):
                try:
                    os.remove(abs_path)
                    removed += 1
                except OSError:
                    continue
            self.db.execute("DELETE FROM trash WHERE id=?", (r["id"],))
        self.db.log_operation(user, "empty_trash", self.dir, "", str(removed))
        return removed

    def purge(self, tids: list[int], user: str = "user") -> int:
        """Permanently purge specific trash items."""
        removed = 0
        for tid in tids:
            r = self.db.one("SELECT * FROM trash WHERE id=?", (tid,))
            if not r or r["restored_at"] is not None:
                continue
            abs_path = os.path.join(self.dir, r["trashed_rel"])
            if os.path.exists(abs_path):
                try:
                    os.remove(abs_path)
                    removed += 1
                except OSError:
                    continue
            self.db.execute("DELETE FROM trash WHERE id=?", (tid,))
        self.db.log_operation(user, "purge_trash", "", "", str(tids))
        return removed

    def total_size(self) -> int:
        total = 0
        for r in self.db.trash_items():
            total += int(r["size"] or 0)
        return total
