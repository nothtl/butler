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

from .db import DB, SCHEMA_VERSION

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"
DISABLED = "DISABLED"


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

    # ------------------------------------------------------------ subsystems
    def subsystems(self) -> dict[str, dict[str, Any]]:
        """Per-subsystem status: HEALTHY / DEGRADED / UNAVAILABLE / DISABLED."""
        cfg = self.cfg
        out: dict[str, dict[str, Any]] = {}

        def add(name: str, state: str, detail: str = "") -> None:
            out[name] = {"state": state, "detail": detail}

        import os
        # database + migrations
        db_ok = False
        try:
            self.db.one("SELECT 1 AS ok")
            db_ok = True
        except Exception as exc:  # noqa: BLE001
            add("database", UNAVAILABLE, str(exc))
        if db_ok:
            add("database", HEALTHY, self.db.path)
            ver = self.db.schema_version()
            if ver == SCHEMA_VERSION:
                add("migrations", HEALTHY, f"schema v{ver}")
            else:
                add("migrations", DEGRADED,
                    f"schema v{ver} != expected v{SCHEMA_VERSION}")

        # filesystem / data dir
        data_dir = getattr(cfg, "data_dir", "")
        if data_dir and os.path.isdir(data_dir) and os.access(data_dir, os.W_OK):
            add("filesystem", HEALTHY, data_dir)
        elif data_dir and os.path.isdir(data_dir):
            add("filesystem", DEGRADED, "data dir is not writable")
        else:
            add("filesystem", UNAVAILABLE, data_dir or "no data dir")

        # telegram
        if not getattr(cfg, "telegram_token", ""):
            add("telegram", DISABLED, "no token configured")
        elif getattr(cfg, "telegram_allowed_users", []) or \
                getattr(cfg, "telegram_open_when_empty", False):
            add("telegram", HEALTHY, "token + access policy configured")
        else:
            add("telegram", DEGRADED, "deny-by-default (empty allow-list)")

        # model provider
        if getattr(cfg, "llm_api_key", ""):
            add("model", HEALTHY, getattr(cfg, "llm_model", ""))
        else:
            add("model", DISABLED, "no LLM key; deterministic mode only")

        # google calendar
        if not getattr(cfg, "google_calendar_enabled", False):
            add("google_calendar", DISABLED, "not enabled")
        else:
            creds = getattr(cfg, "google_calendar_credentials", "")
            token = os.path.join(getattr(cfg, "state_dir", ""), "gcal_token.json")
            if creds and os.path.exists(creds) and os.path.exists(token):
                add("google_calendar", HEALTHY, "connected")
            elif creds and os.path.exists(creds):
                add("google_calendar", UNAVAILABLE,
                    "credentials present, not connected")
            else:
                add("google_calendar", UNAVAILABLE, "no credentials")

        # web provider
        provider = getattr(cfg, "web_search_provider", "offline")
        if not getattr(cfg, "web_enabled", True) or provider == "offline":
            add("web", DISABLED, f"provider={provider}")
        else:
            add("web", HEALTHY, f"provider={provider}")

        add("mcp", HEALTHY, "stdio server available")
        add("memory",
            HEALTHY if getattr(cfg, "memory_enabled", True) else DISABLED,
            "long-term memory")
        add("scheduler",
            HEALTHY if getattr(cfg, "scheduler_enabled", True) else DISABLED,
            getattr(cfg, "proactive_schedule", ""))
        add("optimizer",
            HEALTHY if getattr(cfg, "optimizer_enabled", True) else DISABLED,
            getattr(cfg, "optimizer_default_strategy", "balanced"))
        if getattr(cfg, "proactive_enabled", True) and \
                getattr(cfg, "proactive_engine_enabled", True):
            add("proactive", HEALTHY, "candidate engine enabled")
        else:
            add("proactive", DISABLED, "disabled")

        # heartbeat
        age = self.last_age("db")
        if age is None:
            add("heartbeat", DEGRADED, "no heartbeat recorded")
        elif age <= self._threshold():
            add("heartbeat", HEALTHY, f"{age}s ago")
        else:
            add("heartbeat", DEGRADED, f"stale ({age}s)")

        # recovery
        try:
            last = self.db.latest_backup()
            if last is None:
                add("recovery", HEALTHY, "no backups yet")
            elif str(last["status"]) == "ok":
                add("recovery", HEALTHY, "last backup ok")
            else:
                add("recovery", DEGRADED, "last backup failed")
        except Exception as exc:  # noqa: BLE001
            add("recovery", DEGRADED, str(exc))
        return out

    def overall(self) -> str:
        subs = self.subsystems()
        core_bad = any(subs.get(k, {}).get("state") == UNAVAILABLE
                       for k in ("database", "filesystem"))
        if core_bad:
            return UNAVAILABLE
        if any(v["state"] in (DEGRADED, UNAVAILABLE) for v in subs.values()):
            return DEGRADED
        return HEALTHY

    def report(self) -> dict[str, Any]:
        return {"ok": self.ready(), "overall": self.overall(),
                "version": self._version(), "subsystems": self.subsystems(),
                "heartbeat": self.status()}

    @staticmethod
    def _version() -> str:
        try:
            from . import __version__
            return __version__
        except Exception:  # noqa: BLE001
            return "unknown"

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
