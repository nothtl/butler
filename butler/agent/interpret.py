"""Phase 7 / M2: natural language -> typed :class:`AgentRequest`.

Interpretation is deliberately layered:

* :class:`DeterministicInterpreter` is the safe default. It does **not** grow a
  pile of regexes; it uses a small, ordered keyword table, the existing
  :class:`~butler.decider.Decider` parser as a legacy fallback, live entity
  lookup, and the deterministic :class:`~butler.agent.temporal.TemporalResolver`.
* :class:`LLMInterpreter` lets a model *propose* a structured request, but the
  proposal is parsed with ``AgentRequest.from_dict(strict=True)`` and never
  trusted: unknown fields, unknown enums, bad confidence and incoherent
  target/action combinations are rejected before any domain logic runs.

The rest of the system depends only on the :class:`SemanticInterpreter`
protocol, so an external reasoning runtime can supply its own interpretation
without changing the executive layer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, runtime_checkable

from .errors import SemanticValidationError
from .semantic import (
    ActionKind, AgentRequest, Ambiguity, AmbiguityKind, Constraint,
    ConstraintKind, ConstraintSource, EntityRef, EntityType, Hardness,
    RequestIntent, TemporalRange, _TARGET_REQUIRED,
)
from .temporal import Clock, TemporalResolver

log = logging.getLogger("butler.agent.interpret")

# Ordered, most-specific-first. Substring matching only — not a regex pile.
_ADVISE = (
    "what should i", "what should we", "should i", "recommend",
    "what now", "help me decide", "best use of", "how should i spend",
    "what's the best", "whats the best",
)
_PLAN = (
    "plan my", "plan the", "plan day", "plan week", "plan out",
    "make a plan", "schedule my", "schedule the",
)
_FEASIBILITY = (
    "can i", "do i have time", "is there time", "will i have time",
    "do i have enough", "enough time", "fit in", "feasible", "possible to",
)
_URGENCY = (
    "most urgent", "what's urgent", "whats urgent", "what is urgent",
    "priority", "priorities", "what matters most", "top tasks",
    "what should i prioritize",
)
_STATUS = (
    "status", "how am i doing", "what's on", "whats on", "what do i have",
    "overview", "summary", "brief me", "catch me up",
)
_MOVE = ("move ", "reschedule", "defer", "push back", "push ", "shift ")

# --- M3 project intelligence ------------------------------------------------
_PROJECT_CREATE = (
    "create a project", "new project", "add a project", "start a project",
    "break", "milestone", "project plan",
)
_PROJECT_CREATE_HINTS = (
    "i have a", "i'm starting", "im starting", "starting a", "kick off",
    "set up", "plan out", "project due", "working on a project",
)
_PROJECT_EFFORT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:hour|hr|h|minute|min|m)s?\b|\b(?:two|three|four|"
    r"five|six|seven|eight|nine|ten)\s+hours?\b", re.I)
_PROJECT_DEP = (
    "depend", "blocking", "what's blocking", "whats blocking", "prerequisite",
    "blocked by",
)
_PROJECT_RISK = (
    "at risk", "most at risk", "risk", "behind", "on track", "slipping",
    "am i behind",
)
_PROJECT_WORKLOAD = (
    "how much work", "workload", "how many hours", "hours do i need",
    "work left", "much work left", "how long will", "remaining effort",
)
_PROJECT_NEXT = (
    "what should i work on", "work on next", "what's next", "whats next",
    "next task", "what to work on",
)
_PROJECT_STATUS = (
    "progress", "on track", "how far", "how is", "how's", "status",
)
_PROJECT_TARGETED = frozenset({
    ActionKind.PROJECT_STATUS, ActionKind.PROJECT_WORKLOAD,
    ActionKind.PROJECT_RISK, ActionKind.PROJECT_DEPENDENCIES,
    ActionKind.PROJECT_NEXT,
})

# --- M4 web & external knowledge --------------------------------------------
# Local knowledge is checked BEFORE web: "what do you know about my CS168
# deadline" is a local-state question, while "check online ..." is external.
_KNOWLEDGE = (
    "what do you know about", "what do you know", "do you know about",
    "do you know", "what's in my", "whats in my", "from my", "locally",
    "in my butler", "my notes on", "in my records", "in my data",
)
_WEB_RESEARCH = (
    "check online", "search online", "look online", "search the web",
    "check the web", "look up online", "find online", "web search",
    "google", "the latest", "latest on", "latest about", "latest news",
    "breaking news", "recent news",
    "check the course page", "check the page", "check the website",
)
_WEB_SEARCH = (
    "search for", "find information about", "find info about",
    "search the internet", "look up information", "research",
)
_WEB_FETCH = (
    "open this webpage", "open the webpage", "open this page",
    "open the page", "open this url", "open the url", "fetch the page",
    "fetch this page", "open this link", "open the link",
    "open it", "open that", "open this",
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)

# --- M5 schedule optimization -----------------------------------------------
# Checked after web, before project: "rearrange my week" is an optimization
# request, while "move my CS168 to tomorrow" stays a plain single move.
_OPTIMIZE_WEEK = (
    "optimize my week", "optimize the week", "optimize week",
    "best way to schedule", "best way to plan", "schedule this week",
    "fit all my work", "fit everything in", "fit it all in",
    "fit all my tasks", "rearrange my week", "rearrange the week",
    "rearrange my schedule", "rearrange", "optimize my schedule",
    "optimize my time", "optimize",
)
_OPTIMIZE_DAY = (
    "optimize my day", "optimize the day", "optimize day",
    "plan my day properly", "schedule my day properly",
    "best schedule today", "best plan for today", "make the most of my day",
)
_EVALUATE_SCHEDULE = (
    "evaluate my schedule", "evaluate the schedule", "evaluate schedule",
    "is my schedule feasible", "is the schedule feasible",
    "can i finish everything", "will everything fit", "will it all fit",
    "does everything fit", "can i fit everything",
)
_FIND_BEST_SLOT = (
    "find the best slot", "find a slot", "find the best time",
    "best time to", "best time for", "when should i", "best slot",
    "when's the best time", "what's the best time", "whats the best time",
)
_RESCHEDULE_OPT = (
    "move this without", "move it without", "reschedule so",
    "rearrange so", "rearrange to", "without messing up",
    "without disrupting", "move my study", "move my session",
    "move my study session", "shift things around",
)

_TEMPORAL_MARKERS = (
    "tonight", "this evening", "this morning", "this afternoon", "tomorrow",
    "next week", "this week", "rest of the week", "today", "after dinner",
    "after supper", "before class", "before my class", "right now", "now",
)

_REFERENCE_RE = re.compile(
    r"\b(that|it|this|the other assignment|the other one|the other|"
    r"that one|that task|that block|the same one)\b", re.I)

_COURSE_CODE_RE = re.compile(r"\b([A-Za-z]{2,6}\s?\d{2,4}[A-Za-z]?)\b")


@runtime_checkable
class SemanticInterpreter(Protocol):
    """Anything that can turn text into a typed request (or ``None``)."""

    def interpret(self, text: str, *, context: Any = None,
                  user: str = "user") -> Any: ...


def _match(text: str, table: tuple[str, ...]) -> bool:
    return any(token in text for token in table)


class DeterministicInterpreter:
    """Rule-light, deterministic interpretation with a legacy fallback."""

    def __init__(self, container: Any, *, now_ts: int | None = None):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.clock = Clock.from_config(self.cfg, now_ts=now_ts) \
            if self.cfg is not None else Clock(now_ts=now_ts)
        self.resolver = TemporalResolver(
            self.clock,
            sleep_start=int(getattr(self.cfg, "sleep_start", 23 * 60) or 0),
            sleep_end=int(getattr(self.cfg, "sleep_end", 7 * 60) or 0),
            class_windows=self._class_windows(),
        )

    # ------------------------------------------------------------- public
    def interpret(self, text: str, *, context: Any = None,
                  user: str = "user") -> AgentRequest:
        raw = (text or "").strip()
        low = raw.lower()
        intent, action, confidence = self._classify(low, context)
        phrase, temporal = self._temporal(low)
        scope = self.resolver.scope(phrase, temporal)
        entities = self._entities(low, raw)
        target = self._target(action, entities, low)
        if action == ActionKind.WEB_FETCH and target is None \
                and context is not None:
            focus = getattr(context, "focus", None)
            if focus is not None and focus.type == EntityType.URL \
                    and focus.name:
                target = focus
        preferences = self._preferences(low)
        constraints = self._constraints(preferences)
        ambiguity = self._ambiguity(action, target, low)

        return AgentRequest(
            intent=intent, action=action, target=target, entities=entities,
            scope=scope, constraints=constraints, preferences=preferences,
            temporal=temporal, conversation=context, confidence=confidence,
            ambiguity=ambiguity, requires_confirmation=(action in _MUTATING),
            raw_text=raw, source="deterministic",
        )

    # ---------------------------------------------------------- classify
    def _classify(self, low: str, context: Any = None
                  ) -> tuple[RequestIntent, ActionKind, float]:
        knowledge = self._knowledge_classify(low)
        if knowledge is not None:
            return knowledge
        web = self._web_classify(low)
        if web is not None:
            return web
        optimizer = self._optimizer_classify(low)
        if optimizer is not None:
            return optimizer
        project = self._project_classify(low)
        if project is not None:
            return project
        if _match(low, _MOVE):
            action = ActionKind.RESCHEDULE if "reschedule" in low else ActionKind.MOVE
            if "defer" in low or "push" in low:
                action = ActionKind.DEFER
            return RequestIntent.MUTATE, action, 0.7
        if _match(low, _PLAN):
            action = ActionKind.PLAN_WEEK if "week" in low else ActionKind.PLAN_DAY
            return RequestIntent.PLAN, action, 0.75
        if _match(low, _FEASIBILITY):
            return RequestIntent.EVALUATE, ActionKind.FEASIBILITY, 0.7
        if _match(low, _URGENCY):
            return RequestIntent.QUERY, ActionKind.URGENCY, 0.7
        if _match(low, _STATUS):
            return RequestIntent.QUERY, ActionKind.STATUS, 0.6
        if _match(low, _ADVISE):
            return RequestIntent.ADVISE, ActionKind.RECOMMEND, 0.75
        # legacy deterministic parser as the last structured fallback
        legacy = self._legacy(low)
        if legacy is not None:
            return legacy
        return RequestIntent.CHAT, ActionKind.UNKNOWN, 0.3

    def _knowledge_classify(self, low: str
                            ) -> tuple[RequestIntent, ActionKind, float] | None:
        if _match(low, _KNOWLEDGE):
            return RequestIntent.QUERY, ActionKind.KNOWLEDGE_LOOKUP, 0.7
        return None

    def _web_classify(self, low: str
                      ) -> tuple[RequestIntent, ActionKind, float] | None:
        if _URL_RE.search(low):
            return RequestIntent.QUERY, ActionKind.WEB_FETCH, 0.8
        if _match(low, _WEB_FETCH):
            return RequestIntent.QUERY, ActionKind.WEB_FETCH, 0.75
        if _match(low, _WEB_RESEARCH):
            return RequestIntent.QUERY, ActionKind.WEB_RESEARCH, 0.75
        if _match(low, _WEB_SEARCH):
            return RequestIntent.QUERY, ActionKind.WEB_SEARCH, 0.7
        return None

    def _optimizer_classify(self, low: str
                            ) -> tuple[RequestIntent, ActionKind, float] | None:
        if _match(low, _RESCHEDULE_OPT):
            return RequestIntent.MUTATE, ActionKind.RESCHEDULE_OPTIMIZED, 0.7
        if _match(low, _EVALUATE_SCHEDULE):
            return RequestIntent.EVALUATE, ActionKind.EVALUATE_SCHEDULE, 0.7
        if _match(low, _FIND_BEST_SLOT):
            return RequestIntent.QUERY, ActionKind.FIND_BEST_SLOT, 0.7
        if _match(low, _OPTIMIZE_DAY):
            return RequestIntent.PLAN, ActionKind.OPTIMIZE_DAY, 0.75
        if _match(low, _OPTIMIZE_WEEK):
            return RequestIntent.PLAN, ActionKind.OPTIMIZE_WEEK, 0.75
        return None

    def _project_classify(self, low: str
                          ) -> tuple[RequestIntent, ActionKind, float] | None:
        names = self._project_names()
        has = ("project" in low or any(n and n in low for n in names)
               or "am i behind" in low or "on track" in low)
        if not has:
            return None
        if _match(low, _PROJECT_DEP):
            return RequestIntent.QUERY, ActionKind.PROJECT_DEPENDENCIES, 0.7
        if _match(low, _PROJECT_RISK):
            return RequestIntent.QUERY, ActionKind.PROJECT_RISK, 0.7
        if _match(low, _PROJECT_WORKLOAD):
            return RequestIntent.QUERY, ActionKind.PROJECT_WORKLOAD, 0.7
        if _match(low, _PROJECT_NEXT):
            return RequestIntent.ADVISE, ActionKind.PROJECT_NEXT, 0.7
        explicit = _match(low, _PROJECT_CREATE) and (
            "project" in low or "milestone" in low)
        inferred = ("project" in low and
                    (_match(low, _PROJECT_CREATE_HINTS)
                     or _PROJECT_EFFORT_RE.search(low) is not None))
        if explicit or inferred:
            return RequestIntent.MUTATE, ActionKind.CREATE_PROJECT, 0.7
        if _match(low, _PROJECT_STATUS) or "project" in low:
            return RequestIntent.QUERY, ActionKind.PROJECT_STATUS, 0.65
        return None

    def _project_names(self) -> list[str]:
        pmod = getattr(self.container, "projects", None)
        if pmod is None or not hasattr(pmod, "list_projects"):
            return []
        try:
            return [str(p.get("name", "")).strip().lower()
                    for p in pmod.list_projects()]
        except Exception:  # noqa: BLE001
            return []

    def _legacy(self, low: str) -> tuple[RequestIntent, ActionKind, float] | None:
        decider = getattr(self.container, "decider", None)
        if decider is None or not hasattr(decider, "parse"):
            return None
        try:
            intent = decider.parse(low)
        except Exception:  # noqa: BLE001 — interpretation must never crash
            log.debug("legacy parse failed", exc_info=True)
            return None
        kind = str(getattr(intent, "kind", "") or "")
        mapping = {
            "now": (RequestIntent.ADVISE, ActionKind.RECOMMEND),
            "why": (RequestIntent.QUERY, ActionKind.URGENCY),
            "why_this": (RequestIntent.QUERY, ActionKind.URGENCY),
            "day": (RequestIntent.PLAN, ActionKind.PLAN_DAY),
            "week": (RequestIntent.PLAN, ActionKind.PLAN_WEEK),
            "move_block": (RequestIntent.MUTATE, ActionKind.MOVE),
            "defer_feedback": (RequestIntent.MUTATE, ActionKind.DEFER),
            "briefing": (RequestIntent.QUERY, ActionKind.STATUS),
            "review": (RequestIntent.QUERY, ActionKind.STATUS),
        }
        if kind in mapping:
            req_intent, action = mapping[kind]
            return req_intent, action, 0.65
        return None

    # ------------------------------------------------------------ temporal
    def _temporal(self, low: str) -> tuple[str, TemporalRange]:
        phrase = ""
        for marker in _TEMPORAL_MARKERS:
            if marker in low:
                phrase = marker
                break
        if not phrase:
            m = re.search(
                r"\bin\s+[a-z0-9\-]+\s*(?:hour|hr|h|minute|min|m|day|d|week|w)s?\b",
                low)
            if m:
                phrase = m.group(0)
        if not phrase:
            m = re.search(r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", low)
            if m:
                phrase = m.group(0)
        if not phrase:
            return "", TemporalRange()
        return phrase, self.resolver.resolve(phrase)

    # ------------------------------------------------------------ entities
    def _entities(self, low: str, raw: str = "") -> list[EntityRef]:
        out: list[EntityRef] = []
        for m in _URL_RE.finditer(raw or low):
            url = m.group(0).rstrip(".,);]")
            out.append(EntityRef(
                type=EntityType.URL, id=url, name=url, resolved=True,
                confidence=0.95, source="url_mention"))
        for course in self._courses():
            code = str(course.get("code", "") or "")
            if not code:
                continue
            if re.search(r"\b" + re.escape(code.lower()) + r"\b", low):
                out.append(EntityRef(
                    type=EntityType.COURSE, id=str(course.get("id", "")),
                    name=code, resolved=True, confidence=0.9,
                    source="course_table"))
        for proj in self._project_rows():
            name = str(proj.get("name", "") or "")
            if len(name) < 3:
                continue
            if name.lower() in low:
                out.append(EntityRef(
                    type=EntityType.PROJECT, id=str(proj.get("id", "")),
                    name=name, resolved=True, confidence=0.85,
                    source="project_table"))
        for task in self._tasks():
            title = str(task.get("title", "") or "")
            if len(title) < 4:
                continue
            if title.lower() in low:
                out.append(EntityRef(
                    type=EntityType.TASK, id=str(task.get("id", "")),
                    name=title, resolved=True, confidence=0.8,
                    source="task_table"))
        if not out:
            m = _COURSE_CODE_RE.search(low)
            if m:
                out.append(EntityRef(
                    type=EntityType.COURSE, name=m.group(1).strip().upper(),
                    resolved=False, confidence=0.3, source="mention"))
        return out

    def _target(self, action: ActionKind, entities: list[EntityRef],
                low: str) -> EntityRef | None:
        if action == ActionKind.WEB_FETCH:
            urls = [e for e in entities if e.type == EntityType.URL]
            return urls[0] if urls else None
        if action == ActionKind.FIND_BEST_SLOT:
            resolved = [e for e in entities if e.resolved]
            return resolved[0] if resolved else None
        if action in _PROJECT_TARGETED:
            proj = [e for e in entities
                    if e.type == EntityType.PROJECT and e.resolved]
            return proj[0] if proj else None
        if action not in _TARGET_REQUIRED:
            return None
        resolved = [e for e in entities if e.resolved]
        if resolved:
            return resolved[0]
        m = _REFERENCE_RE.search(low)
        if m:
            return EntityRef(type=EntityType.UNKNOWN, name=m.group(0).lower(),
                             resolved=False, confidence=0.2, source="pronoun")
        return None

    # --------------------------------------------------------- preferences
    def _preferences(self, low: str) -> list[str]:
        prefs: list[str] = []
        try:
            from .. import affinity
            weights = affinity.preference_weights(low)
            prefs += [f"prefer:{k}" for k, v in weights.items() if v > 0]
            if affinity.is_tired(low):
                prefs.append("low_energy")
        except Exception:  # noqa: BLE001
            pass
        return sorted(set(prefs))[:8]

    def _constraints(self, preferences: list[str]) -> list[Constraint]:
        out: list[Constraint] = []
        sleep_start = int(getattr(self.cfg, "sleep_start", 23 * 60) or 0)
        sleep_end = int(getattr(self.cfg, "sleep_end", 7 * 60) or 0)
        if sleep_start and sleep_end:
            out.append(Constraint(
                kind=ConstraintKind.SLEEP, hardness=Hardness.HARD,
                source=ConstraintSource.SYSTEM, label="sleep",
                value={"start_min": sleep_start, "end_min": sleep_end},
                note="authoritative quiet window"))
        if "low_energy" in preferences:
            out.append(Constraint(
                kind=ConstraintKind.ENERGY, hardness=Hardness.SOFT,
                source=ConstraintSource.EXPLICIT_USER, label="low_energy"))
        return out

    # ---------------------------------------------------------- ambiguity
    def _ambiguity(self, action: ActionKind, target: EntityRef | None,
                   low: str) -> list[Ambiguity]:
        if action == ActionKind.WEB_FETCH and target is None:
            return [Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="url",
                mention=low[:60],
                reason="I need the web address you want me to open")]
        if action in _TARGET_REQUIRED and target is None:
            return [Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="target",
                mention=low[:60],
                reason="I need to know which task or block you mean")]
        return []

    # -------------------------------------------------------------- data
    def _project_rows(self) -> list[dict[str, Any]]:
        pmod = getattr(self.container, "projects", None)
        if pmod is None or not hasattr(pmod, "list_projects"):
            return []
        try:
            return list(pmod.list_projects())
        except Exception:  # noqa: BLE001
            return []

    def _courses(self) -> list[dict[str, Any]]:
        db = getattr(self.container, "db", None)
        if db is None or not hasattr(db, "courses"):
            return []
        try:
            return [dict(r) for r in db.courses()]
        except Exception:  # noqa: BLE001
            return []

    def _tasks(self) -> list[dict[str, Any]]:
        db = getattr(self.container, "db", None)
        if db is None or not hasattr(db, "tasks"):
            return []
        try:
            return [dict(r) for r in db.tasks("active")]
        except Exception:  # noqa: BLE001
            return []

    def _class_windows(self) -> list[tuple[int, int, str]]:
        """(start_ts, end_ts, title) for upcoming classes, if available."""
        db = getattr(self.container, "db", None)
        if db is None or not hasattr(db, "events_between"):
            return []
        try:
            now = self.clock.now_ts()
            rows = db.events_between(now, now + 14 * 86400)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for r in rows:
            title = str(r["title"] or "")
            if "class" in title.lower() or "lecture" in title.lower():
                out.append((int(r["start_ts"]), int(r["end_ts"]), title))
        return out


#: Actions whose mere interpretation never authorises a side effect.
_MUTATING = frozenset({
    ActionKind.MOVE, ActionKind.RESCHEDULE, ActionKind.DEFER,
    ActionKind.CREATE_TASK, ActionKind.COMPLETE_TASK, ActionKind.UPDATE,
    ActionKind.CREATE_PROJECT, ActionKind.RESCHEDULE_OPTIMIZED,
})


class LLMInterpreter:
    """Let a model propose an :class:`AgentRequest`; validate it strictly.

    The model output is treated as untrusted input: it must be a single JSON
    object matching the request schema. Anything else raises
    :class:`SemanticValidationError`, which the service turns into an
    ``invalid`` result rather than guessing.
    """

    SYSTEM = (
        "You convert a user's message into a single JSON object for a personal "
        "assistant. Output ONLY JSON, no prose. Schema keys: intent "
        "(advise|plan|evaluate|query|mutate|chat|unknown), action (recommend|"
        "plan_day|plan_week|feasibility|urgency|status|move|reschedule|defer|"
        "create_task|complete_task|update|project_status|project_workload|"
        "project_risk|project_dependencies|project_next|create_project|"
        "web_search|web_research|web_fetch|knowledge_lookup|optimize_day|"
        "optimize_week|evaluate_schedule|reschedule_optimized|find_best_slot|"
        "unknown), "
        "target, entities, scope, "
        "constraints, preferences, temporal, confidence, raw_text. Never mark "
        "an inferred preference as a hard constraint. If unsure, use unknown "
        "and low confidence rather than inventing values."
    )

    def __init__(self, llm: Any):
        self.llm = llm

    def interpret(self, text: str, *, context: Any = None,
                  user: str = "user") -> Any:
        raw = (text or "").strip()
        payload = self._call(raw)
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise SemanticValidationError("llm: response was not a JSON object")
        payload.setdefault("raw_text", raw)
        payload.setdefault("source", "llm")
        req = AgentRequest.from_dict(payload, strict=True)
        if context is not None and req.conversation is None:
            req.conversation = context
        return req

    def _call(self, text: str) -> Any:
        prompt = (self.SYSTEM, text)
        try:
            out = self.llm(prompt) if callable(self.llm) else \
                self.llm._llm(prompt)
        except Exception as exc:  # noqa: BLE001
            raise SemanticValidationError(f"llm: call failed ({exc})") from exc
        if not out:
            return None
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end < start:
            raise SemanticValidationError("llm: no JSON object in response")
        try:
            return json.loads(out[start:end + 1])
        except (TypeError, ValueError) as exc:
            raise SemanticValidationError(
                f"llm: malformed JSON ({exc})") from exc


def default_interpreter(container: Any) -> DeterministicInterpreter:
    return DeterministicInterpreter(container)
