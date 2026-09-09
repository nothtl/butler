"""Phase 6: health, heartbeat and degraded/offline mode.

The DB is the single source of truth for liveness: every subsystem beats its
heart into the ``heartbeat`` table so ``butler health`` can report last-alive
even after a crash, and the scheduler can reclaim a stale lease left by a dead
process.

``Health`` also owns the degraded / offline switch. Offline mode means: do not
reach the outside world at all. Degraded mode means: external calls are blocked
by :class:`SafetyPolicy` but Butler-owned local work continues.
"""

from __future__ import annotations

import os
import time
from typing import Any

from .db import DB


class Health:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db: DB = container.db
        self._started = int(time.time())

    # ------------------------------------------------------------ heartbeat
    def beat(self, source: str = "db", status: str = "ok", note: str = "",
             ts: int | None = None) -> None:
        try:
            self.db.heartbeat(source, ts=ts, status=status, note=note)
        except Exception:  # noqa: BLE001 — a heartbeat failure must never crash
            pass

    def last_age(self, source: str) -> int | None:
        try:
            return self.db.heartbeat_age(source)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        cfg = self.cfg
        sources = {r["source"]: dict(r) for r in self.db.heartbeats()}
        now = int(time.time())
        per_source = {}
        for name, row in sources.items():
            age = max(0, now - int(row["ts"]))
            per_source[name] = {
                "age_seconds": age,
                "status": "ok" if age <= self._threshold() else "stale",
                "ts": int(row["ts"]),
            }
        return {
            "ok": self.ready(),
            "now": now,
            "uptime_seconds": max(0, now - self._started),
            "degraded_mode": bool(cfg.degraded_mode) if cfg else False,
            "offline_mode": bool(getattr(cfg, "offline_mode", False)) if cfg else False,
            "db_path": self.db.path,
            "sources": per_source,
            "breaker": self._breaker_summary(),
        }

    def _threshold(self) -> int:
        cfg = self.cfg
        return int(getattr(cfg, "heartbeat_max_age", 300)) if cfg else 300

    def _breaker_summary(self) -> dict[str, Any]:
        out = {}
        states = []
        for r in self.db.scheduler_states():
            states.append({"job": r["job"], "status": r["last_status"],
                           "failures": r["consecutive_failures"],
                           "disabled": bool(r["disabled"])})
        out["jobs"] = states
        return out

    # ------------------------------------------------------------ readiness
    def ready(self) -> bool:
        """True when the DB is reachable and not in offline mode."""
        cfg = self.cfg
        if cfg is not None and getattr(cfg, "offline_mode", False):
            return False
        try:
            self.db.one("SELECT 1 AS ok")
            return True
        except Exception:  # noqa: BLE001
            return False

    def liveness(self) -> bool:
        return self.ready()

    def set_mode(self, degraded: bool | None = None, offline: bool | None = None) -> dict[str, Any]:
        cfg = self.cfg
        if cfg is None:
            return {"ok": False}
        if degraded is not None:
            cfg.degraded_mode = bool(degraded)
        if offline is not None:
            cfg.offline_mode = bool(offline)
        self.beat("db", status="degraded" if cfg.degraded_mode else "ok")
        return {"ok": True, "degraded_mode": cfg.degraded_mode,
                "offline_mode": cfg.offline_mode}
