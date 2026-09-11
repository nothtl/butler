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
import re
import sys
import time
import uuid
from typing import Any

# ---------------------------------------------------------------------------
# credential redaction
#
# HTTP client libraries (httpx/httpcore) log the full request URL at INFO,
# and the Telegram Bot API embeds the bot token in the URL path
# (``https://api.telegram.org/bot<id>:<secret>/getUpdates``). Exception
# messages and debug logs can carry the same value. Every log record that
# passes through a Butler handler is scrubbed, and the noisy HTTP loggers are
# quieted, so the token never reaches journald/system logs in cleartext.
# ---------------------------------------------------------------------------

# Telegram bot token: "<bot_id>:<secret>" (optionally prefixed by "bot").
_TG_TOKEN_RE = re.compile(r"(?i)\b(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}\b")
# OpenAI/DeepSeek-style API keys.
_API_KEY_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{12,}\b")
# Generic "token=...", "api_key: ...", "Authorization: Bearer ..." pairs.
_SECRET_KV_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer)"
    r"(\s*[:=]\s*|\s+)([^\s,;\"']+)")

# Loggers that emit raw HTTP request/response data.
_HTTP_LOGGERS = (
    "httpx", "httpcore", "urllib3", "requests", "aiohttp", "telegram",
    "telegram.ext", "telegram.request", "telegram.ext.ExtBot",
)


def redact(text: str) -> str:
    """Return *text* with credentials replaced by placeholders."""
    if not text:
        return text
    text = _TG_TOKEN_RE.sub("bot<redacted>", text)
    text = _API_KEY_RE.sub("<redacted-api-key>", text)
    text = _SECRET_KV_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}<redacted>", text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub credentials from a record before it is formatted.

    ``record.args`` are always scrubbed (this never changes the number or
    type of arguments, so ``%``-formatting stays intact). ``record.msg`` is
    only rewritten when there are no args to interpolate; otherwise a value
    like ``"token=%s"`` could be corrupted into ``"token=<redacted>"`` and
    break formatting. The final text (and cached tracebacks) are scrubbed by
    :class:`RedactingFormatter`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, dict):
            record.args = {k: (redact(v) if isinstance(v, str) else v)
                           for k, v in args.items()}
        elif isinstance(args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) else a
                                for a in args)
        if isinstance(record.msg, str) and not record.args:
            record.msg = redact(record.msg)
        return True


class RedactingFormatter(logging.Formatter):
    """Format a record and scrub credentials from the final text."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return redact(text)



def configure_bot_logging(level: int = logging.INFO) -> None:
    """Configure root logging for the bot with credential redaction.

    Installs a single redacting handler on the root logger and raises the
    level of the HTTP client loggers that would otherwise print the full
    Telegram request URL (including the bot token) at INFO.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(RedactingFormatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    for name in _HTTP_LOGGERS:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING)
        for existing in list(lg.handlers):
            lg.removeHandler(existing)


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
        self.addFilter(RedactingFilter())

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            if not _EMIT_TRACE:
                _default_emit(record)
                return
            rid = _run_id.get()
            actor = _actor.get()
            msg = redact(record.getMessage())
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
