"""Phase 7 / M1: intent parsing.

Deterministic first, LLM as a *fallback only*. The deterministic parser reuses
the existing ``Decider.parse`` so every slash command and regex behaviour is
preserved. When it cannot classify a message, an optional LLM may propose a
tool; the proposal is still validated against the registry and gated before it
can run, so a hallucinated intent is harmless.
"""

from __future__ import annotations

import logging
from typing import Any

from .errors import LLMUnavailable, UnknownIntent
from .models import Intent

log = logging.getLogger("butler.agent.intent")

# Messages the deterministic parser resolves to "help"/"chat" but which the
# agent should treat as an actual conversational request.
_CHAT_KINDS = {"chat", "ask", "help"}


class IntentParser:
    def __init__(self, decider: Any = None, chat: Any = None,
                 registry: Any = None):
        self.decider = decider
        self.chat = chat
        self.registry = registry

    def parse(self, message: str) -> Intent:
        """Deterministic parse. Raises :class:`UnknownIntent` for empty input."""
        text = (message or "").strip()
        if not text:
            raise UnknownIntent("empty message")
        if self.decider is not None and hasattr(self.decider, "parse"):
            legacy = self.decider.parse(text)
            return Intent.from_legacy(legacy, raw=text)
        return Intent(kind="chat", query=text, raw=text, source="deterministic")

    def needs_llm(self, intent: Intent) -> bool:
        """True when the deterministic result is too weak to act on."""
        return intent.kind in _CHAT_KINDS

    def parse_with_llm(self, message: str, context_text: str = "") -> Intent:
        """Ask the LLM to choose a tool. Requires a chat object + registry."""
        if self.chat is None or self.registry is None:
            raise LLMUnavailable("no LLM configured for intent fallback")
        ready = getattr(self.chat, "_llm_ready", None)
        if callable(ready) and not ready():
            raise LLMUnavailable("LLM not configured")
        from .prompts import build_intent_prompt, parse_tool_choice
        system, user = build_intent_prompt(message, self.registry.schema(),
                                           context_text)
        try:
            raw = self.chat._llm((system, user))
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(str(exc)) from exc
        choice = parse_tool_choice(raw)
        if not choice or not self.registry.has(choice["tool"]):
            raise UnknownIntent("LLM did not choose a registered tool")
        return Intent(
            kind=choice["tool"],
            params=choice["args"],
            raw=message,
            source="llm",
            confidence=0.6,
        )
