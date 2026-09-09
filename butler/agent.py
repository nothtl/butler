"""DeepSeek / LLM advisory layer for the Phase 2 planner.

This is the ONLY place an LLM is consulted for task reasoning, and it is
strictly *advisory*: it may interpret a free-text "something came up" and
propose a priority / time estimate, but it NEVER decides where a block sits
(windows, gaps, ordering) — that is always the pure constraint solver in
``schedule.py``. The DB remains the source of truth.

Every helper degrades gracefully: if no LLM key is configured (``[ai] api_key``)
or the call fails, deterministic defaults are returned so the scheduler never
blocks on a network call.
"""

from __future__ import annotations

import re
from typing import Any


class Agent:
    """Advisory LLM wrapper. Never schedules; only interprets/prioritises.

    The planner asks the agent for a *proposal* (priority + estimate + clean
    title) and a human sentence; it then still runs the deterministic solver
    with the resolved inputs.
    """

    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg

    # ------------------------------------------------------------------ access
    def ready(self) -> bool:
        chat = getattr(self.container, "chat", None)
        return bool(getattr(chat, "_llm_ready", lambda: False)())

    def _llm(self, system: str, user: str) -> str | None:
        chat = getattr(self.container, "chat", None)
        if chat is None or not self.ready():
            return None
        try:
            return chat._llm((system, user))
        except Exception:
            return None

    # ------------------------------------------------------ interpretation
    def interpret_urgent(self, text: str,
                         default_priority: int = 4,
                         default_est: int = 60) -> dict[str, Any]:
        """Parse "something came up <desc>" into a task proposal.

        Returns ``{"title", "priority", "est_minutes"}``. The caller can supply
        its own defaults (priority, est) which are honoured when the LLM is
        absent or unhelpful.
        """
        clean = self._clean(text)
        if self.ready():
            raw = self._llm(
                "You extract one urgent task from a user message. Reply with a "
                "single JSON object with keys: title (short), priority (1-5, 5 "
                "most urgent), est_minutes (integer). No prose.",
                text,
            )
            parsed = self._parse_json(raw)
            if parsed:
                return {
                    "title": str(parsed.get("title") or clean),
                    "priority": int(parsed.get("priority") or default_priority),
                    "est_minutes": int(parsed.get("est_minutes") or default_est),
                }
        return {"title": clean, "priority": default_priority,
                "est_minutes": default_est}

    # ------------------------------------------------------ explanation
    def why_sentence(self, deterministic: str, cause: str | None,
                     moved: list[dict[str, Any]]) -> str:
        """Expand a deterministic reason into a short human sentence."""
        if not self.ready():
            return deterministic
        slots = [f"{m['title']}" for m in moved if m.get("new")]
        raw = self._llm(
            "You explain a task-scheduler change to the user in ONE short "
            "sentence. Never mention code, algorithms or internal IDs. "
            f"Cause hard commitment: {cause if cause else 'none'}. "
            f"Moved blocks: {slots}.",
            "Why did my schedule change?",
        )
        return (raw or deterministic).strip()

    # ------------------------------------------------------------------ util
    @staticmethod
    def _clean(text: str) -> str:
        t = re.sub(r"(?i)\b(something\s+came\s+up|urgent)\b", " ", text)
        t = re.sub(r"--\S+", " ", t)   # strip --flags
        t = re.sub(r"\s+", " ", t).strip()
        return t or "Untitled"

    @staticmethod
    def _parse_json(raw: str | None) -> dict | None:
        if not raw:
            return None
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            import json
            return json.loads(m.group(0))
        except Exception:
            return None
