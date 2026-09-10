"""Phase 7 agent core.

Public surface:

* :class:`AgentRuntime` — the deterministic control loop (parse -> gate ->
  execute -> audit).
* :class:`ToolRegistry` / :class:`Tool` / :class:`Param` — typed capabilities.
* :class:`Intent`, :class:`ToolCall`, :class:`ToolResult`, :class:`AgentReply`,
  :class:`ContextBundle` — the typed vocabulary.
* :class:`Agent` — the pre-existing *advisory* LLM wrapper (moved here from
  ``butler/agent.py`` and re-exported for backward compatibility).
"""

from __future__ import annotations

from .advisory import Agent
from .context import ContextBuilder
from .errors import (AgentError, LLMUnavailable, PermissionDenied,
                     ToolNotFound, ToolValidationError, UnknownIntent)
from .intent import IntentParser
from .models import (AgentReply, ContextBundle, Intent, ToolCall, ToolResult)
from .registry import Param, Tool, ToolRegistry
from .runtime import AgentRuntime, build_runtime
from .session import PendingAction, Session, SessionStore
from .tools import build_default_registry

__all__ = [
    "Agent",
    "AgentError",
    "AgentReply",
    "AgentRuntime",
    "ContextBuilder",
    "ContextBundle",
    "Intent",
    "IntentParser",
    "LLMUnavailable",
    "Param",
    "PendingAction",
    "PermissionDenied",
    "Session",
    "SessionStore",
    "Tool",
    "ToolCall",
    "ToolNotFound",
    "ToolRegistry",
    "ToolResult",
    "ToolValidationError",
    "UnknownIntent",
    "build_default_registry",
    "build_runtime",
]
