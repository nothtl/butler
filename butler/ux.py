"""M8: user-facing UX helpers.

Keep the two concerns separate:

* **Errors** shown to a user must be understandable and must never leak
  internals or secrets; detailed diagnostics stay in the logs.
* **Messages** sent over Telegram must fit the platform limit and be split
  cleanly.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

TELEGRAM_LIMIT = 4096


def new_request_id() -> str:
    """A short correlation id to trace one request through logs/audit."""
    return uuid.uuid4().hex[:12]

#: Secret-shaped strings that must never be echoed back to a user.
_SECRET_RE = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|password|secret)\b\s*[:=]\s*\S+"),
)


def redact_text(text: Any) -> str:
    out = str(text or "")
    for pat in _SECRET_RE:
        out = pat.sub("[REDACTED]", out)
    return out


def friendly_error(exc: BaseException | str) -> str:
    """A short, safe, user-facing explanation of a failure.

    Never includes the raw exception text (that stays in the logs) so secrets
    and internal details cannot leak through an error message.
    """
    raw = str(exc or "").lower()
    if isinstance(exc, sqlite3.OperationalError) or "database" in raw:
        if "locked" in raw or "busy" in raw:
            return ("I couldn't update your data because the local database was "
                    "temporarily busy. Nothing was changed — please try again.")
        if "readonly" in raw or "read-only" in raw:
            return ("The local database is read-only right now. Nothing was "
                    "changed.")
        return ("I couldn't read or update your local data just now. Nothing was "
                "changed — please try again.")
    if isinstance(exc, TimeoutError) or "timed out" in raw or "timeout" in raw:
        return ("That took too long and was stopped safely. Nothing was changed.")
    if "connection refused" in raw or "unreachable" in raw \
            or "name or service not known" in raw:
        return ("An external service is unreachable right now. Nothing was "
                "changed — your local data is unaffected.")
    if "calendar" in raw or "gcal" in raw:
        return ("I couldn't reach your calendar just now. I'm keeping your local "
                "schedule unchanged until it's back.")
    if "permission" in raw or "denied" in raw:
        return "That action isn't permitted, so nothing was changed."
    return ("Something went wrong and nothing was changed. Please try again; "
            "the details are in Butler's logs.")


def chunk_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split a message into platform-safe chunks on paragraph/line boundaries."""
    text = str(text or "")
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for para in text.split("\n"):
        piece = para + "\n"
        if len(current) + len(piece) > limit:
            if current:
                chunks.append(current.rstrip("\n"))
            while len(piece) > limit:
                chunks.append(piece[:limit])
                piece = piece[limit:]
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks or [text[:limit]]
