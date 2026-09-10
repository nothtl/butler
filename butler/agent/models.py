"""Phase 7 / M1: typed data model for the agent core.

These dataclasses are the *only* structured vocabulary the runtime exchanges
between the deterministic parser, the tool registry, the safety gate and the
front-ends. They are deliberately stdlib-only (no Pydantic) so M1 adds no
dependency; the schema can be migrated later if a heavier validation library is
justified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Intent:
    """A structured interpretation of a user message.

    ``kind`` is the canonical action name (it matches a registered tool where
    possible). ``source`` records who produced it: ``deterministic`` (the
    parser/decider) or ``llm`` (the fallback). ``confirmed`` is the single,
    explicit consent bit the safety gate reads — the legacy decider read a
    field that never existed, so this model fixes that.
    """

    kind: str
    target: str = ""
    query: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    scope: str = ""
    confidence: float = 1.0
    source: str = "deterministic"
    confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        return cls(
            kind=str(d.get("kind", "")),
            target=str(d.get("target", "")),
            query=str(d.get("query", "")),
            params=dict(d.get("params") or {}),
            raw=str(d.get("raw", "")),
            scope=str(d.get("scope", "")),
            confidence=float(d.get("confidence", 1.0)),
            source=str(d.get("source", "deterministic")),
            confirmed=bool(d.get("confirmed", False)),
        )

    @classmethod
    def from_legacy(cls, legacy: Any, *, raw: str = "") -> "Intent":
        """Adapt a ``decider.Intent`` (or any object with the same shape)."""
        return cls(
            kind=str(getattr(legacy, "kind", "") or ""),
            target=str(getattr(legacy, "target", "") or ""),
            query=str(getattr(legacy, "query", "") or ""),
            params=dict(getattr(legacy, "params", {}) or {}),
            raw=str(getattr(legacy, "raw", raw) or raw),
            scope=str(getattr(legacy, "scope", "") or ""),
            confidence=1.0,
            source="deterministic",
            confirmed=bool(getattr(legacy, "confirmed", False)),
        )


@dataclass
class ToolCall:
    """A proposed invocation. It is a *proposal* only: the runtime validates,
    gates and audits it before any handler runs."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolResult:
    """The outcome of executing (or refusing) a tool call."""

    ok: bool
    name: str
    data: Any = None
    error: str = ""
    decision: str = ""          # allowed | denied | degraded | replayed
    replayed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContextBundle:
    """A deterministic, LLM-free snapshot handed to reasoning and tools."""

    now: int = 0
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentReply:
    """What the runtime returns to a front-end.

    ``text`` is intentionally empty unless a renderer/LLM produced it — the
    runtime never invents a sentence (AGENTS.md: no canned replies). ``data``
    always carries the structured truth.
    """

    ok: bool
    text: str = ""
    data: Any = None
    intent: Intent | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    source: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "data": self.data,
            "intent": self.intent.to_dict() if self.intent else None,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "results": [r.to_dict() for r in self.results],
            "source": self.source,
            "error": self.error,
        }
