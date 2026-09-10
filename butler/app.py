"""M8: deterministic application startup, shutdown and lifecycle.

One place that turns the :class:`~butler.core.Container` into a running product:

    load config -> validate -> init DB/migrations -> init subsystems
    -> recover stale state -> validate access -> start periodic jobs
    -> expose ready/health -> (run) -> graceful shutdown

Startup never crashes the whole process for a *safe-to-degrade* optional
integration; it reports which subsystem failed instead. Shutdown is explicit so
SIGTERM/SIGINT leaves durable state recoverable.
"""

from __future__ import annotations

import logging
import signal
from typing import Any

from .core import Container
from .ux import friendly_error

log = logging.getLogger("butler.app")


class ButlerApp:
    def __init__(self, container: Container | None = None):
        self.container = container or Container()
        self.scheduler: Any = None
        self.monitor: Any = None
        self.bot: Any = None
        self._stopped = False
        self._signal_installed = False

    # ------------------------------------------------------------- startup
    def startup(self) -> dict[str, Any]:
        """Idempotent, safe startup. Returns a report; never raises for a
        degradable subsystem."""
        c = self.container
        report: dict[str, Any] = {"ok": True, "steps": [], "errors": []}

        def step(name: str, fn) -> Any:
            try:
                out = fn()
                report["steps"].append({"step": name, "ok": True})
                return out
            except Exception as exc:  # noqa: BLE001 — record, don't crash
                log.warning("startup step %s failed: %s", name, exc)
                report["steps"].append({"step": name, "ok": False,
                                        "error": friendly_error(exc)})
                report["errors"].append({"step": name, "error": str(exc)})
                report["ok"] = False
                return None

        # 1. config + DB already validated by Container(); confirm readiness.
        step("database", lambda: c.db.one("SELECT 1 AS ok"))
        step("migrations", lambda: c.db.schema_version())
        step("heartbeat", lambda: c.health.beat("app", note="startup"))

        # 2. recover stale scheduler leases left by a crashed process.
        import time as _time
        now = int(_time.time())
        if hasattr(c.cfg, "now_local"):
            try:
                now = int(c.cfg.now_local().timestamp())
            except Exception:  # noqa: BLE001
                now = int(_time.time())
        report["recovered_leases"] = step(
            "recover_leases", lambda: c.db.scheduler_reclaim_stale(now)) or 0

        # 3. recover stale proactive candidates (ignored, not accepted).
        eng = getattr(c, "proactive_engine", None)
        if eng is not None:
            report["recovered_candidates"] = step(
                "recover_proactive", lambda: eng.expire_stale(now=now)) or 0

        # 4. validate access posture (warn, never fatal).
        cfg = c.cfg
        if getattr(cfg, "telegram_token", "") and \
                not getattr(cfg, "telegram_allowed_users", []) and \
                not getattr(cfg, "telegram_open_when_empty", False):
            report["warning"] = ("Telegram is deny-by-default (empty "
                                 "allow-list); no one can use the bot yet.")
            log.warning("%s", report["warning"])

        # 5. expose ready/health.
        report["health"] = c.health.report()
        report["version"] = report["health"].get("version")
        return report

    # -------------------------------------------------------------- running
    def install_signal_handlers(self) -> None:
        if self._signal_installed:
            return
        try:
            signal.signal(signal.SIGTERM, self._on_signal)
            signal.signal(signal.SIGINT, self._on_signal)
            self._signal_installed = True
        except (ValueError, OSError):  # pragma: no cover — non-main thread
            pass

    def _on_signal(self, signum: int, _frame: Any) -> None:  # pragma: no cover
        log.info("received signal %s; shutting down", signum)
        self.shutdown()
        raise SystemExit(0)

    def start_jobs(self) -> None:
        from .scheduler import Scheduler
        if getattr(self.container.cfg, "scheduler_enabled", True):
            self.scheduler = Scheduler(self.container)
            self.scheduler.start()

    def run_bot(self) -> int:
        self.install_signal_handlers()
        self.startup()
        self.start_jobs()
        from .telebot import TelegramBot
        self.bot = TelegramBot(self.container)
        try:
            self.bot.run_forever()
        finally:
            self.shutdown()
        return 0

    def run_daemon(self) -> int:
        self.install_signal_handlers()
        self.startup()
        from .monitor import CourseMonitor
        self.monitor = CourseMonitor(self.container)
        self.monitor.start()
        self.start_jobs()
        log.info("butler daemon running (scheduler + monitor)")
        try:
            import time
            while not self._stopped:
                time.sleep(1)
        except KeyboardInterrupt:  # pragma: no cover
            pass
        finally:
            self.shutdown()
        return 0

    # ------------------------------------------------------------- shutdown
    def shutdown(self) -> dict[str, Any]:
        if self._stopped:
            return {"ok": True, "already": True}
        self._stopped = True
        out: dict[str, Any] = {"ok": True, "stopped": []}

        def stop(name: str, obj: Any) -> None:
            if obj is None:
                return
            try:
                obj.stop()
                out["stopped"].append(name)
            except Exception as exc:  # noqa: BLE001
                log.warning("shutdown %s failed: %s", name, exc)
                out["ok"] = False

        stop("scheduler", self.scheduler)
        stop("monitor", self.monitor)
        try:
            self.container.health.beat("app", status="stopped", note="shutdown")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.container.db.close()
            out["stopped"].append("db")
        except Exception as exc:  # noqa: BLE001
            out["ok"] = False
            log.warning("shutdown db failed: %s", exc)
        return out
