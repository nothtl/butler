"""Phase 7 / M1: agent core error hierarchy.

All errors raised inside the agent runtime are explicit and typed so the
deterministic layer (and the caller) can distinguish "the model proposed
something invalid" from "the tool failed" from "policy refused".
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class for every agent-core failure."""


class ToolNotFound(AgentError):
    """The requested tool name is not registered."""


class ToolValidationError(AgentError):
    """Tool arguments failed deterministic validation."""


class PermissionDenied(AgentError):
    """The safety policy refused the action (or it needs confirmation)."""


class UnknownIntent(AgentError):
    """No intent could be derived from the message."""


class LLMUnavailable(AgentError):
    """The LLM fallback was requested but is not configured/reachable."""


class SemanticValidationError(AgentError):
    """A structured semantic request failed deterministic validation.

    Raised when a request (typically produced by an LLM) contains an unknown
    enum, an unsafe/incoherent constraint (e.g. an inferred preference marked
    hard), an out-of-range confidence, or is otherwise malformed. The caller
    must reject the request rather than guess — this is the safety boundary that
    keeps the model from smuggling invalid intent past the deterministic layer.
    """


class AmbiguousRequest(AgentError):
    """A request references an entity/time that cannot be resolved uniquely."""

    def __init__(self, message: str, *, ambiguity: Any = None) -> None:
        super().__init__(message)
        self.ambiguity = ambiguity
