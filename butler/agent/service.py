"""Phase 7 / M2: the deterministic executive service.

The service is the seam between a *typed request* (from AI Butler, an LLM, or
the deterministic interpreter) and Pi Butler's authoritative domain logic. It
does four things and nothing more:

1. **Validate** the request (enums, confidence, target/action coherence).
2. **Resolve** entity references and ambiguity against live state and the
   bounded conversation focus — asking rather than guessing.
3. **Gather** a just-in-time, request-scoped :class:`ContextSnapshot`.
4. **Evaluate** with existing deterministic domain code (planner, schedule) and
   return a structured :class:`AgentResult`.

It deliberately contains **no second agent loop** and never performs a side
effect: mutating actions return ``NEEDS_CONFIRMATION`` with candidate actions
so an authorised layer can act after consent.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from .context import ContextBuilder
from .errors import SemanticValidationError
from .interpret import DeterministicInterpreter
from .semantic import (
    ActionKind, AgentRequest, AgentResult, Ambiguity, AmbiguityKind, EntityRef,
    EntityType, Recommendation, RequestIntent, ResultStatus, ScopeKind,
    _TARGET_REQUIRED,
)
from .temporal import Clock, TemporalResolver

log = logging.getLogger("butler.agent.service")

_REFERENCE_WORDS = (
    "that", "it", "this", "that one", "the other", "the other one",
    "the other assignment", "the same one", "that task", "that block",
    "this task", "this block",
)

_WORD_NUMBERS = {
    "an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


class ExecutiveService:
    def __init__(self, container: Any, *, interpreter: Any = None,
                 now_ts: int | None = None):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.clock = Clock.from_config(self.cfg, now_ts=now_ts) \
            if self.cfg is not None else Clock(now_ts=now_ts)
        self.resolver = TemporalResolver(
            self.clock,
            sleep_start=int(getattr(self.cfg, "sleep_start", 23 * 60) or 0),
            sleep_end=int(getattr(self.cfg, "sleep_end", 7 * 60) or 0),
        )
        self.context = ContextBuilder(container)
        self.interpreter = interpreter or DeterministicInterpreter(
            container, now_ts=now_ts)
        self._fallback_session: Any = None

    # --------------------------------------------------------------- public
    def ask(self, *, request: Any = None, text: str = "", user: str = "user",
            include_context: bool = False) -> AgentResult:
        try:
            if request is not None:
                req = request if isinstance(request, AgentRequest) \
                    else AgentRequest.from_dict(request)
                req.validate()
            else:
                req = self.interpreter.interpret(text, user=user)
                if req is None:
                    return AgentResult.invalid("could not interpret request")
                req.validate()
        except SemanticValidationError as exc:
            return AgentResult.invalid(str(exc))
        return self.handle(req, user=user, include_context=include_context)

    def handle(self, req: AgentRequest, *, user: str = "user",
               include_context: bool = False) -> AgentResult:
        try:
            req.validate()
        except SemanticValidationError as exc:
            return AgentResult.invalid(str(exc))
        session = self.session(user)
        ambiguities = self._resolve(req, session)
        if ambiguities:
            req.ambiguity = ambiguities
            res = AgentResult.ambiguous(ambiguities)
            if include_context:
                res.context = self.context.build_snapshot(req)
            self._remember(session, req, res)
            return res
        snapshot = self.context.build_snapshot(req)
        try:
            res = self._dispatch(req, snapshot)
        except SemanticValidationError as exc:
            return AgentResult.invalid(str(exc))
        except Exception as exc:  # noqa: BLE001 — domain code must not crash ask
            log.warning("executive dispatch failed: %s", exc, exc_info=True)
            return AgentResult(status=ResultStatus.ERROR, error=str(exc))
        if include_context:
            res.context = snapshot
        if not res.provenance:
            res.with_provenance("executive_service", req.action.value,
                                deterministic=True)
        self._remember(session, req, res)
        return res

    # -------------------------------------------------------------- session
    @property
    def store(self) -> Any:
        agent = getattr(self.container, "agent", None)
        return getattr(agent, "store", None)

    def session(self, user: str = "user") -> Any:
        store = self.store
        if store is not None and hasattr(store, "get"):
            return store.get(user)
        if self._fallback_session is None:
            from .session import Session
            self._fallback_session = Session(user=user)
        return self._fallback_session

    # ------------------------------------------------------------- resolve
    def _resolve(self, req: AgentRequest, session: Any) -> list[Ambiguity]:
        ambiguities: list[Ambiguity] = list(req.ambiguity)
        for entity in req.entities:
            if entity.resolved:
                continue
            found = self._lookup(entity.name)
            if len(found) == 1:
                self._adopt(entity, found[0])
            elif len(found) > 1:
                entity.candidates = [e.to_dict() for e in found]
                ambiguities.append(self._ambiguous(entity.name, found))
        target = req.target
        if target is not None and not target.resolved:
            found = self._resolve_target(target, session)
            if len(found) == 1:
                req.target = self._adopt(target, found[0])
            elif len(found) > 1:
                ambiguities.append(self._ambiguous(target.name, found))
            elif _is_reference(target.name):
                ambiguities.append(Ambiguity(
                    kind=AmbiguityKind.ENTITY, field_name="target",
                    mention=target.name,
                    reason="I'm not sure what you're referring to yet"))
            else:
                ambiguities.append(Ambiguity(
                    kind=AmbiguityKind.ENTITY, field_name="target",
                    mention=target.name,
                    reason=f"I couldn't find anything matching {target.name!r}"))
        elif target is None and req.action in _TARGET_REQUIRED:
            ambiguities.append(Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="target",
                mention=(req.raw_text or "")[:60],
                reason="I need to know which task or block you mean"))
        return _dedupe(ambiguities)

    def _adopt(self, ref: EntityRef, found: EntityRef) -> EntityRef:
        ref.type, ref.id, ref.name = found.type, found.id, found.name
        ref.resolved, ref.confidence = True, found.confidence
        ref.source = found.source or "live_state"
        return ref

    def _ambiguous(self, mention: str, found: list[EntityRef]) -> Ambiguity:
        return Ambiguity(
            kind=AmbiguityKind.ENTITY, field_name="target", mention=mention,
            candidates=[e.to_dict() for e in found],
            reason=f"which one — {', '.join(e.name for e in found[:4])}?")

    def _resolve_target(self, target: EntityRef, session: Any) -> list[EntityRef]:
        name = (target.name or "").strip().lower()
        if _is_reference(name):
            recent = [e for e in session.recent_entities
                      if e.type in (EntityType.TASK, EntityType.COURSE,
                                    EntityType.BLOCK)]
            if "other" in name:
                focus = session.focus_entity
                pool = [e for e in recent
                        if focus is None or e.key() != focus.key()]
                return _unique(_filter_by_noun(name, pool))
            if session.focus_entity is not None:
                return [session.focus_entity]
            return _unique(recent)
        return self._lookup(name)

    def _lookup(self, name: str) -> list[EntityRef]:
        query = (name or "").strip().lower()
        if not query:
            return []
        tokens = [t for t in re.split(r"\W+", query) if len(t) > 2]
        out: list[EntityRef] = []
        for entity in self._live_entities():
            title = entity.name.lower()
            hit = query in title or title in query
            if not hit and tokens:
                hit = all(t in title for t in tokens)
            if hit:
                out.append(entity)
        return out[:6]

    def _live_entities(self) -> list[EntityRef]:
        out: list[EntityRef] = []
        db = getattr(self.container, "db", None)
        if db is not None and hasattr(db, "tasks"):
            try:
                for row in db.tasks("active"):
                    out.append(EntityRef(
                        type=EntityType.TASK, id=str(row["id"]),
                        name=str(row["title"] or ""), resolved=True,
                        confidence=0.9, source="task_table"))
            except Exception:  # noqa: BLE001
                pass
        if db is not None and hasattr(db, "courses"):
            try:
                for row in db.courses():
                    out.append(EntityRef(
                        type=EntityType.COURSE, id=str(row["id"]),
                        name=str(row["code"] or ""), resolved=True,
                        confidence=0.95, source="course_table"))
            except Exception:  # noqa: BLE001
                pass
        return out

    # ------------------------------------------------------------- dispatch
    def _dispatch(self, req: AgentRequest, snap: Any) -> AgentResult:
        handlers: dict[ActionKind, Callable[[AgentRequest, Any], AgentResult]] = {
            ActionKind.RECOMMEND: self._advise,
            ActionKind.PLAN_DAY: self._plan,
            ActionKind.PLAN_WEEK: self._plan,
            ActionKind.FEASIBILITY: self._feasibility,
            ActionKind.URGENCY: self._urgency,
            ActionKind.STATUS: self._status,
        }
        if req.action in handlers:
            return handlers[req.action](req, snap)
        if req.action in _TARGET_REQUIRED:
            return self._gated_mutation(req, snap)
        if req.intent == RequestIntent.CHAT:
            return AgentResult(
                status=ResultStatus.OK,
                facts=[{"kind": "chat", "actionable": False}],
                assumptions=["conversational request; no domain action taken"])
        return AgentResult(
            status=ResultStatus.UNAVAILABLE,
            missing_information=[f"action:{req.action.value}"],
            warnings=["that request has no deterministic executive action yet"])

    # ------------------------------------------------------------- handlers
    def _advise(self, req: AgentRequest, snap: Any) -> AgentResult:
        planner = getattr(self.container, "planner", None)
        if planner is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="planner unavailable")
        day_ts = self._scope_day(req)
        now_min = self._advice_now_min(req)
        r = planner.what_now(message=req.raw_text, day_ts=day_ts, now_min=now_min)
        result = AgentResult(
            status=ResultStatus.OK,
            data={"what_now": r, "available_windows": snap.available_windows},
            facts=[{"context_influenced": r.get("context_influenced", False),
                    "user_override": r.get("user_override", False),
                    "ha_influence": r.get("ha_influence", False),
                    "routine_influenced": r.get("routine_influenced", False),
                    "free_minutes": sum(int(w.get("minutes", 0))
                                        for w in snap.available_windows),
                    "hard_commitments": len(snap.commitments)}],
            assumptions=_scope_assumptions(req),
        )
        cand = r.get("candidate")
        if not cand:
            result.warnings.append("no schedulable task in this window")
            return result
        start_min = _hm_to_min(str(cand.get("start", "")))
        end_min = _hm_to_min(str(cand.get("end", "")))
        target = EntityRef(type=EntityType.TASK, id=str(cand.get("task_id", "")),
                           name=str(cand.get("title", "")), resolved=True,
                           confidence=0.9, source="planner")
        result.recommendations.append(Recommendation(
            action=ActionKind.RECOMMEND, target=target,
            title=str(cand.get("title", "")), reason=str(r.get("reason", "")),
            score=1.0, window=[start_min, end_min],
            window_ts=[day_ts + start_min * 60, day_ts + end_min * 60],
            provenance="planner.what_now"))
        if snap.truncated:
            result.warnings.append("context was truncated to stay bounded")
        return result

    def _feasibility(self, req: AgentRequest, snap: Any) -> AgentResult:
        available = sum(int(w.get("minutes", 0))
                        for w in snap.available_windows)
        requested = _requested_minutes(req.raw_text)
        task_minutes = sum(int(t.get("est_minutes", 0) or 0)
                           for t in snap.tasks)
        required = requested if requested is not None else task_minutes
        surplus = available - required
        result = AgentResult(
            status=ResultStatus.OK,
            data={"available_windows": snap.available_windows,
                  "scope": req.scope.to_dict()},
            facts=[{"available_minutes": available,
                    "required_minutes": required,
                    "task_minutes": task_minutes,
                    "surplus_minutes": surplus,
                    "requested_minutes": requested,
                    "task_count": len(snap.tasks)}],
            assumptions=_scope_assumptions(req),
        )
        if required > available:
            result.warnings.append(
                f"short by {required - available} minutes in this window")
            result.conflicts.append({
                "kind": "capacity", "deficit_minutes": required - available,
                "available_minutes": available, "required_minutes": required})
            result.candidate_actions = [{
                "kind": "defer", "target": t.get("title"),
                "task_id": t.get("id"),
                "reason": "lowest-deadline-pressure task in the shortfall"}
                for t in _rank_tasks(snap.tasks)[-2:]]
        return result

    def _urgency(self, req: AgentRequest, snap: Any) -> AgentResult:
        ranked = _rank_tasks(snap.tasks)
        result = AgentResult(
            status=ResultStatus.OK,
            facts=[{"task_count": len(snap.tasks),
                    "next_deadline": (ranked[0].get("deadline_iso")
                                      if ranked and ranked[0].get("deadline")
                                      else "")}],
            assumptions=_scope_assumptions(req),
        )
        for task in ranked[:3]:
            result.recommendations.append(Recommendation(
                action=ActionKind.RECOMMEND,
                target=EntityRef(type=EntityType.TASK, id=str(task.get("id")),
                                 name=str(task.get("title", "")), resolved=True,
                                 confidence=0.8, source="task_table"),
                title=str(task.get("title", "")),
                reason=("deadline " + str(task.get("deadline_iso")))
                if task.get("deadline") else "no deadline; priority order",
                score=float(task.get("est_minutes", 0) or 0),
                provenance="schedule.rank"))
        if not ranked:
            result.warnings.append("no active tasks in this scope")
        return result

    def _status(self, req: AgentRequest, snap: Any) -> AgentResult:
        return AgentResult(
            status=ResultStatus.OK,
            data={"snapshot": snap.to_dict()},
            facts=[{"commitments": len(snap.commitments),
                    "available_windows": len(snap.available_windows),
                    "active_tasks": len(snap.tasks),
                    "courses": len(snap.courses),
                    "deadlines": len(snap.deadlines),
                    "has_committed_plan": snap.current_plan is not None,
                    "presence": snap.presence.get("status", "unknown")}],
            assumptions=_scope_assumptions(req))

    def _plan(self, req: AgentRequest, snap: Any) -> AgentResult:
        planner = getattr(self.container, "planner", None)
        if planner is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="planner unavailable")
        day_ts = self._scope_day(req)
        days = 7 if req.action == ActionKind.PLAN_WEEK else 1
        if req.scope.kind in (ScopeKind.THIS_WEEK, ScopeKind.NEXT_WEEK):
            days = 7
        plan = planner.plan_week(day_ts=day_ts, days=days)
        facts = [{"date": d.get("date"), "slots": len(d.get("slots", [])),
                  "free_minutes": d.get("free_minutes")}
                 for d in plan.get("days", [])]
        return AgentResult(
            status=ResultStatus.OK, data={"plan": plan}, facts=facts,
            assumptions=_scope_assumptions(req) +
            ["read-only preview; nothing was committed"])

    def _gated_mutation(self, req: AgentRequest, snap: Any) -> AgentResult:
        target = req.target.to_dict() if req.target else None
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            facts=[{"action": req.action.value, "target": target,
                    "temporal": req.temporal.to_dict(),
                    "hard_constraints": [c.to_dict()
                                         for c in req.hard_constraints()]}],
            candidate_actions=[{
                "action": req.action.value, "target": target,
                "when": req.temporal.to_dict(),
                "requires_confirmation": True}],
            warnings=["this change is prepared but not applied; confirmation "
                      "is required before any state is modified"],
            assumptions=_scope_assumptions(req))

    # -------------------------------------------------------------- helpers
    def _scope_day(self, req: AgentRequest) -> int:
        ts = int(req.temporal.start or self.clock.now_ts())
        return self.clock.midnight(ts)

    def _advice_now_min(self, req: AgentRequest) -> int:
        now_min = self.clock.minute_of_day(self.clock.now_ts())
        if req.scope.kind in (ScopeKind.NOW, ScopeKind.TONIGHT):
            if req.temporal.start:
                return max(now_min,
                           self.clock.minute_of_day(req.temporal.start))
            return now_min
        if req.temporal.all_day or req.scope.kind in (
                ScopeKind.TOMORROW, ScopeKind.THIS_WEEK, ScopeKind.NEXT_WEEK):
            return 0
        return now_min

    def _remember(self, session: Any, req: AgentRequest,
                  res: AgentResult) -> None:
        try:
            session.set_topic(req.action.value)
            focus_set = False
            subject = req.target if req.target is not None and req.target.resolved \
                else (req.entities[0] if req.entities and req.entities[0].resolved
                      else None)
            if subject is not None:
                session.note_entity(subject, focus=True)
                focus_set = True
            for ref in req.entities:
                if ref.resolved and ref is not subject:
                    session.note_entity(ref, focus=False)
            for rec in res.recommendations:
                if rec.target is not None and rec.target.resolved:
                    session.note_entity(rec.target, focus=not focus_set)
                    focus_set = True
            session.note_result({
                "status": res.status.value, "action": req.action.value,
                "titles": [r.title for r in res.recommendations][:5],
                "warnings": res.warnings[:3]})
        except Exception:  # noqa: BLE001 — memory must never break a reply
            log.debug("session update failed", exc_info=True)


# ------------------------------------------------------------------ module
def _is_reference(name: str) -> bool:
    name = (name or "").strip().lower()
    return any(name == w or name.startswith(w) for w in _REFERENCE_WORDS)


def _unique(refs: list[EntityRef]) -> list[EntityRef]:
    out: list[EntityRef] = []
    seen: set[str] = set()
    for r in refs:
        if r.key() not in seen:
            seen.add(r.key())
            out.append(r)
    return out


#: Light noun hints so "the other assignment" can exclude unrelated tasks.
_NOUN_HINTS = {
    "assignment": ("assignment", "essay", "homework", "paper", "problem",
                   "pset", "hw", "report", "lab"),
    "homework": ("assignment", "homework", "paper", "problem", "pset", "hw"),
    "essay": ("essay", "paper", "writing", "draft"),
    "paper": ("paper", "essay", "writing", "draft"),
}


def _filter_by_noun(name: str, refs: list[EntityRef]) -> list[EntityRef]:
    for noun, hints in _NOUN_HINTS.items():
        if noun in name:
            narrowed = [r for r in refs
                        if any(h in r.name.lower() for h in hints)]
            if narrowed:
                return narrowed
    return refs


def _dedupe(items: list[Ambiguity]) -> list[Ambiguity]:
    out: list[Ambiguity] = []
    seen: set[tuple[str, str]] = set()
    for a in items:
        key = (a.kind.value, a.field_name or a.mention)
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def _rank_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(t: dict[str, Any]) -> tuple[int, int, int]:
        deadline = int(t.get("deadline", 0) or 0)
        return (0 if deadline else 1, deadline,
                -int(t.get("est_minutes", 0) or 0))
    return sorted(tasks, key=key)


def _hm_to_min(value: str) -> int:
    try:
        hour, minute = value.split(":")
        return int(hour) * 60 + int(minute)
    except (ValueError, AttributeError):
        return 0


def _requested_minutes(text: str) -> int | None:
    low = (text or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*(hour|hr|h|minute|min|m)s?\b", low)
    if m:
        n = float(m.group(1))
        return int(n * 60) if m.group(2) in ("hour", "hr", "h") else int(n)
    m = re.search(r"\b(" + "|".join(_WORD_NUMBERS) + r")\s+"
                  r"(hour|hr|minute|min)s?\b", low)
    if m:
        n = _WORD_NUMBERS[m.group(1)]
        return n * 60 if m.group(2) in ("hour", "hr") else n
    return None


def _scope_assumptions(req: AgentRequest) -> list[str]:
    out = [f"scope={req.scope.kind.value}"]
    if req.temporal.phrase:
        out.append(f"time={req.temporal.phrase!r} "
                   f"({req.temporal.resolution.value})")
    return out
