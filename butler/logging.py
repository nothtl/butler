"""Operation logging (feature 11).

Every plan, file action (move/rename/mkdir/trash/restore/purge) and search is
logged both to SQLite (`operations` table) and to a human-readable log file at
``<state_dir>/butler.log``. This gives a transparent undo history (pair with
trash.recover) and an audit trail.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .config import Config
from .db import DB

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(cfg: Config, level: int = logging.INFO) -> None:
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("butler")
    if logger.handlers:
        return
    logger.setLevel(level)
    fh = logging.FileHandler(cfg.log_path(), encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FORMAT))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(fh)
    logger.addHandler(sh)


def log_action(db: DB, cfg: Config, user: str, action: str, target: str = "",
               dest: str = "", detail: str = "", status: str = "applied",
               plan_id: str | None = None) -> int:
    """Record an operation in the DB and mirror it to the log file."""
    op_id = db.log_operation(user, action, target, dest, detail, status, plan_id)
    logger = logging.getLogger("butler")
    logger.info("[op:%s] %s %s -> %s (%s) status=%s",
                op_id, action, target or "-", dest or "-", detail or "", status)
    return op_id
