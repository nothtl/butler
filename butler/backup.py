"""Backup monitoring (feature 18).

Reports the health of the last backup, trash size and free disk, and can
trigger a rsync-style mirror of the root into a configured backup directory.

On the Pi this is intentionally lightweight & schedule-agnostic (a crontab or
systemd timer can call `butler backup`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from .config import Config
from .db import DB


class Backup:
    def __init__(self, cfg: Config, db: DB):
        self.cfg = cfg
        self.db = db

    def status(self) -> dict[str, Any]:
        last = self.db.latest_backup()
        free = shutil.disk_usage(self.cfg.data_dir)
        trash_size = self._trash_size()
        return {
            "last_backup": dict(last) if last else None,
            "backup_dir": self.cfg.backup_dir or "(not configured)",
            "schedule": self.cfg.backup_schedule,
            "free_gb": round(free.free / 1024**3, 2),
            "total_gb": round(free.total / 1024**3, 2),
            "trash_gb": round(trash_size / 1024**3, 3),
        }

    def run(self, source: str | None = None) -> dict[str, Any]:
        if not self.cfg.backup_dir:
            return {"ok": False, "detail": "backup_dir not configured; set in config.toml"}
        src = source or self.cfg.data_dir
        os.makedirs(self.cfg.backup_dir, exist_ok=True)
        try:
            rsync = shutil.which("rsync")
            if rsync:
                cmd = [rsync, "-a", "--delete", "--exclude=.butler/trash",
                       src + os.sep, self.cfg.backup_dir + os.sep]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                ok = proc.returncode == 0
                detail = proc.stderr.strip()[-400:] if not ok else "ok"
            else:
                # fallback copy
                shutil.copytree(src, self.cfg.backup_dir, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(".butler/trash"))
                ok = True
                detail = "copytree fallback"
            self.db.log_backup(src, self.cfg.backup_dir, "ok" if ok else "error", detail)
            return {"ok": ok, "detail": detail, "source": src,
                    "dest": self.cfg.backup_dir}
        except Exception as exc:  # noqa: BLE001
            self.db.log_backup(src, self.cfg.backup_dir, "error", str(exc))
            return {"ok": False, "detail": str(exc), "source": src,
                    "dest": self.cfg.backup_dir}

    def _trash_size(self) -> int:
        from .trash import Trash
        from .engine import Engine
        return Trash(self.cfg, self.db, Engine(self.cfg)).total_size()
