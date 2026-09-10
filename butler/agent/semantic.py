"""Phase 7 / M2: the typed semantic + domain contract.

This module is the single structured vocabulary that crosses the boundary
between an external reasoning runtime (AI Butler) and Pi Butler's deterministic
executive layer. It is deliberately stdlib-only (no Pydantic): the contract must
be cheap to import, trivially serialisable over MCP/JSON, and impossible to
violate silently.

Two rules are encoded structurally rather than by convention:

* **Inferred preferences can never be hard.** :class:`Constraint` refuses to
  exist when ``hardness == hard`` and its ``source`` is inferred (routine,
  location or learned preference). A model that tries to smuggle a soft signal
  in as a hard constraint raises :class:`SemanticValidationError`.
* **A model proposal is data, not truth.** :meth:`AgentRequest.from_dict`
  rejects unknown fields, unknown enum values, out-of-range confidence and
  incoherent target/action combinations before any domain logic runs.

The vocabulary is intentionally richer than a single intent string: a request
carries an intent, a concrete action, entities, a scope, hard/soft constraints,
temporal resolution, bounded conversation context, ambiguity and confidence.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any

from .errors import SemanticValidationError

# ---------------------------------------------------------------------------
# serialisation helper
# ---------------------------------------------------------------------------


def _dump(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _dump(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _dump(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(v) for v in value]
    return value


def _enum(cls: type[Enum], value: Any, field_name: str,
          *, default: Enum | None = None) -> Any:
    if value is None or value == "":
        if default is not None:
            return default
        raise SemanticValidationError(f"{field_name}: missing value")
    if isinstance(value, cls):
        return value
    try:
        return cls(str(value).strip().lower())
    except ValueError as exc:
        allowed = ", ".join(e.value for e in cls)
        raise SemanticValidationError(
            f"{field_name}: unknown value {value!r}; expected one of {allowed}"
        ) from exc


def _confidence(value: Any, field_name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise SemanticValidationError(
            f"{field_name}: confidence must be a number") from exc
    if not 0.0 <= out <= 1.0:
        raise SemanticValidationError(
            f"{field_name}: confidence must be within [0, 1], got {out}")
    return out


def _obj(value: Any, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SemanticValidationError(f"{field_name}: expected a JSON object")
    return value


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------


class RequestIntent(str, Enum):
    """What the user is fundamentally asking for."""

    ADVISE = "advise"          # recommend / decide
    PLAN = "plan"              # produce a schedule
    EVALUATE = "evaluate"      # is this feasible / do I have time
    QUERY = "query"            # read status / rank
    MUTATE = "mutate"          # change state (gated)
    CHAT = "chat"              # conversational, no domain action
    UNKNOWN = "unknown"


class ActionKind(str, Enum):
    """The concrete operation requested (finer than :class:`RequestIntent`)."""

    RECOMMEND = "recommend"
    PLAN_DAY = "plan_day"
    PLAN_WEEK = "plan_week"
    FEASIBILITY = "feasibility"
    URGENCY = "urgency"
    STATUS = "status"
    MOVE = "move"
    RESCHEDULE = "reschedule"
    DEFER = "defer"
    CREATE_TASK = "create_task"
    COMPLETE_TASK = "complete_task"
    UPDATE = "update"
    # --- M3 project intelligence (read-only unless noted) ---
    PROJECT_STATUS = "project_status"
    PROJECT_WORKLOAD = "project_workload"
    PROJECT_RISK = "project_risk"
    PROJECT_DEPENDENCIES = "project_dependencies"
    PROJECT_NEXT = "project_next"
    CREATE_PROJECT = "create_project"
    # --- M4 web & external knowledge (strictly read-only) ---
    WEB_SEARCH = "web_search"
    WEB_RESEARCH = "web_research"
    WEB_FETCH = "web_fetch"
    KNOWLEDGE_LOOKUP = "knowledge_lookup"
    # --- M5 schedule optimization (read-only; reschedule is gated) ---
    OPTIMIZE_DAY = "optimize_day"
    OPTIMIZE_WEEK = "optimize_week"
    EVALUATE_SCHEDULE = "evaluate_schedule"
    RESCHEDULE_OPTIMIZED = "reschedule_optimized"
    FIND_BEST_SLOT = "find_best_slot"
    # --- M6 long-term memory + learning ---
    MEMORY_QUERY = "memory_query"
    MEMORY_SEARCH = "memory_search"
    MEMORY_EXPLAIN = "memory_explain"
    MEMORY_FORGET = "memory_forget"
    MEMORY_CONFIRM = "memory_confirm"
    MEMORY_CORRECT = "memory_correct"
    MEMORY_LEARN = "memory_learn"
    # --- M7 proactive executive behavior ---
    PROACTIVE_QUERY = "proactive_query"
    PROACTIVE_LIST = "proactive_list"
    PROACTIVE_EXPLAIN = "proactive_explain"
    PROACTIVE_SNOOZE = "proactive_snooze"
    PROACTIVE_SUPPRESS = "proactive_suppress"
    # --- N2 universal tracking / triggers ---
    TRACKER_CREATE = "tracker_create"
    TRACKER_LIST = "tracker_list"
    TRACKER_QUERY = "tracker_query"
    TRACKER_CONTROL = "tracker_control"
    TRACKER_EVALUATE = "tracker_evaluate"
    TRACKER_EXPLAIN = "tracker_explain"
    UNKNOWN = "unknown"


class EntityType(str, Enum):
    TASK = "task"
    COURSE = "course"
    PROJECT = "project"
    EVENT = "event"
    DEADLINE = "deadline"
    BLOCK = "block"
    LOCATION = "location"
    TIME = "time"
    URL = "url"
    UNKNOWN = "unknown"


class ScopeKind(str, Enum):
    NOW = "now"
    TODAY = "today"
    TONIGHT = "tonight"
    TOMORROW = "tomorrow"
    THIS_WEEK = "this_week"
    NEXT_WEEK = "next_week"
    RANGE = "range"
    UNKNOWN = "unknown"


class Hardness(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class ConstraintKind(str, Enum):
    CALENDAR_EVENT = "calendar_event"
    CLASS = "class"
    EXAM = "exam"
    APPOINTMENT = "appointment"
    FLIGHT = "flight"
    MEETING = "meeting"
    SLEEP = "sleep"
    DEADLINE = "deadline"
    PREFERRED_TIME = "preferred_time"
    ENERGY = "energy"
    ROUTINE = "routine"
    FOOD = "food"
    EXERCISE = "exercise"
    LOCATION = "location"
    FRAGMENTATION = "fragmentation"
    OTHER = "other"


class ConstraintSource(str, Enum):
    EXPLICIT_USER = "explicit_user"
    CALENDAR = "calendar"
    DEADLINE = "deadline"
    SYSTEM = "system"
    ROUTINE_INFERRED = "routine_inferred"
    LOCATION_INFERRED = "location_inferred"
    PREFERENCE_LEARNED = "preference_learned"


#: Sources that are *never* authoritative enough to be hard constraints.
INFERRED_SOURCES = frozenset({
    ConstraintSource.ROUTINE_INFERRED,
    ConstraintSource.LOCATION_INFERRED,
    ConstraintSource.PREFERENCE_LEARNED,
})


class TemporalResolution(str, Enum):
    EXPLICIT = "explicit"       # an exact instant was given
    RESOLVED = "resolved"       # safely derived from the phrase
    INFERRED = "inferred"       # best-effort, soft
    UNRESOLVED = "unresolved"   # not safe to guess


class AmbiguityKind(str, Enum):
    ENTITY = "entity"
    TIME = "time"
    SCOPE = "scope"
    ACTION = "action"


class ResultStatus(str, Enum):
    OK = "ok"
    NEEDS_CONFIRMATION = "needs_confirmation"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class ProvenanceKind(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
    CONVERSATION = "conversation"
    DOMAIN_TOOL = "domain_tool"
    USER = "user"


# Actions that cannot be interpreted without a concrete target entity.
_TARGET_REQUIRED = frozenset({
    ActionKind.MOVE,
    ActionKind.RESCHEDULE,
    ActionKind.DEFER,
    ActionKind.COMPLETE_TASK,
    ActionKind.UPDATE,
})


# ---------------------------------------------------------------------------
# leaf models
# ---------------------------------------------------------------------------


@dataclass
class EntityRef:
    """A reference to a domain entity, resolved or not.

    ``resolved`` is True only when ``id`` was matched against live state. An
    unresolved mention ("that assignment") carries ``candidates`` instead so the
    caller can ask rather than guess.
    """

    type: EntityType = EntityType.UNKNOWN
    id: str = ""
    name: str = ""
    resolved: bool = False
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    def key(self) -> str:
        return f"{self.type.value}:{self.id}" if self.id else \
            self.name.strip().lower()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EntityRef":
        d = _obj(d, "entity")
        return cls(
            type=_enum(EntityType, d.get("type"), "entity.type",
                       default=EntityType.UNKNOWN),
            id=str(d.get("id", "") or ""),
            name=str(d.get("name", "") or ""),
            resolved=bool(d.get("resolved", False)),
            confidence=_confidence(d.get("confidence", 0.0),
                                   "entity.confidence"),
            candidates=[dict(x) for x in (d.get("candidates") or [])
                        if isinstance(x, dict)],
            source=str(d.get("source", "") or ""),
        )


@dataclass
class Constraint:
    """A structured boundary. Hard constraints are inviolable; inferred sources
    may never be hard."""

    kind: ConstraintKind = ConstraintKind.OTHER
    hardness: Hardness = Hardness.SOFT
    source: ConstraintSource = ConstraintSource.EXPLICIT_USER
    label: str = ""
    start: int = 0
    end: int = 0
    value: Any = None
    confidence: float = 1.0
    note: str = ""

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> "Constraint":
        if self.hardness == Hardness.HARD and self.source in INFERRED_SOURCES:
            raise SemanticValidationError(
                f"constraint {self.kind.value!r}: inferred source "
                f"{self.source.value!r} may never be hard")
        if self.start and self.end and self.end < self.start:
            raise SemanticValidationError(
                f"constraint {self.kind.value!r}: end before start")
        _confidence(self.confidence, "constraint.confidence")
        return self

    def is_hard(self) -> bool:
        return self.hardness == Hardness.HARD

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Constraint":
        d = _obj(d, "constraint")
        return cls(
            kind=_enum(ConstraintKind, d.get("kind"), "constraint.kind",
                       default=ConstraintKind.OTHER),
            hardness=_enum(Hardness, d.get("hardness"), "constraint.hardness",
                           default=Hardness.SOFT),
            source=_enum(ConstraintSource, d.get("source"),
                         "constraint.source",
                         default=ConstraintSource.EXPLICIT_USER),
            label=str(d.get("label", "") or ""),
            start=int(d.get("start", 0) or 0),
            end=int(d.get("end", 0) or 0),
            value=d.get("value"),
            confidence=_confidence(d.get("confidence", 1.0),
                                   "constraint.confidence"),
            note=str(d.get("note", "") or ""),
        )


@dataclass
class TemporalRange:
    """A resolved (or deliberately unresolved) time span.

    The original ``phrase`` is always preserved. When a phrase cannot be safely
    resolved, ``resolution`` is ``unresolved`` and ``start``/``end`` stay 0 —
    callers must ask, never invent a time.
    """

    phrase: str = ""
    start: int = 0
    end: int = 0
    timezone: str = ""
    resolution: TemporalResolution = TemporalResolution.UNRESOLVED
    confidence: float = 0.0
    all_day: bool = False
    source: str = ""

    def __post_init__(self) -> None:
        if self.start and self.end and self.end < self.start:
            raise SemanticValidationError("temporal: end before start")
        _confidence(self.confidence, "temporal.confidence")

    def is_resolved(self) -> bool:
        return (self.resolution in (TemporalResolution.EXPLICIT,
                                    TemporalResolution.RESOLVED)
                and self.start > 0)

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TemporalRange":
        d = _obj(d, "temporal")
        return cls(
            phrase=str(d.get("phrase", "") or ""),
            start=int(d.get("start", 0) or 0),
            end=int(d.get("end", 0) or 0),
            timezone=str(d.get("timezone", "") or ""),
            resolution=_enum(TemporalResolution, d.get("resolution"),
                             "temporal.resolution",
                             default=TemporalResolution.UNRESOLVED),
            confidence=_confidence(d.get("confidence", 0.0),
                                   "temporal.confidence"),
            all_day=bool(d.get("all_day", False)),
            source=str(d.get("source", "") or ""),
        )


@dataclass
class Scope:
    kind: ScopeKind = ScopeKind.UNKNOWN
    start: int = 0
    end: int = 0
    timezone: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scope":
        d = _obj(d, "scope")
        return cls(
            kind=_enum(ScopeKind, d.get("kind"), "scope.kind",
                       default=ScopeKind.UNKNOWN),
            start=int(d.get("start", 0) or 0),
            end=int(d.get("end", 0) or 0),
            timezone=str(d.get("timezone", "") or ""),
        )


@dataclass
class Ambiguity:
    kind: AmbiguityKind = AmbiguityKind.ENTITY
    field_name: str = ""
    mention: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Ambiguity":
        d = _obj(d, "ambiguity")
        return cls(
            kind=_enum(AmbiguityKind, d.get("kind"), "ambiguity.kind",
                       default=AmbiguityKind.ENTITY),
            field_name=str(d.get("field_name", d.get("field", "")) or ""),
            mention=str(d.get("mention", "") or ""),
            candidates=[dict(x) for x in (d.get("candidates") or [])
                        if isinstance(x, dict)],
            reason=str(d.get("reason", "") or ""),
        )


@dataclass
class ConversationContext:
    """A *bounded* slice of conversational state.

    Deliberately small: topic, the focused entity, a handful of recent
    entities, a digest of the last result and a short summary. This is not a
    second session system — it is just enough to resolve "that" / "the other
    one" without an unbounded history.
    """

    topic: str = ""
    focus: EntityRef | None = None
    recent_entities: list[EntityRef] = field(default_factory=list)
    last_result: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    turn_count: int = 0

    MAX_ENTITIES = 8

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConversationContext":
        d = _obj(d, "conversation")
        focus = d.get("focus")
        return cls(
            topic=str(d.get("topic", "") or ""),
            focus=EntityRef.from_dict(focus) if isinstance(focus, dict) else None,
            recent_entities=[EntityRef.from_dict(e)
                             for e in (d.get("recent_entities") or [])
                             if isinstance(e, dict)][:cls.MAX_ENTITIES],
            last_result=dict(d.get("last_result") or {}),
            summary=str(d.get("summary", "") or ""),
            turn_count=int(d.get("turn_count", 0) or 0),
        )


@dataclass
class Provenance:
    source: str = ""
    method: str = ""
    tool: str = ""
    generated: int = 0
    deterministic: bool = True
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Provenance":
        d = _obj(d, "provenance")
        return cls(
            source=str(d.get("source", "") or ""),
            method=str(d.get("method", "") or ""),
            tool=str(d.get("tool", "") or ""),
            generated=int(d.get("generated", 0) or 0),
            deterministic=bool(d.get("deterministic", True)),
            request_id=str(d.get("request_id", "") or ""),
        )


@dataclass
class Recommendation:
    action: ActionKind = ActionKind.UNKNOWN
    target: EntityRef | None = None
    title: str = ""
    reason: str = ""
    score: float = 0.0
    window: list[int] = field(default_factory=list)      # [start_min, end_min]
    window_ts: list[int] = field(default_factory=list)   # absolute epochs
    provenance: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recommendation":
        d = _obj(d, "recommendation")
        target = d.get("target")
        return cls(
            action=_enum(ActionKind, d.get("action"), "recommendation.action",
                         default=ActionKind.UNKNOWN),
            target=EntityRef.from_dict(target) if isinstance(target, dict)
            else None,
            title=str(d.get("title", "") or ""),
            reason=str(d.get("reason", "") or ""),
            score=float(d.get("score", 0.0) or 0.0),
            window=[int(x) for x in (d.get("window") or [])],
            window_ts=[int(x) for x in (d.get("window_ts") or [])],
            provenance=str(d.get("provenance", "") or ""),
        )


# ---------------------------------------------------------------------------
# context snapshot
# ---------------------------------------------------------------------------


@dataclass
class ContextSnapshot:
    """A typed, just-in-time slice of live state.

    Built per request from only the data the request needs (scope, entities,
    intent) and bounded, so a reasoning runtime never receives a full database
    dump. ``truncated`` records that a list was capped.
    """

    now: int = 0
    timezone: str = ""
    day_start: int = 0
    day_end: int = 0
    sleep_start: int = 0
    sleep_end: int = 0
    commitments: list[dict[str, Any]] = field(default_factory=list)
    available_windows: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    courses: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    deadlines: list[dict[str, Any]] = field(default_factory=list)
    current_plan: dict[str, Any] | None = None
    presence: dict[str, Any] = field(default_factory=dict)
    routines: list[dict[str, Any]] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    recent_state: dict[str, Any] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    truncated: bool = False
    focus: str = ""
    # --- M4: bounded external evidence (never a whole webpage) ---
    external_sources: list[dict[str, Any]] = field(default_factory=list)
    external_facts: list[dict[str, Any]] = field(default_factory=list)
    research_summary: str = ""
    research_timestamp: int = 0
    # --- M6: bounded, relevant long-term memory (never a full dump) ---
    relevant_memories: list[dict[str, Any]] = field(default_factory=list)
    memory_summary: str = ""
    memory_warnings: list[str] = field(default_factory=list)
    memory_timestamp: int = 0
    # --- N1: the current Telegram topic as relevance context (not a boundary) ---
    topic: dict[str, Any] = field(default_factory=dict)
    topic_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ContextSnapshot":
        d = _obj(d, "context")
        return cls(**{f.name: d.get(f.name, f.default)
                      for f in fields(cls)})


# ---------------------------------------------------------------------------
# request / result
# ---------------------------------------------------------------------------


@dataclass
class AgentRequest:
    """A fully structured user request, safe to validate deterministically."""

    intent: RequestIntent = RequestIntent.UNKNOWN
    action: ActionKind = ActionKind.UNKNOWN
    target: EntityRef | None = None
    entities: list[EntityRef] = field(default_factory=list)
    scope: Scope = field(default_factory=Scope)
    constraints: list[Constraint] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    temporal: TemporalRange = field(default_factory=TemporalRange)
    conversation: ConversationContext | None = None
    confidence: float = 0.0
    ambiguity: list[Ambiguity] = field(default_factory=list)
    requires_confirmation: bool = False
    raw_text: str = ""
    source: str = "deterministic"
    # N1: the current Telegram topic (chat_id/thread_id), used as relevance
    # context by the executive service. Empty for non-topic requests.
    topic: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    def hard_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if c.is_hard()]

    def soft_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if not c.is_hard()]

    def validate(self) -> "AgentRequest":
        _confidence(self.confidence, "request.confidence")
        if self.action in _TARGET_REQUIRED and self.target is None \
                and not self.entities and not self.ambiguity:
            raise SemanticValidationError(
                f"action {self.action.value!r} requires a target entity")
        return self

    @classmethod
    def from_dict(cls, d: dict[str, Any], *,
                  strict: bool = True) -> "AgentRequest":
        if not isinstance(d, dict):
            raise SemanticValidationError("request: expected a JSON object")
        if strict:
            unknown = set(d) - {f.name for f in fields(cls)}
            if unknown:
                raise SemanticValidationError(
                    "request: unknown field(s): " + ", ".join(sorted(unknown)))
        try:
            req = cls(
                intent=_enum(RequestIntent, d.get("intent"), "request.intent",
                             default=RequestIntent.UNKNOWN),
                action=_enum(ActionKind, d.get("action"), "request.action",
                             default=ActionKind.UNKNOWN),
                target=EntityRef.from_dict(d["target"])
                if isinstance(d.get("target"), dict) else None,
                entities=[EntityRef.from_dict(e)
                          for e in (d.get("entities") or [])
                          if isinstance(e, dict)],
                scope=Scope.from_dict(d.get("scope") or {}),
                constraints=[Constraint.from_dict(c)
                             for c in (d.get("constraints") or [])
                             if isinstance(c, dict)],
                preferences=[str(p) for p in (d.get("preferences") or [])],
                temporal=TemporalRange.from_dict(d.get("temporal") or {}),
                conversation=ConversationContext.from_dict(d["conversation"])
                if isinstance(d.get("conversation"), dict) else None,
                confidence=_confidence(d.get("confidence", 0.0),
                                       "request.confidence"),
                ambiguity=[Ambiguity.from_dict(a)
                           for a in (d.get("ambiguity") or [])
                           if isinstance(a, dict)],
                requires_confirmation=bool(
                    d.get("requires_confirmation", False)),
                raw_text=str(d.get("raw_text", "") or ""),
                source=str(d.get("source", "llm") or "llm"),
                topic=dict(d.get("topic") or {}),
            )
        except SemanticValidationError:
            raise
        except (TypeError, ValueError) as exc:
            raise SemanticValidationError(
                f"request: malformed structure ({exc})") from exc
        if strict:
            req.validate()
        return req


@dataclass
class AgentResult:
    """The structured domain answer returned to the reasoning runtime.

    ``answer`` may hold a live-rendered sentence, but the truth is always in the
    structured fields (facts, recommendations, warnings, conflicts, ...). The
    deterministic layer never invents a recommendation it cannot justify.
    """

    status: ResultStatus = ResultStatus.OK
    answer: str = ""
    data: Any = None
    facts: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    candidate_actions: list[dict[str, Any]] = field(default_factory=list)
    confirmation_required: bool = False
    provenance: list[Provenance] = field(default_factory=list)
    context: ContextSnapshot | None = None
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)

    @property
    def ok(self) -> bool:
        return self.status in (ResultStatus.OK, ResultStatus.NEEDS_CONFIRMATION)

    def with_provenance(self, source: str, method: str, *,
                        tool: str = "", deterministic: bool = True,
                        generated: int = 0,
                        request_id: str = "") -> "AgentResult":
        self.provenance.append(Provenance(
            source=source, method=method, tool=tool,
            generated=generated or int(time.time()),
            deterministic=deterministic, request_id=request_id))
        return self

    @classmethod
    def invalid(cls, error: str, *,
                missing: list[str] | None = None) -> "AgentResult":
        return cls(status=ResultStatus.INVALID, error=error,
                   missing_information=list(missing or []),
                   provenance=[Provenance(source="validator",
                                          method="reject",
                                          deterministic=True,
                                          generated=int(time.time()))])

    @classmethod
    def ambiguous(cls, ambiguities: list[Ambiguity]) -> "AgentResult":
        missing = [f"{a.kind.value}:{a.field_name or a.mention}"
                   for a in ambiguities]
        return cls(
            status=ResultStatus.AMBIGUOUS,
            warnings=ambiguity_warnings(ambiguities),
            missing_information=missing,
            candidate_actions=[{"kind": a.kind.value, "field": a.field_name,
                                "mention": a.mention,
                                "candidates": a.candidates}
                               for a in ambiguities],
            provenance=[Provenance(source="resolver", method="ambiguity",
                                   deterministic=True,
                                   generated=int(time.time()))],
        )


def ambiguity_warnings(ambiguities: list[Ambiguity]) -> list[str]:
    return [a.reason or f"ambiguous {a.kind.value}: {a.mention}"
            for a in ambiguities]
