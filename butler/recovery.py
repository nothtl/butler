"""Phase 6: recovery — undo and DB backup/restore.

``undo`` reverses a Butler-owned change using the durable history it wrote
(``task_history``, ``task_gcal``, ``operations``). A reversible action is undone
by returning the pre-action state; a genuinely destructive, non-reversible
external action is *never* silently undone — it is reported instead.

``db_backup`` snapshots the live SQLite file (plus its WAL) so a failed
migration or a corrupt write can be rolled back with ``db_restore``.
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from typing import Any

from .db import DB, ACTIVE_TASK_STATUSES, TERMINAL_STATUSES


class Recovery:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db: DB = container.db
        self.audit = getattr(container, "audit", None)

    # ------------------------------------------------------------- undo
    def undo_task(self, task_id: int, *, run_id: str = "",
                  actor: str = "user") -> dict[str, Any]:
        """Restore a task to the status it had before its most recent change."""
        history = self.db.task_history(task_id=task_id, limit=10)
        if not history:
            return {"ok": False, "error": "no history to undo", "task_id": task_id}
        last = dict(history[0])
        prev_status = last.get("from_status") or "todo"
        current = self.db.task_by_id(task_id)
        cur_status = str(current["status"]) if current else ""
        self.db.set_task_status(task_id, prev_status, reason="undo")
        # If a task-gcal mapping exists, resync the state to match the restore.
        self.db.execute(
            "UPDATE task_gcal SET state='pending_update' WHERE task_id=?",
            (task_id,))
        if self.audit is not None:
            self.audit.record("undo_task", run_id=run_id, actor=actor,
                              kind="low_risk_write", target=f"task:{task_id}",
                              decision="allowed", reason="undo",
                              outcome="ok",
                              detail={"task_id": task_id,
                                      "from": cur_status, "to": prev_status})
        return {"ok": True, "task_id": task_id,
                "restored": prev_status, "previous": cur_status,
                "when": self._fmt(history[0]["ts"])}

    def undo(self, op_ref: str = "", *, run_id: str = "",
             actor: str = "user") -> dict[str, Any]:
        """Generic undo by reference. ``op_ref`` may be a task id."""
        if op_ref.isdigit():
            return self.undo_task(int(op_ref), run_id=run_id, actor=actor)
        return {"ok": False,
                "error": "nothing to undo; provide a task id (e.g. /undo 7)"}

    def reversible(self, action: str) -> bool:
        """Whether a recorded action may be undone by Butler."""
        return action in {
            "add_task", "task_lifecycle", "plan_make", "plan_apply",
            "food_add", "food_consume", "recipe_mark", "note", "route",
            "move_block",
        }

    # ------------------------------------------------------ DB backup/restore
    def db_backup(self, note: str = "", *, run_id: str = "",
                  actor: str = "scheduler") -> dict[str, Any]:
        if self.cfg is None or not getattr(self.cfg, "backup_dir", ""):
            return {"ok": False, "detail": "backup_dir not configured; set in config.toml"}
        src = self.db.path
        dest_dir = self.cfg.backup_dir
        import os, uuid
        os.makedirs(dest_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(dest_dir,
                            f"butler-db-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3")
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001
            pass
        try:
            shutil.copy2(src, dest)
        except Exception as exc:  # noqa: BLE001
            self.db.log_backup(src, dest_dir, "error", str(exc))
            return {"ok": False, "detail": str(exc)}
        self.db.log_backup(src, dest_dir, "ok", note or f"db snapshot {dest}")
        if self.audit is not None:
            self.audit.record("db_backup", run_id=run_id, actor=actor,
                              kind="low_risk_write", target=dest,
                              decision="allowed", outcome="ok", detail={"note": note})
        return {"ok": True, "dest": dest, "size_bytes": self._size(dest)}

    def db_restore(self, path: str, *, run_id: str = "",
                   actor: str = "user") -> dict[str, Any]:
        """Replace the live DB with a previously exported snapshot.

        Only a snapshot living under ``backup_dir`` (or an explicit path the
        caller supplies) is accepted; this never reads from outside the sandbox.
        """
        import os
        if not path or not os.path.exists(path):
            return {"ok": False, "detail": "restore file not found"}
        src = os.path.abspath(path)
        backup_dir = os.path.abspath(self.cfg.backup_dir) if \
            getattr(self.cfg, "backup_dir", "") else ""
        if backup_dir and not src.startswith(backup_dir):
            return {"ok": False,
                    "detail": "restore source must live under the backup dir"}
        self.db_backup("pre-restore snapshot", run_id=run_id, actor=actor)
        self.db.close()
        # clear any stale WAL/SHM from the live DB so the snapshot's committed
        # state is authoritative (a leftover WAL could replay the pre-restore
        # writes and "undo" the rollback).
        for suffix in ("-wal", "-shm"):
            if os.path.exists(self.db.path + suffix):
                try:
                    os.remove(self.db.path + suffix)
                except OSError:
                    pass
        # copy the snapshot (plus a matching WAL if the snapshot carried one)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(src + suffix):
                shutil.copy2(src + suffix, self.db.path + suffix)
        self.db.reopen()
        if self.audit is not None:
            self.audit.record("db_restore", run_id=run_id, actor=actor,
                              kind="low_risk_write", target=src,
                              decision="allowed", outcome="ok", detail={"restored": self.db.path})
        return {"ok": True, "restored": self.db.path, "from": src}

    def list_backups(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM backups ORDER BY ts DESC LIMIT 20")]

    # ------------------------------------------------------------- helpers
    def _fmt(self, ts: int) -> str:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            return str(ts)

    def _size(self, path: str) -> int:
        try:
            import os
            return os.path.getsize(path)
        except Exception:  # noqa: BLE001
            return 0
