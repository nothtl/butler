"""Phase 6: universal idempotency.

A deterministic client (Telegram button, scheduler job, CLI path, retry loop) is
replayed accidentally when a network call times out, a process restarts mid-plan,
or a job fires twice. Idempotency turns that replay into a no-op that returns the
*original* result rather than running the side effect twice.

The key equals the canonical identity of the operation (e.g. the scheduler job
name + the day it targets, or a Telegram callback's ``message id + action``). A
retry of the *same* key reuses the stored outcome; a genuinely different
operation gets a different key and runs normally.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .db import DB


def make_key(action: str, *parts: Any) -> str:
    """Deterministic idempotency key: ``action`` + a stable hash of ``parts``."""
    payload = json.dumps([_norm(p) for p in parts], ensure_ascii=False,
                         sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
    return f"{action}:{digest}"


def _norm(x: Any) -> Any:
    if isinstance(x, (dict, list, tuple)):
        return json.loads(json.dumps(x, ensure_ascii=False, sort_keys=True,
                                     default=str))
    return str(x)


class Idempotency:
    """Registry of finished operations. Only successful/exhausted outcomes are
    stored; a lock is held for the duration of an in-flight operation so a
    concurrent duplicate is rejected rather than executed twice."""

    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------------
    def start(self, key: str, *, actor: str = "", action: str = "",
              ttl: int = 0) -> bool:
        """Begin a keyed operation. Returns True if the caller is the first
        owner, False if the key is already in flight or finished.
        ``ttl`` (seconds) expires the stored outcome so a truly-new operation
        with the same key may later run again."""
        row = self.db.idem_get(key)
        if row is not None:
            if int(row["expires"] or 0) and int(row["expires"]) < time.time():
                self.db.idem_delete(key)
                row = None
            else:
                return False
        self.db.idem_put(key, status="in_progress", actor=actor,
                         action=action, expires=int(time.time() + ttl) if ttl else 0)
        return True

    def finish(self, key: str, result: Any) -> None:
        """Record a successful outcome so a replay returns ``result``."""
        try:
            payload = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            payload = str(result)
        row = self.db.idem_get(key)
        ttl = int(row["expires"] or 0) if row else 0
        self.db.idem_put(key, status="ok", result=payload,
                         expires=ttl)

    def fail(self, key: str) -> None:
        """Mark a key failed so a replay is allowed to try again."""
        self.db.idem_put(key, status="failed", result="")

    # ------------------------------------------------------------------
    def replay(self, key: str) -> Any | None:
        """Return the stored result if this key finished successfully, else None."""
        row = self.db.idem_get(key)
        if row is None or row["status"] != "ok":
            return None
        try:
            return json.loads(row["result"])
        except Exception:  # noqa: BLE001
            return row["result"]

    # ------------------------------------------------------------------
    def once(self, key: str, fn, *, ttl: int = 0,
             actor: str = "", action: str = "") -> tuple[Any, bool]:
        """Run ``fn`` exactly once per key.

        Returns ``(result, replayed)``. If the key already finished, returns the
        stored result with ``replayed=True`` and never calls ``fn``. If already
        in flight, returns ``(None, True)``.
        """
        stored = self.replay(key)
        if stored is not None:
            return stored, True
        if not self.start(key, actor=actor, action=action, ttl=ttl):
            return None, True
        try:
            result = fn()
            self.finish(key, result)
            return result, False
        except Exception:
            self.fail(key)
            raise

    def count(self) -> int:
        return self.db.idem_count()

    def prune_expired(self) -> int:
        return self.db.idem_prune_expired()
