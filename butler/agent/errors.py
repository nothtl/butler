"""Phase 7 / M1: agent core error hierarchy.

All errors raised inside the agent runtime are explicit and typed so the
deterministic layer (and the caller) can distinguish "the model proposed
something invalid" from "the tool failed" from "policy refused".
"""

from __future__ import annotations


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
