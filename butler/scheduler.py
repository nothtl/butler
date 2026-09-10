"""Lightweight scheduler (feature 18).

A stdlib daemon thread that runs periodic jobs without external dependencies:
  * reindex  — re-scan roots + prune missing entries (every ``reindex_every_hours``)
  * backup   — run a backup (``backup_schedule`` cadence)
  * digest   — produce a daily summary and push it to Telegram (``digest_chat``)

Last-run timestamps are persisted in the state dir so restarts don't re-run jobs.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

log = logging.getLogger("butler.scheduler")


def _cadence_seconds(spec: str | int) -> int:
    """Parse 'daily'|'weekly'|'6h'|'30m'|'900s' into seconds (0 = disabled).

    An integer is already a number of seconds (e.g. course_monitor_interval).
    """
    if isinstance(spec, int):
        return int(spec)
    spec = (spec or "").strip().lower()
    if not spec:
        return 0
    if spec == "off" or spec == "never":
        return 0
    if spec in ("daily", "day", "1d"):
        return 24 * 3600
    if spec in ("weekly", "week", "1w"):
        return 7 * 24 * 3600
    if spec in ("hourly", "1h"):
        return 3600
    if spec in ("minutely", "1m"):
        return 60
    try:
        if spec.endswith("h"):
            return int(spec[:-1]) * 3600
        if spec.endswith("m"):
            return int(spec[:-1]) * 60
        if spec.endswith("s"):
            return int(spec[:-1])
        if spec.endswith("d"):
            return int(spec[:-1]) * 24 * 3600
        if spec.endswith("w"):
            return int(spec[:-1]) * 7 * 24 * 3600
        return int(spec)
    except ValueError:
        return 0


class Scheduler:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.state_file = os.path.join(self.cfg.state_dir, "scheduler.json")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------- lifecycle ----------------
    def start(self) -> None:
        if not self.cfg.scheduler_enabled:
            log.info("scheduler disabled")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="butler-scheduler")
        self._thread.start()
        log.info("scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # ---------------- main loop ----------------
    def _loop(self) -> None:
        state = self._load_state()
        # migrate/seed the DB-backed scheduler state so restart keeps last-run.
        self._seed_state()
        # run any job that is already overdue once on startup
        while not self._stop.wait(60):
            now = int(time.time())
            self.db = getattr(self.container, "db", None)
            # Reclaim any lease left by a crashed run so the job can resume.
            if self.db is not None:
                try:
                    self.db.scheduler_reclaim_stale(now)
                except Exception:  # noqa: BLE001
                    pass
            jobs = [
                ("reindex", self.cfg.reindex_every_hours * 3600, self._run_reindex),
                ("backup", _cadence_seconds(self.cfg.backup_schedule), self._run_backup),
                ("digest", _cadence_seconds(self.cfg.digest_schedule), self._run_digest),
                ("courses", _cadence_seconds(self.cfg.course_monitor_interval),
                 self._run_courses),
                ("proactive", _cadence_seconds(self.cfg.proactive_schedule),
                 self._run_proactive),
                ("gcal_sync", _cadence_seconds(self.cfg.calendar_sync_schedule),
                 self._run_gcal_sync),
                ("briefing", _cadence_seconds(self.cfg.briefing_schedule),
                 self._run_briefing),
                ("review", _cadence_seconds(self.cfg.review_schedule),
                 self._run_review),
            ]
            for name, every, fn in jobs:
                if every <= 0:
                    continue
                last = self._last_run(name, state, int(now))
                if last + every > now:
                    continue
                if not self._acquire(name):
                    continue
                try:
                    # circuit-breaker guard: pause a job that keeps failing.
                    if getattr(self.container, "retry", None) and \
                            getattr(self.container, "retry", None).breaker_open(name):
                        self._skip(name, "circuit_open")
                        continue
                    fn()
                    state[name] = int(time.time())
                    self._mark_done(name, ok=True)
                except Exception as exc:  # noqa: BLE001
                    log.warning("job %s failed: %s", name, exc)
                    self._mark_done(name, ok=False, error=str(exc))
            self._save_state(state)

    # ---------------- DB-backed job state (Phase 6 robustness) ----------------
    def _seed_state(self) -> None:
        if getattr(self.container, "db", None) is None:
            return
        try:
            self.container.db.upsert_scheduler_state("reindex", self.cfg.reindex_every_hours * 3600)
            self.container.db.upsert_scheduler_state("backup", _cadence_seconds(self.cfg.backup_schedule))
            self.container.db.upsert_scheduler_state("digest", _cadence_seconds(self.cfg.digest_schedule))
            self.container.db.upsert_scheduler_state("courses", _cadence_seconds(self.cfg.course_monitor_interval))
            self.container.db.upsert_scheduler_state("proactive", _cadence_seconds(self.cfg.proactive_schedule))
            self.container.db.upsert_scheduler_state("gcal_sync", _cadence_seconds(self.cfg.calendar_sync_schedule))
            self.container.db.upsert_scheduler_state("briefing", _cadence_seconds(self.cfg.briefing_schedule))
            self.container.db.upsert_scheduler_state("review", _cadence_seconds(self.cfg.review_schedule))
        except Exception:  # noqa: BLE001
            pass

    def _last_run(self, name: str, state: dict, now: int) -> int:
        """Last run time from the DB scheduler_state (falls back to the legacy
        state file so an existing install does not re-run everything)."""
        if getattr(self.container, "db", None) is not None:
            try:
                row = self.container.db.scheduler_state(name)
                if row is not None:
                    return int(row["last_done"] or 0) or int(row["last_ts"] or 0) \
                        or int(state.get(name, 0) or 0)
            except Exception:  # noqa: BLE001
                pass
        return int(state.get(name, 0) or 0)

    def _acquire(self, name: str) -> bool:
        """Acquire the per-job lease so a long/overlapping run is skipped."""
        db = getattr(self.container, "db", None)
        if db is None:
            return True
        try:
            return db.scheduler_mark_start(name)
        except Exception:  # noqa: BLE001
            return True

    def _mark_done(self, name: str, ok: bool, error: str = "") -> None:
        db = getattr(self.container, "db", None)
        if db is not None:
            try:
                db.scheduler_mark_done(name, ok, error)
            except Exception:  # noqa: BLE001
                pass

    def _skip(self, name: str, reason: str) -> None:
        db = getattr(self.container, "db", None)
        if db is not None:
            try:
                db.scheduler_note_skipped(name, reason)
            except Exception:  # noqa: BLE001
                pass

    # ---------------- jobs ----------------
    def _run_reindex(self) -> None:
        idx = self.container.indexer
        stats: dict[str, Any] = {}
        for root in self.cfg.roots + self.cfg.index_roots:
            stats[root] = idx.index_root(root)
        stats["pruned"] = idx.prune_missing()
        log.info("scheduled reindex: %s", stats)

    def _run_backup(self) -> None:
        res = self.container.backup.run()
        log.info("scheduled backup: %s", res.get("dest", res))

    def _run_courses(self) -> None:
        courses = getattr(self.container, "courses", None)
        if courses is None:
            return
        updates = courses.check_all()
        if updates:
            log.info("course monitor: %d update(s)", len(updates))
            if self.cfg.digest_chat and self.cfg.telegram_token:
                self._push_digest("📚 New course activity:\n" + "\n".join(
                    f"• {u.get('course_code','')} {u.get('title','')}"
                    for u in updates))

    def _run_proactive(self) -> None:
        proactive = getattr(self.container, "proactive", None)
        if proactive is not None:
            res = proactive.run()
            log.info("proactive check: %s", res)
        # M7: deterministic candidate engine on the same cadence (bounded,
        # idempotent; never a second background loop).
        engine = getattr(self.container, "proactive_engine", None)
        if engine is not None:
            try:
                out = engine.run_cycle(deliver=True)
                log.info("proactive engine: generated=%s notified=%s",
                         out.get("generated"), out.get("notified"))
            except Exception as exc:  # noqa: BLE001 — never break the cadence
                log.warning("proactive engine failed: %s", exc)

    def _run_gcal_sync(self) -> None:
        """Push the local plan to Google Calendar (Phase 5.0 write projection).

        Built on the planner so every failure is swallowed there; the job merely
        triggers it on the configured cadence.
        """
        planner = getattr(self.container, "planner", None)
        if planner is None or not hasattr(planner, "sync_calendar"):
            return
        res = planner.sync_calendar()
        log.info("scheduled gcal sync: %s",
                 {k: v for k, v in res.items() if k != "ok"})

    def _run_briefing(self) -> None:
        exec_ = getattr(self.container, "executive", None)
        if exec_ is None or not hasattr(exec_, "briefing"):
            return
        res = exec_.briefing()
        log.info("briefing (%d sections)", len(res.get("text", [])))

    def _run_review(self) -> None:
        exec_ = getattr(self.container, "executive", None)
        if exec_ is None or not hasattr(exec_, "review"):
            return
        res = exec_.review()
        log.info("review: %s", res.get("actual", {}))

    def _run_digest(self) -> None:
        text = self.build_digest(self.container)
        dest_path = os.path.join(self.cfg.state_dir, "digest.txt")
        with open(dest_path, "w") as fh:
            fh.write(text + "\n")
        log.info("scheduled digest written to %s", dest_path)
        if self.cfg.digest_chat and self.cfg.telegram_token:
            self._push_digest(text)

    def _push_digest(self, text: str) -> None:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json={"chat_id": self.cfg.digest_chat, "text": text},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("digest push failed: %s", exc)

    # ---------------- state ----------------
    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self.state_file) as fh:
                return json.load(fh)
        except Exception:
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        try:
            with open(self.state_file, "w") as fh:
                json.dump(state, fh)
        except Exception as exc:  # noqa: BLE001
            log.debug("state save failed: %s", exc)

    # ---------------- helpers ----------------
    @staticmethod
    def build_digest(container: Any, since_hours: int = 24) -> str:
        from .telebot import human_size
        cfg = container.cfg
        db = container.db
        since = int(time.time()) - since_hours * 3600
        ops = db.query(
            "SELECT action, detail, status FROM operations WHERE ts>=? ORDER BY id DESC LIMIT 40",
            (since,),
        )
        files = db.one("SELECT COUNT(*) AS n FROM files WHERE is_dir=0")
        chunks = db.one("SELECT COUNT(*) AS n FROM chunks")
        trash = db.one("SELECT COUNT(*) AS n FROM trash WHERE restored_at IS NULL")
        lines = ["📊 Butler daily digest", ""]
        lines.append(f"Indexed files : {files['n'] if files else 0}")
        lines.append(f"Chunks        : {chunks['n'] if chunks else 0}")
        lines.append(f"In trash      : {trash['n'] if trash else 0}")
        if ops:
            moves = sum(1 for o in ops if o["action"] == "move")
            trashed = sum(1 for o in ops if o["action"] == "trash")
            restored = sum(1 for o in ops if o["action"] == "restore")
            lines.append(f"Recent ops    : {moves} move(s), {trashed} trashed, {restored} restored")
        lines.append("")
        lines.append("Tip: /ask <question> or /teach <topic> to chat with your files.")
        return "\n".join(lines)
