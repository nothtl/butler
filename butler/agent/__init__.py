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
from .errors import (AgentError, AmbiguousRequest, LLMUnavailable,
                     PermissionDenied, SemanticValidationError, ToolNotFound,
                     ToolValidationError, UnknownIntent)
from .intent import IntentParser
from .interpret import (DeterministicInterpreter, LLMInterpreter,
                        SemanticInterpreter, default_interpreter)
from .models import (AgentReply, ContextBundle, Intent, ToolCall, ToolResult)
from .registry import Param, Tool, ToolRegistry
from .runtime import AgentRuntime, build_runtime
from .semantic import (ActionKind, AgentRequest, AgentResult, Constraint,
                       ConstraintSource, ContextSnapshot, EntityRef,
                       EntityType, Hardness, RequestIntent, ResultStatus,
                       Scope, ScopeKind, TemporalRange, TemporalResolution)
from .service import ExecutiveService
from .session import PendingAction, Session, SessionStore
from .temporal import Clock, TemporalResolver
from .tools import build_default_registry

__all__ = [
    "ActionKind",
    "Agent",
    "AgentError",
    "AgentReply",
    "AgentRequest",
    "AgentResult",
    "AgentRuntime",
    "AmbiguousRequest",
    "Clock",
    "Constraint",
    "ConstraintSource",
    "ContextBuilder",
    "ContextBundle",
    "ContextSnapshot",
    "DeterministicInterpreter",
    "EntityRef",
    "EntityType",
    "ExecutiveService",
    "Hardness",
    "Intent",
    "IntentParser",
    "LLMInterpreter",
    "LLMUnavailable",
    "Param",
    "PendingAction",
    "PermissionDenied",
    "RequestIntent",
    "ResultStatus",
    "Scope",
    "ScopeKind",
    "SemanticInterpreter",
    "SemanticValidationError",
    "Session",
    "SessionStore",
    "TemporalRange",
    "TemporalResolution",
    "TemporalResolver",
    "Tool",
    "ToolCall",
    "ToolNotFound",
    "ToolRegistry",
    "ToolResult",
    "ToolValidationError",
    "UnknownIntent",
    "build_default_registry",
    "build_runtime",
    "default_interpreter",
]
