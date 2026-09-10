"""Phase 7 / M1: per-user session state.

Session state was previously scattered across ad-hoc ``TelegramBot`` fields
(``pending``, ``_pending_urls``, ``_await_url``, ``_url_seed``). This module
gives the agent a single, explicit place for conversation turns, remembered
values and *pending confirmations* — the last of which is what makes
consequent-external actions safe: a tool that needs consent parks a request
here, and the next turn can confirm it by id.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .models import Intent


@dataclass
class PendingAction:
    """A gated action awaiting explicit human confirmation."""

    id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    created: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tool": self.tool, "args": self.args,
                "reason": self.reason, "created": self.created}


@dataclass
class Session:
    user: str = "user"
    turns: list[dict[str, Any]] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    last_intent: Intent | None = None
    pending: dict[str, PendingAction] = field(default_factory=dict)

    # ---------------------------------------------------------------- turns
    def add_turn(self, role: str, content: str,
                 meta: dict[str, Any] | None = None) -> None:
        self.turns.append({"role": role, "content": content,
                           "meta": meta or {}, "ts": int(time.time())})
        if len(self.turns) > 40:
            del self.turns[:-40]

    def history(self, limit: int = 12) -> list[dict[str, Any]]:
        return self.turns[-limit:]

    # ------------------------------------------------------------ key/value
    def remember(self, key: str, value: Any) -> None:
        self.data[key] = value

    def recall(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    # -------------------------------------------------------------- pending
    def park(self, action: PendingAction) -> None:
        if not action.created:
            action.created = int(time.time())
        self.pending[action.id] = action

    def take(self, action_id: str = "") -> PendingAction | None:
        """Remove and return a pending action (the most recent if no id)."""
        if action_id:
            return self.pending.pop(action_id, None)
        if not self.pending:
            return None
        newest = max(self.pending.values(), key=lambda a: a.created)
        return self.pending.pop(newest.id, None)

    def clear_pending(self) -> None:
        self.pending.clear()

    def reset(self) -> None:
        self.turns.clear()
        self.data.clear()
        self.pending.clear()
        self.last_intent = None


class SessionStore:
    """Thread-safe, in-memory store keyed by user id.

    Durable persistence is intentionally deferred (M3 Memory 2.0); M1 only
    needs correctness within a process, which is where the Telegram bot and
    CLI live.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    def get(self, user: str = "user") -> Session:
        key = str(user or "user")
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = Session(user=key)
                self._sessions[key] = session
            return session

    def reset(self, user: str = "user") -> None:
        with self._lock:
            self._sessions.pop(str(user or "user"), None)

    def users(self) -> list[str]:
        with self._lock:
            return list(self._sessions)
