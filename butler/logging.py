"""Phase 6: structured logging.

Every Butler action runs inside a ``Timing``/``run_id`` context so that logs,
audit rows and idempotency keys share one trace. A single RFC3339-ish log line:

    2026-09-09T12:00:00Z  run_id  actor  action  key=value ...

``run_id`` is stable across a retry (a retry reuses the originating run), which
means the audit trail and the operational logs stay coherent — you can replay
exactly what happened.
"""

from __future__ import annotations

import contextvars
import logging
import os
import sys
import time
import uuid
from typing import Any

# A per-call context: run_id and actor. Defaulted but never raises.
_run_id: contextvars.ContextVar[str] = contextvars.ContextVar("butler_run_id",
                                                             default="")
_actor: contextvars.ContextVar[str] = contextvars.ContextVar("butler_actor",
                                                             default="")

_EMIT_TRACE = True


class ButlerHandler(logging.Handler):
    """A lightweight structured formatter that adds run_id/actor/action context."""
    def __init__(self, stream=None):
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))
        self.stream = stream or sys.stderr

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            if not _EMIT_TRACE:
                _default_emit(record)
                return
            rid = _run_id.get()
            actor = _actor.get()
            msg = record.getMessage()
            if rid:
                msg = f"{rid}  {actor or '-'}  {record.name}  {msg}"
            elif actor:
                msg = f"-  {actor}  {record.name}  {msg}"
            self.stream.write(msg + "\n")
            self.stream.flush()
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def _default_emit(record: logging.LogRecord) -> None:  # pragma: no cover
    try:
        logging.basicConfig()
        logging.getLogger().handle(record)
    except Exception:  # noqa: BLE001
        pass


def new_run_id() -> str:
    return uuid.uuid4().hex


def set_run_id(run_id: str) -> None:
    _run_id.set(run_id or "")


def get_run_id() -> str:
    return _run_id.get()


def set_actor(actor: str) -> None:
    _actor.set(actor or "")


def get_actor() -> str:
    return _actor.get()


def reset() -> None:
    _run_id.set("")
    _actor.set("")


def init(level: int = logging.INFO) -> None:
    """Install a single structured handler on the ``butler`` logger."""
    root = logging.getLogger("butler")
    root.setLevel(level)
    # Avoid duplicate handlers on re-init (tests re-init between cases).
    for h in list(root.handlers):
        if isinstance(h, ButlerHandler):
            root.removeHandler(h)
    h = ButlerHandler(sys.stderr)
    h.setLevel(level)
    root.addHandler(h)
    root.propagate = False


def disable_trace() -> None:
    """Turn off per-line run_id emission (used by acceptance harnesses that
    assert on ``butler.cli`` output rather than logs)."""
    global _EMIT_TRACE
    _EMIT_TRACE = False


def enable_trace() -> None:
    global _EMIT_TRACE
    _EMIT_TRACE = True


def now_iso(ts: int | None = None) -> str:
    ts = ts if ts is not None else int(time.time())
    try:
        import datetime
        return datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # pragma: no cover
        return str(ts)
