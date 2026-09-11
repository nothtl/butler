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
from .interactions import (
    InteractionKind, InteractionStatus, InteractionStore, match_candidates,
    ref_from_candidate,
)
from .clarification import (
    ClarificationRequest, apply_slot, build_clarification, explain,
    missing_slots, parse_answer,
)
from .actions import slot_spec
from .temporal import Clock, TemporalResolver
from ..ux import new_request_id

log = logging.getLogger("butler.agent.service")

from .actions import SlotSpec as _SlotSpec  # noqa: E402
_TARGET_SLOT_SPEC = _SlotSpec("target", "target", True, "Which one do you mean?", dynamic="candidates")

_REFERENCE_WORDS = (
    "that", "it", "this", "that one", "the other", "the other one",
    "the other assignment", "the same one", "that task", "that block",
    "this task", "this block",
)

# Bounded follow-up markers used to detect a modification of a pending proposal.
_MODIFICATION_WORDS = (
    "only", "just", "instead", "actually", "change it", "change that",
    "make it", "no,", "no ", "rather", "exclude", "include", "also",
)

# P2.3 deterministic resolution precedence.
RESOLUTION_PRECEDENCE = (
    "clarification", "confirmation", "topic", "recent_entities",
    "conversation", "global_lookup", "ask",
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
        if interpreter is not None:
            self.interpreter = interpreter
        else:
            from .interpret import resolve_interpreter
            self.interpreter = resolve_interpreter(container, now_ts=now_ts)
        self._fallback_session: Any = None
        # P2: explicit, expiring pending-interaction state (clarification +
        # confirmation). Never rely on model memory for follow-ups.
        self.interactions = InteractionStore()
        # When True, memory-mutating actions return a proposal instead of
        # executing (used by the strictly read-only `executive_ask` MCP tool).
        self._read_only = False
        self.request_id = ""

    # --------------------------------------------------------------- public
    def ask(self, *, request: Any = None, text: str = "", user: str = "user",
            include_context: bool = False, read_only: bool = False,
            topic: dict[str, Any] | None = None) -> AgentResult:
        try:
            if request is not None:
                req = request if isinstance(request, AgentRequest) \
                    else AgentRequest.from_dict(request)
                req.validate()
            else:
                req = self._interpret(text, user=user, topic=topic)
                if req is None:
                    return AgentResult.invalid("could not interpret request")
                req.validate()
        except SemanticValidationError as exc:
            return AgentResult.invalid(str(exc))
        if topic:
            req.topic = dict(topic)
        return self.handle(req, user=user, include_context=include_context,
                           read_only=read_only)

    def _interpret(self, text: str, *, user: str = "user",
                   topic: dict[str, Any] | None = None) -> AgentRequest | None:
        """Interpret text, passing topic/context when the interpreter supports it."""
        import time as _time
        started = _time.perf_counter()
        try:
            req = self.interpreter.interpret(text, user=user, topic=topic)
        except TypeError:
            # Custom interpreter with the older signature.
            req = self.interpreter.interpret(text, user=user)
        latency_ms = int((_time.perf_counter() - started) * 1000)
        source = getattr(self.interpreter, "last_source", None) or \
            (req.source if req is not None else "unknown")
        if req is not None:
            log.info(
                "semantic source=%s action=%s confidence=%.2f latency_ms=%d",
                source, req.action.value, req.confidence, latency_ms)
        return req

    def handle(self, req: AgentRequest, *, user: str = "user",
               include_context: bool = False, read_only: bool = False) -> AgentResult:
        try:
            req.validate()
        except SemanticValidationError as exc:
            return AgentResult.invalid(str(exc))
        self._read_only = bool(read_only)
        self.request_id = new_request_id()
        session = self.session(user)
        # P2.4: purge stale state; never apply an expired interaction.
        expired = self.interactions.purge_expired(session)
        expired_notice = bool(expired) and (
            _is_reference(req.raw_text)
            or len((req.raw_text or "").split()) <= 6
            or req.action in (ActionKind.UNKNOWN, ActionKind.RESOLVE_REFERENCE))
        # Q1/P2.1: answer an active clarification first, using structured state.
        clar = self.interactions.active(session, InteractionKind.CLARIFICATION)
        if clar is not None:
            handled = self._answer_clarification(
                session, clar, req, include_context=include_context,
                expired_notice=expired_notice)
            if handled is not None:
                return handled
        # P2.2: a follow-up may modify the pending proposal instead of creating
        # a new, unrelated request.
        conf = self.interactions.active(session, InteractionKind.CONFIRMATION)
        if conf is not None and self._is_modification(req):
            merged = self._merge_pending(conf, req)
            self.interactions.close(session, conf, InteractionStatus.CANCELLED)
            res = AgentResult(
                status=ResultStatus.NEEDS_CONFIRMATION,
                answer=merged.get("summary", ""),
                data=merged, confirmation_required=True,
                assumptions=["updated the pending proposal"])
            return self._finish_result(res, req, session, include_context,
                                       expired_notice)
        return self._dispatch_or_clarify(
            req, session, include_context=include_context,
            expired_notice=expired_notice)

    # --------------------------------------------------- clarification flow
    def _answer_clarification(self, session: Any, clar: Any, req: AgentRequest,
                              *, include_context: bool,
                              expired_notice: bool) -> AgentResult | None:
        cdict = (clar.proposal or {}).get("clarification")
        creq = ClarificationRequest.from_dict(cdict) if cdict else None
        parsed = None
        if creq is not None and creq.slot_name:
            parsed = parse_answer(req.raw_text, creq)
        # State-first: always try the stored candidate set before anything else,
        # including when the model re-classified the follow-up as a new request.
        if parsed is None and clar.candidates:
            cand = match_candidates(req.raw_text, clar.candidates)
            if cand is not None:
                parsed = (cand, "")
        # A free-text follow-up to a target clarification may carry the target
        # itself (e.g. "the CS188 ones" -> target "CS188"); adopt it onto the
        # pending request rather than restarting interpretation.
        if parsed is None and creq is not None and creq.slot_name == "target" \
                and req.target is not None and req.target.name:
            parsed = ({"type": req.target.type.value,
                       "name": req.target.name}, "")
        if parsed is None:
            # A new, clearly-recognised request supersedes a stale clarification.
            if req.action not in (ActionKind.UNKNOWN,) \
                    and req.confidence >= 0.5:
                self.interactions.close(session, clar,
                                        InteractionStatus.CANCELLED)
                return None
            # Not understood: keep the interaction and re-ask (no state lost).
            if creq is not None and creq.options:
                res = AgentResult(
                    status=ResultStatus.AMBIGUOUS,
                    data={"clarification": creq.to_dict()},
                    warnings=["That doesn't match the available choices. "
                              "Pick one below or type your own."])
                return self._finish_result(res, req, session, include_context,
                                           expired_notice)
            return None
        value, option_id = parsed
        self.interactions.close(session, clar, InteractionStatus.COMPLETED)
        if option_id == "__cancel":
            res = AgentResult(status=ResultStatus.OK,
                              answer="Cancelled — nothing was changed.")
            return self._finish_result(res, req, session, include_context,
                                       expired_notice)
        base = AgentRequest.from_dict(clar.request) if clar.request else req
        base.raw_text = req.raw_text
        slot = creq.slot_name if (creq is not None and creq.slot_name) else "target"
        apply_slot(base, slot, value)
        if slot == "target":
            base.entities = []
            base.ambiguity = []
        return self._dispatch_or_clarify(
            base, session, include_context=include_context,
            expired_notice=expired_notice)

    def _dispatch_or_clarify(self, req: AgentRequest, session: Any, *,
                             include_context: bool = False,
                             expired_notice: bool = False) -> AgentResult:
        ambiguities = self._resolve(req, session)
        if ambiguities:
            candidates = []
            for a in ambiguities:
                candidates.extend(a.candidates)
            # Only ask "which one?" when we have concrete, server-resolved
            # candidates. A model-emitted ambiguity without candidates is not a
            # real ambiguity, but a server-detected reference ("this"/"that")
            # still needs an answer.
            needs_answer = bool(candidates) or any(
                a.source == "server" for a in ambiguities)
            if needs_answer:
                req.ambiguity = ambiguities
                creq = self._open_target_clarification(session, req, candidates)
                res = AgentResult.ambiguous(ambiguities)
                res.data = {"clarification": creq.to_dict()}
                return self._finish_result(res, req, session, include_context,
                                           expired_notice)
        # Q1/P1.1: missing required slots (semantic path). The deterministic
        # fallback keeps its own bounded handling.
        if req.source == "llm":
            missing = missing_slots(req)
            if missing:
                res = self._open_slot_clarification(session, req, missing[0])
                return self._finish_result(res, req, session, include_context,
                                           expired_notice)
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
                                deterministic=True,
                                request_id=getattr(self, "request_id", ""))
        data = res.data if isinstance(res.data, dict) else {}
        proposal = dict(data.get("proposal") or {})
        resol = proposal.get("resolution") or {}
        # Only a genuine *target ambiguity* becomes a clarification. Proposal
        # "questions" (missing details) stay part of the confirmation proposal.
        has_target_ambiguity = (
            (res.status == ResultStatus.AMBIGUOUS
             and bool(resol.get("candidates")))
            or str(resol.get("status") or "") == "ambiguous")
        if has_target_ambiguity:
            creq = self._open_target_clarification(
                session, req, list(resol.get("candidates") or []))
            res.data = {"clarification": creq.to_dict()}
            res.missing_information = [creq.slot_name]
            return self._finish_result(res, req, session, include_context,
                                       expired_notice)
        pending = (res.status == ResultStatus.NEEDS_CONFIRMATION
                   or bool(data.get("tracker")) or bool(proposal))
        if pending and (data.get("summary") or proposal or data.get("tracker")
                        or res.answer):
            proposal.setdefault("summary", data.get("summary") or res.answer)
            proposal.setdefault(
                "name", req.target.name if req.target is not None else "")
            proposal["parameters"] = dict(req.parameters or {})
            proposal.setdefault("action", req.action.value)
            if data.get("tracker"):
                proposal.setdefault("tracker", data.get("tracker"))
            self.interactions.open(
                session, kind=InteractionKind.CONFIRMATION,
                original_text=req.raw_text, request=req.to_dict(),
                proposal=proposal)
        return self._finish_result(res, req, session, include_context,
                                   expired_notice)

    def _open_target_clarification(self, session: Any, req: AgentRequest,
                                   candidates: list[dict[str, Any]]
                                   ) -> ClarificationRequest:
        spec = slot_spec(req.action.value, "target") or \
            _TARGET_SLOT_SPEC
        enriched = self._enrich_candidates(candidates)
        reason = (f"I found {len(enriched)} matches and need to know which "
                  f"one you mean." if len(enriched) > 1 else "")
        return self._store_clarification(session, req, spec, enriched, reason)

    def _open_slot_clarification(self, session: Any, req: AgentRequest,
                                 spec: Any) -> AgentResult:
        candidates: list[dict[str, Any]] = []
        if getattr(spec, "dynamic", "") == "candidates":
            candidates = self._enrich_candidates(
                [e.to_dict() for e in self._live_entities()][:6])
        creq = self._store_clarification(session, req, spec, candidates, "")
        return AgentResult(
            status=ResultStatus.AMBIGUOUS,
            data={"clarification": creq.to_dict()},
            missing_information=[spec.name], warnings=[explain(creq)])

    def _store_clarification(self, session: Any, req: AgentRequest, spec: Any,
                             candidates: list[dict[str, Any]],
                             reason: str) -> ClarificationRequest:
        creq = build_clarification("", spec, candidates=candidates,
                                   topic_scope=req.topic, reason=reason)
        it = self.interactions.open(
            session, kind=InteractionKind.CLARIFICATION,
            original_text=req.raw_text, request=req.to_dict(),
            candidates=candidates, expected_slot=spec.name,
            ambiguity_type=("entity" if spec.kind == "target" else spec.kind),
            proposal={"clarification": creq.to_dict()})
        creq.interaction_id = it.id
        it.proposal["clarification"] = creq.to_dict()
        return creq

    def resume_with_slot(self, interaction_id: str, slot: str, value: Any, *,
                         user: str = "user") -> AgentResult:
        """Apply a button answer directly (no re-interpretation), then resume."""
        session = self.session(user)
        clar = None
        for it in self.interactions._all(session):
            if it.id == interaction_id and it.is_open() \
                    and it.kind == InteractionKind.CLARIFICATION:
                clar = it
                break
        if clar is None:
            return AgentResult(status=ResultStatus.INVALID,
                               error="That choice has expired. Please choose again.")
        if clar.expected_slot and slot and clar.expected_slot != slot:
            return AgentResult(status=ResultStatus.INVALID,
                               error="That doesn't match the available choices.")
        base = AgentRequest.from_dict(clar.request) if clar.request else             AgentRequest()
        apply_slot(base, slot or clar.expected_slot, value)
        self.interactions.close(session, clar, InteractionStatus.COMPLETED)
        return self._dispatch_or_clarify(base, session)

    def _finish_result(self, res: AgentResult, req: AgentRequest, session: Any,
                       include_context: bool, expired_notice: bool) -> AgentResult:
        if expired_notice:
            res.warnings = list(res.warnings) + [
                "That earlier choice has expired. Please choose again."]
        if include_context and res.context is None:
            res.context = self.context.build_snapshot(req)
        self._remember(session, req, res)
        return res

    # ------------------------------------------------- pending interactions
    @staticmethod
    def _is_modification(req: AgentRequest) -> bool:
        if req.action in (ActionKind.UPDATE, ActionKind.UPDATE_ITEM,
                          ActionKind.SETTINGS_UPDATE):
            return True
        low = (req.raw_text or "").lower()
        return any(w in low for w in _MODIFICATION_WORDS)

    def _merge_pending(self, conf: Any, req: AgentRequest) -> dict[str, Any]:
        proposal = dict(conf.proposal or {})
        params = dict(proposal.get("parameters") or {})
        params.update(req.parameters or {})
        proposal["parameters"] = params
        if req.temporal is not None and req.temporal.phrase:
            proposal["temporal"] = req.temporal.to_dict()
        proposal["modified_by"] = req.raw_text
        proposal["summary"] = self._proposal_summary(proposal)
        conf.proposal = proposal
        return proposal

    @staticmethod
    def _proposal_summary(proposal: dict[str, Any]) -> str:
        name = proposal.get("name") or "the request"
        parts = [f"Okay — I'll update {name}."]
        params = proposal.get("parameters") or {}
        if params:
            parts.append("Settings: " + ", ".join(
                f"{k}={v}" for k, v in params.items()) + ".")
        parts.append("Confirm?")
        return " ".join(parts)

    def _open_clarification(self, session: Any, req: AgentRequest,
                            ambiguities: list[Ambiguity]) -> None:
        candidates: list[dict[str, Any]] = []
        for a in ambiguities:
            candidates.extend(a.candidates)
        self.interactions.open(
            session, kind=InteractionKind.CLARIFICATION,
            original_text=req.raw_text, request=req.to_dict(),
            candidates=self._enrich_candidates(candidates),
            ambiguity_type=ambiguities[0].kind.value if ambiguities else "")

    def _enrich_candidates(self, candidates: list[dict[str, Any]]
                           ) -> list[dict[str, Any]]:
        """Add distinguishing qualifiers so a follow-up can pick one."""
        out: list[dict[str, Any]] = []
        for raw in candidates:
            c = dict(raw)
            qualifiers: list[str] = []
            try:
                if str(c.get("type")) == "project":
                    pmod = getattr(self.container, "projects", None)
                    db = getattr(self.container, "db", None)
                    got = pmod.get_project(int(c.get("id") or 0)) \
                        if pmod is not None else None
                    cid = (got or {}).get("course_id") if got else None
                    if cid and db is not None:
                        row = db.course_by_id(int(cid))
                        if row is not None:
                            qualifiers.append(str(row["code"]))
            except Exception:  # noqa: BLE001 — qualifiers are best effort
                pass
            if qualifiers:
                c["qualifiers"] = qualifiers
                c["label"] = f"{c.get('name', '')} ({' '.join(qualifiers)})"
            out.append(c)
        return out

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
                    mention=target.name, source="server",
                    reason="I'm not sure what you're referring to yet"))
            else:
                ambiguities.append(Ambiguity(
                    kind=AmbiguityKind.ENTITY, field_name="target",
                    mention=target.name, source="server",
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
            source="server", candidates=[e.to_dict() for e in found],
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
        pmod = getattr(self.container, "projects", None)
        if pmod is not None and hasattr(pmod, "list_projects"):
            try:
                for row in pmod.list_projects():
                    out.append(EntityRef(
                        type=EntityType.PROJECT, id=str(row["id"]),
                        name=str(row["name"] or ""), resolved=True,
                        confidence=0.9, source="project_table"))
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
            ActionKind.PROJECT_STATUS: self._project_status,
            ActionKind.PROJECT_WORKLOAD: self._project_workload,
            ActionKind.PROJECT_RISK: self._project_risk,
            ActionKind.PROJECT_DEPENDENCIES: self._project_dependencies,
            ActionKind.PROJECT_NEXT: self._project_next,
            ActionKind.CREATE_PROJECT: self._project_proposal,
            ActionKind.OPTIMIZE_DAY: self._optimize,
            ActionKind.OPTIMIZE_WEEK: self._optimize,
            ActionKind.EVALUATE_SCHEDULE: self._evaluate_schedule,
            ActionKind.FIND_BEST_SLOT: self._find_best_slot,
            ActionKind.RESCHEDULE_OPTIMIZED: self._reschedule_optimized,
            ActionKind.MEMORY_QUERY: self._memory_query,
            ActionKind.MEMORY_SEARCH: self._memory_search,
            ActionKind.MEMORY_EXPLAIN: self._memory_explain,
            ActionKind.MEMORY_LEARN: self._memory_learn,
            ActionKind.MEMORY_FORGET: self._memory_forget,
            ActionKind.MEMORY_CONFIRM: self._memory_confirm,
            ActionKind.MEMORY_CORRECT: self._memory_correct,
            ActionKind.PROACTIVE_QUERY: self._proactive_query,
            ActionKind.PROACTIVE_LIST: self._proactive_list,
            ActionKind.PROACTIVE_EXPLAIN: self._proactive_explain,
            ActionKind.PROACTIVE_SNOOZE: self._proactive_snooze,
            ActionKind.PROACTIVE_SUPPRESS: self._proactive_suppress,
            ActionKind.TRACKER_CREATE: self._tracker_create,
            ActionKind.TRACKER_LIST: self._tracker_list,
            ActionKind.TRACKER_QUERY: self._tracker_query,
            ActionKind.TRACKER_CONTROL: self._tracker_control,
            ActionKind.TRACKER_EVALUATE: self._tracker_evaluate,
            ActionKind.TRACKER_EXPLAIN: self._tracker_explain,
            ActionKind.CREATE_ITEM: self._creation_create,
            ActionKind.PREVIEW_CREATION: self._creation_preview,
            ActionKind.RESOLVE_REFERENCE: self._creation_resolve,
            ActionKind.LINK_ITEMS: self._creation_link,
            ActionKind.UPDATE_ITEM: self._creation_update,
            ActionKind.ORGANIZE_ITEMS: self._creation_organize,
            ActionKind.SETTINGS_VIEW: self._settings_view,
            ActionKind.SETTINGS_UPDATE: self._settings_update,
            ActionKind.WEB_SEARCH: self._web_search,
            ActionKind.WEB_RESEARCH: self._web_research,
            ActionKind.WEB_FETCH: self._web_fetch,
            ActionKind.KNOWLEDGE_LOOKUP: self._knowledge_lookup,
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

    def _topic_state_text(self, subject: str, req: AgentRequest) -> str:
        """Q6: a contextual answer built from live topic state (no phrase lists)."""
        from ..topics import CAPABILITIES, CAP_LABELS
        topics = getattr(self.container, "topics", None)
        t = req.topic or {}
        prof = None
        if topics is not None and t.get("chat_id") is not None:
            try:
                prof = topics.get(int(t["chat_id"]), int(t.get("thread_id") or 0))
            except Exception:  # noqa: BLE001
                prof = None
        subject = str(subject or "").lower()
        if prof is None:
            if subject in ("tracking", "activity"):
                return "I'm not currently monitoring anything."
            return "I'm not in a configured topic right now."
        name = prof.name or "this topic"
        if subject == "connections":
            links = topics.links(prof) if topics is not None else []
            if not links:
                return f"{name} isn't connected to anything yet."
            return f"{name} is connected to:\n" + "\n".join(
                f"• {l['target_type']}" for l in links)
        if subject == "configuration":
            enabled = [CAP_LABELS[c] for c in CAPABILITIES
                       if prof.cap(c) == "enabled"]
            return (f"Enabled in {name}: "
                    + (", ".join(enabled) if enabled else "nothing") + ".")
        if subject in ("tracking", "activity"):
            lines = topics.tracking_lines(prof) if topics is not None else []
            if not lines:
                return f"I'm not currently monitoring anything in {name}."
            return f"I'm monitoring in {name}:\n" + "\n".join(
                f"• {x}" for x in lines)
        # capabilities
        enabled = [CAP_LABELS[c] for c in CAPABILITIES
                   if prof.cap(c) == "enabled"]
        out = [f"In {name} I can help with:"]
        out += [f"• {x}" for x in enabled] or ["• (nothing enabled yet)"]
        lines = topics.tracking_lines(prof) if topics is not None else []
        if lines:
            out += ["", "Currently tracking:"]
            out += [f"• {x}" for x in lines]
        return "\n".join(out)

    def _status(self, req: AgentRequest, snap: Any) -> AgentResult:
        subject = (req.parameters or {}).get("query_subject")
        if subject:
            return AgentResult(
                status=ResultStatus.OK,
                data={"text": self._topic_state_text(str(subject), req),
                      "query_subject": str(subject)},
                facts=[{"kind": "state_query", "query_subject": str(subject)}])
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

    # ------------------------------------------------------ project handlers
    def _projects_module(self) -> Any:
        return getattr(self.container, "projects", None)

    def _project_ident(self, req: AgentRequest) -> Any:
        if req.target is not None and req.target.resolved:
            return req.target.id or req.target.name
        if req.target is not None and req.target.name:
            return req.target.name
        return None

    def _project_unavailable(self) -> AgentResult:
        return AgentResult(status=ResultStatus.UNAVAILABLE,
                           error="project intelligence unavailable")

    def _project_status(self, req: AgentRequest, snap: Any) -> AgentResult:
        pmod = self._projects_module()
        if pmod is None:
            return self._project_unavailable()
        ident = self._project_ident(req)
        if ident is not None:
            project = pmod.get_project(ident)
            if project is None:
                return AgentResult.ambiguous([Ambiguity(
                    kind=AmbiguityKind.ENTITY, field_name="target",
                    mention=str(ident),
                    reason="I couldn't find that project")])
            return AgentResult(
                status=ResultStatus.OK,
                data={"project": project},
                facts=[{"project_id": project["id"],
                        "name": project["name"],
                        "status": project["status"],
                        "progress": project["progress"],
                        "progress_source": project["progress_source"],
                        "remaining_minutes": project["remaining_minutes"],
                        "risk_level": project["risk_level"],
                        "milestones": len(project["milestones"])}])
        projects = pmod.list_projects()
        return AgentResult(
            status=ResultStatus.OK,
            data={"projects": projects, "count": len(projects)},
            facts=[{"project_count": len(projects),
                    "active": sum(1 for p in projects
                                  if p["status"] == "active")}])

    def _project_workload(self, req: AgentRequest, snap: Any) -> AgentResult:
        pmod = self._projects_module()
        if pmod is None:
            return self._project_unavailable()
        ident = self._project_ident(req)
        if ident is not None:
            wl = pmod.workload(ident)
            if wl is None:
                return AgentResult.ambiguous([Ambiguity(
                    kind=AmbiguityKind.ENTITY, field_name="target",
                    mention=str(ident),
                    reason="I couldn't find that project")])
            return AgentResult(
                status=ResultStatus.OK,
                data={"workload": wl},
                facts=[{"project_id": wl["project_id"], "name": wl["name"],
                        "remaining_minutes": wl["remaining_minutes"],
                        "estimated_minutes": wl["estimated_minutes"],
                        "progress": wl["progress"],
                        "progress_source": wl["progress_source"],
                        "feasible": wl["feasible"],
                        "available_minutes_until_deadline":
                            wl["available_minutes_until_deadline"],
                        "blocked_tasks": len(wl["blocked_task_ids"])}])
        workloads = [pmod.workload(p["id"]) for p in pmod.list_projects("active")]
        workloads = [w for w in workloads if w]
        total_remaining = sum(int(w["remaining_minutes"]) for w in workloads)
        return AgentResult(
            status=ResultStatus.OK,
            data={"workloads": workloads},
            facts=[{"project_count": len(workloads),
                    "total_remaining_minutes": total_remaining,
                    "total_estimated_minutes":
                        sum(int(w["estimated_minutes"]) for w in workloads)}])

    def _project_risk(self, req: AgentRequest, snap: Any) -> AgentResult:
        pmod = self._projects_module()
        if pmod is None:
            return self._project_unavailable()
        ident = self._project_ident(req)
        if ident is not None:
            r = pmod.risk(ident)
            if r is None:
                return AgentResult.ambiguous([Ambiguity(
                    kind=AmbiguityKind.ENTITY, field_name="target",
                    mention=str(ident),
                    reason="I couldn't find that project")])
            return AgentResult(
                status=ResultStatus.OK, data={"risk": r},
                facts=[{"project_id": r["project_id"], "name": r["name"],
                        "score": r["score"], "level": r["level"],
                        "top_factor": max(r["factors"],
                                          key=lambda f: float(f["weight"]) *
                                          (f["value"] or 0))["name"]}])
        worst = pmod.most_at_risk()
        if worst is None:
            return AgentResult(status=ResultStatus.OK, data={"risk": None},
                               warnings=["no active projects to assess"])
        return AgentResult(
            status=ResultStatus.OK, data={"risk": worst, "most_at_risk": True},
            facts=[{"project_id": worst["project_id"], "name": worst["name"],
                    "score": worst["score"], "level": worst["level"]}])

    def _project_dependencies(self, req: AgentRequest, snap: Any) -> AgentResult:
        pmod = self._projects_module()
        if pmod is None:
            return self._project_unavailable()
        ident = self._project_ident(req)
        if ident is None:
            return AgentResult.ambiguous([Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="target",
                mention=(req.raw_text or "")[:60],
                reason="which project should I check dependencies for?")])
        deps = pmod.dependencies(ident)
        if deps is None:
            return AgentResult.ambiguous([Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="target",
                mention=str(ident),
                reason="I couldn't find that project")])
        return AgentResult(
            status=ResultStatus.OK, data={"dependencies": deps},
            facts=[{"project_id": deps["project_id"], "name": deps["name"],
                    "edges": len(deps["edges"]),
                    "blocked": len(deps["blocked"]),
                    "cycles": len(deps["cycles"])}],
            warnings=(["dependency cycle detected"]
                      if deps["cycles"] else []))

    def _project_next(self, req: AgentRequest, snap: Any) -> AgentResult:
        pmod = self._projects_module()
        if pmod is None:
            return self._project_unavailable()
        ident = self._project_ident(req)
        if ident is None:
            return self._advise(req, snap)
        wl = pmod.workload(ident)
        if wl is None:
            return AgentResult.ambiguous([Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="target",
                mention=str(ident),
                reason="I couldn't find that project")])
        result = AgentResult(
            status=ResultStatus.OK,
            data={"workload": wl, "next_tasks": wl["next_tasks"]},
            facts=[{"project_id": wl["project_id"], "name": wl["name"],
                    "remaining_minutes": wl["remaining_minutes"],
                    "blocked_tasks": len(wl["blocked_task_ids"])}],
            assumptions=["project-scoped advisory ranking; the scheduler "
                         "remains the authority for feasibility"])
        for t in wl["next_tasks"][:1]:
            result.recommendations.append(Recommendation(
                action=ActionKind.RECOMMEND,
                target=EntityRef(type=EntityType.TASK, id=str(t["task_id"]),
                                 name=str(t["title"]), resolved=True,
                                 confidence=0.8, source="project_table"),
                title=str(t["title"]),
                reason=("blocked by an unfinished dependency"
                        if t["blocked"] else
                        "highest-priority unblocked task in this project"),
                score=float(t["priority"]),
                provenance="projects.candidates"))
        if not wl["next_tasks"]:
            result.warnings.append("no active tasks linked to this project")
        return result

    def _project_proposal(self, req: AgentRequest, snap: Any) -> AgentResult:
        pmod = self._projects_module()
        if pmod is None:
            return self._project_unavailable()
        proposal = pmod.propose_project(req.raw_text or "")
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            data={"proposal": proposal},
            facts=[{"kind": "project_proposal",
                    "name": proposal["name"],
                    "deadline": proposal["deadline"],
                    "estimated_total_minutes":
                        proposal["estimated_total_minutes"],
                    "milestones": len(proposal["milestones"]),
                    "confidence": proposal["confidence"]}],
            candidate_actions=[{"action": "create_project",
                                "proposal": proposal,
                                "requires_confirmation": True}],
            warnings=(["I need a few details before creating this project: "
                       + "; ".join(proposal["questions"])]
                      if proposal["questions"] else
                      ["this project is prepared from an inferred description; "
                       "confirmation is required before it is saved"]),
            assumptions=["all proposal fields are marked inferred provenance"])

    # ------------------------------------------------------ memory handlers
    def _memory_module(self) -> Any:
        return getattr(self.container, "memory", None)

    def _memory_unavailable(self) -> AgentResult:
        return AgentResult(status=ResultStatus.UNAVAILABLE,
                           error="memory unavailable")

    def _memory_ident(self, req: AgentRequest) -> Any:
        if req.target is not None and req.target.resolved:
            return req.target.id or req.target.name
        if req.target is not None and req.target.name:
            return req.target.name
        return _clean_memory_query(req.raw_text)

    def _memory_gated(self, req: AgentRequest) -> AgentResult:
        """A read-only proposal for a memory mutation (never executes)."""
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            data={"proposed_action": req.action.value,
                  "text": req.raw_text, "committed": False},
            facts=[{"kind": "memory_proposal", "action": req.action.value,
                    "text": req.raw_text}],
            candidate_actions=[{"action": req.action.value,
                                "text": req.raw_text,
                                "requires_confirmation": True}],
            warnings=["this memory change is prepared but not applied; "
                      "confirmation is required"],
            assumptions=["read-only executive surface: memory writes require "
                         "the user's explicit confirmation"])

    def _memory_query(self, req: AgentRequest, snap: Any) -> AgentResult:
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        rows = mem.list(active_only=False, limit=50)
        routines = list(getattr(snap, "routines", []) or [])
        inferred = sum(1 for r in rows
                       if r.get("provenance") in ("routine_inferred",
                                                  "llm_inferred"))
        result = AgentResult(
            status=ResultStatus.OK,
            data={"memories": rows, "routines": routines, "count": len(rows)},
            facts=[{"kind": "memory_query", "count": len(rows),
                    "confirmed": sum(1 for r in rows
                                     if r.get("confirmation_state") == "confirmed"),
                    "inferred": inferred,
                    "routines": len(routines)}],
            assumptions=["memories are Butler-owned and reversible; inferred "
                         "memories are soft and never hard constraints"])
        if inferred:
            result.warnings.append("some memories are inferred, not confirmed")
        return result

    def _memory_search(self, req: AgentRequest, snap: Any) -> AgentResult:
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        q = _clean_memory_query(req.raw_text)
        rows = mem.search(q)
        return AgentResult(
            status=ResultStatus.OK,
            data={"query": q, "memories": rows, "count": len(rows)},
            facts=[{"kind": "memory_search", "query": q, "count": len(rows)}])

    def _memory_explain(self, req: AgentRequest, snap: Any) -> AgentResult:
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        ident = self._memory_ident(req)
        ex = mem.explain(ident)
        if ex is None:
            return AgentResult(
                status=ResultStatus.UNAVAILABLE,
                warnings=["I don't have a memory that explains that"],
                missing_information=["memory"])
        return AgentResult(
            status=ResultStatus.OK, data={"explanation": ex},
            facts=[{"kind": "memory_explain",
                    "id": ex["memory"]["id"],
                    "provenance": ex["memory"]["provenance"],
                    "confirmed": ex["confirmed"], "inferred": ex["inferred"]}],
            warnings=(["this is an inferred pattern, not a rule"]
                      if ex["inferred"] else []))

    def _memory_learn(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._memory_gated(req)
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        res = mem.remember_explicit(req.raw_text or "")
        if not res.get("ok"):
            status = ResultStatus.INVALID if "secret" in str(res.get("reason", "")) \
                else ResultStatus.UNAVAILABLE
            return AgentResult(status=status, data=res,
                               warnings=[str(res.get("reason", ""))])
        stored = res.get("stored") or {}
        return AgentResult(
            status=ResultStatus.OK,
            data={"stored": stored, "action": res.get("action")},
            facts=[{"kind": "memory_learn", "action": res.get("action"),
                    "type": stored.get("type"),
                    "provenance": stored.get("provenance"),
                    "confirmation_state": stored.get("confirmation_state"),
                    "id": stored.get("id")}],
            assumptions=["explicit user statement stored as confirmed memory"])

    def _memory_forget(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._memory_gated(req)
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        res = mem.forget(self._memory_ident(req))
        if res.get("ambiguous"):
            cands = res.get("candidates", [])
            return AgentResult.ambiguous([Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="memory",
                mention=(req.raw_text or "")[:60],
                candidates=cands,
                reason="which memory should I forget? I won't guess")])
        if not res.get("ok"):
            return AgentResult(status=ResultStatus.UNAVAILABLE, data=res,
                               warnings=[str(res.get("error", "no match"))])
        return AgentResult(
            status=ResultStatus.OK, data={"forgotten": res.get("memory")},
            facts=[{"kind": "memory_forget",
                    "id": (res.get("memory") or {}).get("id")}])

    def _memory_confirm(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._memory_gated(req)
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        res = mem.confirm(self._memory_ident(req))
        if not res.get("ok"):
            return AgentResult(status=ResultStatus.UNAVAILABLE, data=res,
                               warnings=[str(res.get("error", "no match"))])
        return AgentResult(
            status=ResultStatus.OK, data={"confirmed": res.get("memory")},
            facts=[{"kind": "memory_confirm",
                    "id": (res.get("memory") or {}).get("id")}])

    def _memory_correct(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._memory_gated(req)
        mem = self._memory_module()
        if mem is None:
            return self._memory_unavailable()
        res = mem.correct(req.raw_text or "")
        if not res.get("ok"):
            status = ResultStatus.INVALID if "secret" in str(res.get("reason", "")) \
                else ResultStatus.UNAVAILABLE
            return AgentResult(status=status, data=res,
                               warnings=[str(res.get("reason", ""))])
        return AgentResult(
            status=ResultStatus.OK,
            data={"stored": res.get("stored"), "action": res.get("action")},
            facts=[{"kind": "memory_correct",
                    "action": res.get("action"),
                    "id": (res.get("stored") or {}).get("id")}])

    # --------------------------------------------------- proactive handlers
    def _proactive_module(self) -> Any:
        return getattr(self.container, "proactive_engine", None)

    def _proactive_unavailable(self) -> AgentResult:
        return AgentResult(status=ResultStatus.UNAVAILABLE,
                           error="proactive engine unavailable")

    def _proactive_cycle(self, req: AgentRequest) -> dict[str, Any]:
        eng = self._proactive_module()
        res = eng.run_cycle(now=self.clock.now_ts(), deliver=False,
                            persist=not self._read_only)
        return res

    def _proactive_match(self, req: AgentRequest) -> dict[str, Any] | None:
        eng = self._proactive_module()
        q = _clean_proactive_query(req.raw_text)
        cands = eng.list_candidates(limit=100)
        if not cands:
            return None
        low = q.lower()
        if low:
            for c in cands:
                hay = f"{c['key']} {c['title']} {c['summary']}".lower()
                if low in hay:
                    return c
            toks = [t for t in re.split(r"\W+", low) if len(t) > 2]
            best, best_hits = None, 0
            for c in cands:
                hay = f"{c['key']} {c['title']} {c['summary']}".lower()
                hits = sum(1 for t in toks if t in hay)
                if hits > best_hits:
                    best, best_hits = c, hits
            if best is not None and best_hits > 0:
                return best
        # fall back to the highest-priority pending candidate
        rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        pending = [c for c in cands if str(c.get("state")) == "pending"]
        pool = pending or cands
        pool.sort(key=lambda c: (rank.get(str(c.get("priority")), 3),
                                 -float(c.get("score") or 0.0)))
        return pool[0]

    def _proactive_query(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._proactive_module()
        if eng is None:
            return self._proactive_unavailable()
        res = self._proactive_cycle(req)
        top = res.get("candidates", [])[:3]
        status = eng.status(now=self.clock.now_ts())
        result = AgentResult(
            status=ResultStatus.OK,
            data={"candidates": res.get("candidates", []),
                  "notified_today": status.get("notified_today"),
                  "quiet_hours": status.get("quiet_hours")},
            facts=[{"kind": "proactive_query",
                    "count": len(res.get("candidates", [])),
                    "top": [c.get("title") for c in top],
                    "notified_today": status.get("notified_today")}],
            warnings=["proactive candidates are read-only recommendations; "
                      "actions require confirmation"])
        for c in top:
            result.recommendations.append(Recommendation(
                action=ActionKind.PROACTIVE_QUERY,
                title=str(c.get("title", "")),
                reason=str(c.get("explanation") or c.get("summary") or ""),
                score=float(c.get("score") or 0.0),
                provenance="proactive_engine"))
        return result

    def _proactive_list(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._proactive_module()
        if eng is None:
            return self._proactive_unavailable()
        res = self._proactive_cycle(req)
        status = eng.status(now=self.clock.now_ts())
        return AgentResult(
            status=ResultStatus.OK,
            data={"candidates": res.get("candidates", []), "status": status,
                  "suppressed": res.get("suppressed", [])},
            facts=[{"kind": "proactive_list",
                    "count": len(res.get("candidates", [])),
                    "suppressed": len(res.get("suppressed", [])),
                    "notified_today": status.get("notified_today")}])

    def _proactive_explain(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._proactive_module()
        if eng is None:
            return self._proactive_unavailable()
        self._proactive_cycle(req)  # refresh candidates deterministically
        cand = self._proactive_match(req)
        if cand is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               warnings=["I have nothing to explain right now"])
        ex = eng.explain(cand["key"])
        return AgentResult(
            status=ResultStatus.OK, data={"explanation": ex},
            facts=[{"kind": "proactive_explain", "key": cand["key"],
                    "priority": cand.get("priority"),
                    "category": cand.get("category")}])

    def _proactive_gated(self, req: AgentRequest) -> AgentResult:
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            data={"proposed_action": req.action.value, "text": req.raw_text,
                  "committed": False},
            facts=[{"kind": "proactive_proposal", "action": req.action.value,
                    "text": req.raw_text}],
            candidate_actions=[{"action": req.action.value,
                                "text": req.raw_text,
                                "requires_confirmation": True}],
            warnings=["this proactive change is prepared but not applied; "
                      "confirmation is required"],
            assumptions=["read-only executive surface: proactive mutations "
                         "require explicit confirmation"])

    def _proactive_snooze(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._proactive_gated(req)
        eng = self._proactive_module()
        if eng is None:
            return self._proactive_unavailable()
        self._proactive_cycle(req)
        cand = self._proactive_match(req)
        if cand is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               warnings=["no proactive candidate to snooze"])
        minutes = _requested_minutes(req.raw_text or "") or 180
        res = eng.snooze(cand["key"], minutes, now=self.clock.now_ts())
        return AgentResult(
            status=ResultStatus.OK,
            data={"snoozed": cand["key"], "until": res["until"],
                  "minutes": res["minutes"]},
            facts=[{"kind": "proactive_snooze", "key": cand["key"],
                    "minutes": res["minutes"]}])

    def _proactive_suppress(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._proactive_gated(req)
        eng = self._proactive_module()
        if eng is None:
            return self._proactive_unavailable()
        self._proactive_cycle(req)
        q = _clean_proactive_query(req.raw_text).lower()
        categories = {"deadline": "deadline_risk", "risk": "project_risk",
                      "routine": "routine", "estimate": "estimate",
                      "travel": "travel", "food": "food", "course": "course",
                      "conflict": "schedule_conflict",
                      "free time": "free_time"}
        target, scope = None, "candidate"
        for word, cat in categories.items():
            if word in q:
                target, scope = cat, "category"
                break
        if target is None:
            cand = self._proactive_match(req)
            if cand is None:
                return AgentResult(
                    status=ResultStatus.UNAVAILABLE,
                    warnings=["I couldn't tell which reminder to stop"])
            target = cand["key"]
        res = eng.suppress(target, now=self.clock.now_ts(), reason="user_stop",
                           scope=scope)
        return AgentResult(
            status=ResultStatus.OK,
            data={"suppressed": res["key"], "scope": res["scope"]},
            facts=[{"kind": "proactive_suppress", "key": res["key"],
                    "scope": res["scope"]}],
            assumptions=["critical safety/deadline warnings are never muted"])

    # --------------------------------------------------- tracker handlers
    def _tracker_module(self) -> Any:
        return getattr(self.container, "trackers", None)

    def _tracker_topic_ctx(self, req: AgentRequest) -> dict[str, Any]:
        ref = dict(req.topic or {})
        ctx: dict[str, Any] = {}
        if ref.get("chat_id") is not None:
            ctx["chat_id"] = int(ref.get("chat_id") or 0)
            ctx["thread_id"] = int(ref.get("thread_id") or 0)
        topics = getattr(self.container, "topics", None)
        if topics is not None and ctx.get("chat_id") is not None:
            try:
                prof = topics.get(ctx["chat_id"], ctx.get("thread_id", 0))
                if prof is not None:
                    ctx["topic_id"] = prof.id
                    ctx.setdefault("topic_name", prof.name)
            except Exception:  # noqa: BLE001
                pass
        return ctx

    def _tracker_gated(self, req: AgentRequest) -> AgentResult:
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            data={"proposed_action": req.action.value, "text": req.raw_text,
                  "committed": False},
            facts=[{"kind": "tracker_proposal", "action": req.action.value,
                    "text": req.raw_text}],
            candidate_actions=[{"action": req.action.value,
                                "text": req.raw_text,
                                "requires_confirmation": True}],
            warnings=["this tracker change is prepared but not applied; "
                      "confirmation is required"],
            assumptions=["read-only executive surface: tracker mutations "
                         "require explicit confirmation"])

    def _tracker_create(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._tracker_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="tracker engine unavailable")
        if self._read_only:
            return self._tracker_gated(req)
        ctx = self._tracker_topic_ctx(req)
        proposal = eng.parse_request(req.raw_text or "", context=ctx)
        tracker = proposal["tracker"]
        if proposal["questions"]:
            return AgentResult(
                status=ResultStatus.AMBIGUOUS,
                data={"proposal": proposal},
                warnings=list(proposal["questions"]),
                missing_information=list(proposal["questions"]),
                candidate_actions=[{"action": "tracker_create",
                                    "proposal": proposal,
                                    "requires_confirmation": False}])
        dest = proposal.get("destination") or {}
        t = eng.create(
            name=tracker["name"], source=tracker["source"],
            target_type=tracker["target_type"], target_id=tracker["target_id"],
            target_ref=tracker["target_ref"], condition=tracker["condition"],
            action=tracker["action"], cadence=tracker["cadence"],
            scope=tracker["scope"], priority=tracker["priority"],
            destination=dest, one_shot=tracker["one_shot"],
            expires_at=tracker["expires_at"])
        return AgentResult(
            status=ResultStatus.OK,
            data={"tracker": t.to_dict(), "summary": proposal["summary"]},
            facts=[{"kind": "tracker_create", "tracker_id": t.id,
                    "source": t.source, "target": t.target_ref,
                    "condition": (t.condition or {}).get("type"),
                    "destination": dest}],
            assumptions=["trackers propose actions; they never execute "
                         "consequential external effects"])

    def _tracker_list(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._tracker_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="tracker engine unavailable")
        ctx = self._tracker_topic_ctx(req)
        rows = (eng.by_destination(ctx["chat_id"], ctx.get("thread_id", 0))
                if ctx.get("chat_id") is not None else eng.list(limit=100))
        return AgentResult(
            status=ResultStatus.OK,
            data={"trackers": [t.to_dict() for t in rows], "count": len(rows),
                  "text": self._topic_state_text("tracking", req)},
            facts=[{"kind": "tracker_list", "count": len(rows),
                    "active": sum(1 for t in rows if t.state == "active")}])

    def _tracker_query(self, req: AgentRequest, snap: Any) -> AgentResult:
        return self._tracker_list(req, snap)

    def _tracker_find(self, req: AgentRequest) -> Any:
        eng = self._tracker_module()
        rows = eng.list(limit=200)
        if req.target is not None and req.target.id:
            try:
                return eng.get(int(req.target.id))
            except (TypeError, ValueError):
                pass
        q = _clean_tracker_query(req.raw_text)
        if q:
            for t in rows:
                if q in (t.name or "").lower() or q in (t.target_ref or "").lower():
                    return t
            for t in rows:
                toks = [x for x in q.split() if len(x) > 2]
                if toks and any(x in (t.name or "").lower()
                                or x in (t.target_ref or "").lower()
                                for x in toks):
                    return t
        return None

    def _tracker_control(self, req: AgentRequest, snap: Any) -> AgentResult:
        if self._read_only:
            return self._tracker_gated(req)
        eng = self._tracker_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="tracker engine unavailable")
        t = self._tracker_find(req)
        if t is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               warnings=["I couldn't find that tracker"])
        low = (req.raw_text or "").lower()
        if "resume" in low or "enable" in low:
            action = "resume"
        elif "disable" in low:
            action = "disable"
        elif "stop" in low or "forget" in low or "delete" in low:
            action = "archive"
        else:
            action = "pause"
        res = eng.control(t.id, action)
        if not res.get("ok"):
            return AgentResult(status=ResultStatus.UNAVAILABLE, data=res,
                               warnings=[res.get("error", "control failed")])
        return AgentResult(
            status=ResultStatus.OK,
            data={"tracker": res["tracker"], "action": action},
            facts=[{"kind": "tracker_control", "tracker_id": t.id,
                    "action": action}])

    def _tracker_evaluate(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._tracker_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="tracker engine unavailable")
        t = self._tracker_find(req)
        if t is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               warnings=["I couldn't find that tracker"])
        res = eng.evaluate(t, now=self.clock.now_ts(), dry_run=True)
        return AgentResult(
            status=ResultStatus.OK,
            data={"evaluation": res.to_dict()},
            facts=[{"kind": "tracker_evaluate", "tracker_id": t.id,
                    "fired": res.fired, "reason": res.reason,
                    "dry_run": True}])

    def _tracker_explain(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._tracker_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="tracker engine unavailable")
        t = self._tracker_find(req)
        if t is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               warnings=["I don't have a tracker that explains that"])
        why = eng.why(t.id)
        return AgentResult(
            status=ResultStatus.OK, data={"explanation": why},
            facts=[{"kind": "tracker_explain", "tracker_id": t.id,
                    "state": t.state}])

    # -------------------------------------------------- creation handlers
    def _creation_module(self) -> Any:
        return getattr(self.container, "creation", None)

    def _creation_ctx(self, req: AgentRequest) -> dict[str, Any]:
        ref = dict(req.topic or {})
        ctx: dict[str, Any] = {}
        if ref.get("chat_id") is not None:
            ctx["chat_id"] = int(ref.get("chat_id") or 0)
            ctx["thread_id"] = int(ref.get("thread_id") or 0)
        topics = getattr(self.container, "topics", None)
        if topics is not None and ctx.get("chat_id") is not None:
            try:
                prof = topics.get(ctx["chat_id"], ctx.get("thread_id", 0))
                if prof is not None:
                    ctx["topic_id"] = prof.id
                    ctx["topic_name"] = prof.name
                    ctx["linked"] = topics.links(prof)
            except Exception:  # noqa: BLE001
                pass
        if req.conversation is not None:
            if req.conversation.focus is not None:
                ctx["focus"] = req.conversation.focus.to_dict()
            ctx["recent"] = [e.to_dict() for e in req.conversation.recent_entities]
        return ctx

    def _creation_gated(self, req: AgentRequest) -> AgentResult:
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            data={"proposed_action": req.action.value, "text": req.raw_text,
                  "committed": False},
            facts=[{"kind": "creation_proposal", "action": req.action.value,
                    "text": req.raw_text}],
            candidate_actions=[{"action": req.action.value,
                                "text": req.raw_text,
                                "requires_confirmation": True}],
            warnings=["this change is prepared but not applied; confirmation "
                      "is required"],
            assumptions=["read-only executive surface: creation mutations "
                         "require explicit confirmation"])

    def _creation_parse(self, req: AgentRequest) -> dict[str, Any]:
        eng = self._creation_module()
        return eng.parse(req.raw_text or "", context=self._creation_ctx(req))

    def _creation_preview(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._creation_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="creation service unavailable")
        prop = self._creation_parse(req)
        return AgentResult(
            status=ResultStatus.OK, data={"proposal": prop},
            facts=[{"kind": "creation_preview",
                    "operation": prop.get("operation"),
                    "target_type": prop.get("target_type"),
                    "confidence": prop.get("confidence")}],
            warnings=list(prop.get("questions") or []))

    def _creation_create(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._creation_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="creation service unavailable")
        if self._read_only:
            return self._creation_gated(req)
        prop = self._creation_parse(req)
        if prop.get("questions"):
            return AgentResult(
                status=ResultStatus.AMBIGUOUS, data={"proposal": prop},
                warnings=list(prop["questions"]),
                missing_information=list(prop["questions"]))
        res = eng.execute(prop)
        if not res.get("ok"):
            status = (ResultStatus.AMBIGUOUS
                      if res.get("status") == "ambiguous"
                      else ResultStatus.UNAVAILABLE)
            return AgentResult(status=status, data=res,
                               warnings=list(res.get("questions")
                                             or [res.get("message", "failed")]))
        return AgentResult(
            status=ResultStatus.OK, data=res,
            facts=[{"kind": "creation", "operation": prop.get("operation"),
                    "target_type": prop.get("target_type"),
                    "created": res.get("created") or res.get("updated")}],
            warnings=[res["message"]] if res.get("message") else [])

    def _creation_link(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._creation_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="creation service unavailable")
        if self._read_only:
            return self._creation_gated(req)
        prop = self._creation_parse(req)
        prop["operation"] = "link"
        res = eng.execute(prop)
        status = ResultStatus.OK if res.get("ok") else (
            ResultStatus.AMBIGUOUS if res.get("status") == "ambiguous"
            else ResultStatus.UNAVAILABLE)
        return AgentResult(status=status, data=res,
                           warnings=list(res.get("questions")
                                         or ([res["message"]]
                                             if res.get("message") else [])))

    def _creation_update(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._creation_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="creation service unavailable")
        if self._read_only:
            return self._creation_gated(req)
        prop = self._creation_parse(req)
        prop["operation"] = "update"
        res = eng.execute(prop)
        status = ResultStatus.OK if res.get("ok") else ResultStatus.UNAVAILABLE
        return AgentResult(status=status, data=res,
                           warnings=[res.get("message", "")] if res.get("message")
                           else [])

    def _creation_organize(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._creation_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="creation service unavailable")
        if self._read_only:
            return self._creation_gated(req)
        prop = self._creation_parse(req)
        prop["operation"] = "organize"
        res = eng.execute(prop)
        if not res.get("ok"):
            return AgentResult(status=ResultStatus.UNAVAILABLE, data=res,
                               warnings=[res.get("message", "could not organize")])
        return AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True, data=res,
            facts=[{"kind": "organize_proposal",
                    "items": len((res.get("plan") or {}).get("items", []))}],
            candidate_actions=[{"action": "organize_apply",
                                "plan": res.get("plan"),
                                "requires_confirmation": True}],
            warnings=[res.get("message", "")])

    def _creation_resolve(self, req: AgentRequest, snap: Any) -> AgentResult:
        eng = self._creation_module()
        if eng is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="creation service unavailable")
        q = _clean_creation_query(req.raw_text)
        res = eng.resolve(q, context=self._creation_ctx(req))
        return AgentResult(
            status=ResultStatus.OK if res.get("status") == "resolved"
            else ResultStatus.AMBIGUOUS, data={"resolution": res},
            facts=[{"kind": "resolve_reference", "status": res.get("status"),
                    "candidates": len(res.get("candidates", []))}])

    # --------------------------------------------------- settings handlers
    def _settings_module(self) -> Any:
        return getattr(self.container, "settings", None)

    def _settings_view(self, req: AgentRequest, snap: Any) -> AgentResult:
        settings = self._settings_module()
        if settings is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="settings unavailable")
        # Q6: inside a topic, "what's enabled here?" means the topic config.
        t = req.topic or {}
        topics = getattr(self.container, "topics", None)
        if topics is not None and t.get("chat_id") is not None:
            try:
                prof = topics.get(int(t["chat_id"]), int(t.get("thread_id") or 0))
            except Exception:  # noqa: BLE001
                prof = None
            if prof is not None:
                return AgentResult(
                    status=ResultStatus.OK,
                    data={"settings": settings.snapshot(),
                          "text": self._topic_state_text("configuration", req)},
                    facts=[{"kind": "settings_view", "scope": "topic"}])
        return AgentResult(
            status=ResultStatus.OK,
            data={"settings": settings.snapshot(), "text": settings.render()},
            facts=[{"kind": "settings_view"}])

    def _settings_update(self, req: AgentRequest, snap: Any) -> AgentResult:
        settings = self._settings_module()
        if settings is None:
            return AgentResult(status=ResultStatus.UNAVAILABLE,
                               error="settings unavailable")
        parsed = settings.parse(req.raw_text or "")
        if not parsed or not parsed.get("changes"):
            return AgentResult(
                status=ResultStatus.UNAVAILABLE,
                warnings=["I couldn't tell which setting to change."])
        if self._read_only:
            return AgentResult(
                status=ResultStatus.NEEDS_CONFIRMATION,
                confirmation_required=True,
                data={"proposed_action": "settings_update",
                      "changes": parsed["changes"], "summary": parsed["summary"]},
                candidate_actions=[{"action": "settings_update",
                                    "changes": parsed["changes"],
                                    "requires_confirmation": True}],
                warnings=[parsed["summary"]])
        applied = {}
        for key, value in parsed["changes"].items():
            res = settings.set(key, value)
            if res.get("ok"):
                applied[key] = res.get("value")
        return AgentResult(
            status=ResultStatus.OK,
            data={"applied": applied, "summary": parsed["summary"]},
            facts=[{"kind": "settings_update", "applied": sorted(applied)}],
            warnings=[parsed["summary"]])

    # ---------------------------------------------------- optimizer handlers
    def _optimizer_module(self) -> Any:
        return getattr(self.container, "optimizer", None)

    def _optimizer_unavailable(self) -> AgentResult:
        return AgentResult(status=ResultStatus.UNAVAILABLE,
                           error="schedule optimizer unavailable")

    def _optimizer_days(self, req: AgentRequest) -> int:
        cfg_max = int(getattr(self.cfg, "optimizer_max_horizon_days", 14) or 14)
        if req.action == ActionKind.OPTIMIZE_WEEK:
            return 7
        if req.scope.kind in (ScopeKind.THIS_WEEK, ScopeKind.NEXT_WEEK):
            return 7
        if req.temporal.start and req.temporal.end \
                and req.temporal.end > req.temporal.start:
            days = (int(req.temporal.end) - int(req.temporal.start)) // 86400 + 1
            return max(1, min(int(days), cfg_max))
        return 1

    def _optimizer_strategy(self) -> str:
        return str(getattr(self.cfg, "optimizer_default_strategy", "balanced")
                   or "balanced")

    def _optimize(self, req: AgentRequest, snap: Any) -> AgentResult:
        opt = self._optimizer_module()
        if opt is None:
            return self._optimizer_unavailable()
        days = self._optimizer_days(req)
        res = opt.optimize_from_state(days=days, strategy=self._optimizer_strategy(),
                                      day_ts=self._scope_day(req),
                                      now=self.clock.now_ts())
        result = AgentResult(
            status=ResultStatus.OK,
            data={"optimization": res.to_dict(), "committed": False},
            facts=[{"kind": "schedule_optimization", "feasible": res.feasible,
                    "strategy": res.strategy, "sessions": len(res.sessions),
                    "unscheduled": len(res.unscheduled_items),
                    "violations": len(res.violations), "score": res.score,
                    "horizon_days": res.horizon.get("days"),
                    "churn": res.churn.get("counts", {})}],
            warnings=[str(v.get("detail", "")) for v in res.violations][:5],
            assumptions=["read-only optimization proposal; nothing was committed",
                         "hard constraints are absolute; soft preferences only rank"])
        for s in res.sessions[:8]:
            result.recommendations.append(Recommendation(
                action=ActionKind.RECOMMEND,
                target=EntityRef(type=EntityType.TASK, id=str(s.task_id),
                                 name=s.title, resolved=True, confidence=0.8,
                                 source="optimizer"),
                title=s.title, reason=s.reason, score=res.score,
                window=[s.start_min, s.end_min],
                window_ts=[s.start_ts, s.end_ts],
                provenance="optimizer.optimize"))
        return result

    def _evaluate_schedule(self, req: AgentRequest, snap: Any) -> AgentResult:
        opt = self._optimizer_module()
        if opt is None:
            return self._optimizer_unavailable()
        days = self._optimizer_days(req)
        if days == 1 and req.action == ActionKind.EVALUATE_SCHEDULE:
            days = 7
        res = opt.optimize_from_state(days=days, strategy=self._optimizer_strategy(),
                                      day_ts=self._scope_day(req),
                                      now=self.clock.now_ts())
        projects = res.risk_summary.get("projects", [])
        tightest = res.risk_summary.get("most_at_risk")
        result = AgentResult(
            status=ResultStatus.OK,
            data={"optimization": res.to_dict(), "evaluated": True},
            facts=[{"kind": "schedule_evaluation", "feasible": res.feasible,
                    "horizon_days": res.horizon.get("days"),
                    "sessions": len(res.sessions),
                    "unscheduled": len(res.unscheduled_items),
                    "violations": len(res.violations),
                    "overall_pressure": res.risk_summary.get("overall_pressure"),
                    "tightest_project": (tightest or {}).get("project_id"),
                    "tightest_slack_minutes": (tightest or {}).get("slack_minutes"),
                    "projects": len(projects)}],
            warnings=[str(v.get("detail", "")) for v in res.violations][:5],
            conflicts=[{"kind": v.get("kind"), "task_id": v.get("task_id"),
                        "detail": v.get("detail")} for v in res.violations],
            assumptions=["read-only feasibility evaluation; nothing was committed"])
        for p in projects:
            if p.get("slack_minutes") is not None and p["slack_minutes"] < 0:
                result.warnings.append(
                    f"project {p['project_id']} is short by "
                    f"{-p['slack_minutes']} minutes before its deadline")
        return result

    def _find_best_slot(self, req: AgentRequest, snap: Any) -> AgentResult:
        opt = self._optimizer_module()
        if opt is None:
            return self._optimizer_unavailable()
        days = 7
        res = opt.optimize_from_state(days=days, strategy=self._optimizer_strategy(),
                                      day_ts=self._scope_day(req),
                                      now=self.clock.now_ts())
        sessions = res.sessions
        tid = None
        if req.target is not None and req.target.resolved \
                and req.target.type == EntityType.TASK:
            try:
                tid = int(req.target.id)
            except (TypeError, ValueError):
                tid = None
        if tid is not None:
            sessions = [s for s in sessions if s.task_id == tid]
        else:
            terms: list[str] = []
            if req.target is not None and req.target.name:
                terms.append(req.target.name.strip().lower())
            for e in req.entities:
                if e.name:
                    terms.append(e.name.strip().lower())
            terms = [t for t in terms if len(t) >= 3]
            if not terms:
                terms = [w for w in re.split(r"\W+", (req.raw_text or "").lower())
                         if len(w) >= 4]
            if terms:
                sessions = [s for s in sessions
                            if any(t in s.title.lower() for t in terms)]
        if not sessions:
            return AgentResult(
                status=ResultStatus.UNAVAILABLE,
                data={"optimization": res.to_dict()},
                warnings=["no feasible slot found for that task in the horizon"],
                assumptions=["read-only optimization; nothing was committed"])
        best = sessions[0]
        result = AgentResult(
            status=ResultStatus.OK,
            data={"best_slot": best.to_dict(), "optimization": res.to_dict()},
            facts=[{"kind": "best_slot", "task_id": best.task_id,
                    "title": best.title, "start": best.start_min,
                    "end": best.end_min, "feasible": res.feasible}],
            assumptions=["read-only optimization; nothing was committed"])
        result.recommendations.append(Recommendation(
            action=ActionKind.RECOMMEND,
            target=EntityRef(type=EntityType.TASK, id=str(best.task_id),
                             name=best.title, resolved=True, confidence=0.8,
                             source="optimizer"),
            title=best.title, reason=best.reason, score=res.score,
            window=[best.start_min, best.end_min],
            window_ts=[best.start_ts, best.end_ts],
            provenance="optimizer.find_best_slot"))
        return result

    def _reschedule_optimized(self, req: AgentRequest, snap: Any) -> AgentResult:
        opt = self._optimizer_module()
        if opt is None:
            return self._optimizer_unavailable()
        days = self._optimizer_days(req)
        res = opt.optimize_from_state(days=days, strategy=self._optimizer_strategy(),
                                      day_ts=self._scope_day(req),
                                      now=self.clock.now_ts())
        counts = res.churn.get("counts", {})
        planner = getattr(self.container, "planner", None)
        undo_available = bool(planner is not None
                              and getattr(planner, "db", None) is not None
                              and planner.db.latest_plan() is not None)
        change = (counts.get("moved", 0) + counts.get("added", 0)
                  + counts.get("removed", 0))
        result = AgentResult(
            status=ResultStatus.NEEDS_CONFIRMATION,
            confirmation_required=True,
            data={"optimization": res.to_dict(), "diff": res.churn,
                  "undo_available": undo_available, "committed": False},
            facts=[{"kind": "reschedule_proposal", "feasible": res.feasible,
                    "moved": counts.get("moved", 0),
                    "added": counts.get("added", 0),
                    "removed": counts.get("removed", 0),
                    "unchanged": counts.get("unchanged", 0),
                    "affected_tasks": [m["task_id"] for m in res.churn.get("moved", [])]
                    + [a["task_id"] for a in res.churn.get("added", [])],
                    "undo_available": undo_available,
                    "deadline_impact": len([v for v in res.violations
                                            if v.get("kind") == "deadline_miss"]),
                    "risk_impact": res.risk_summary.get("overall_pressure")}],
            candidate_actions=[{"action": "reschedule",
                                "requires_confirmation": True,
                                "changes": change,
                                "diff": res.churn,
                                "undo_available": undo_available}],
            warnings=["this reschedule is prepared but not applied; confirmation "
                      "is required before any calendar or schedule change",
                      f"{change} block(s) would change"] if change
            else ["the optimized schedule matches the current one"],
            assumptions=["optimization is read-only; committing uses the existing "
                         "safety, idempotency and undo path"])
        if not res.feasible:
            result.warnings.append("the requested horizon is not fully feasible")
        return result

    # --------------------------------------------------------- web handlers
    def _web_module(self) -> Any:
        return getattr(self.container, "web", None)

    def _web_unavailable(self) -> AgentResult:
        return AgentResult(status=ResultStatus.UNAVAILABLE,
                           error="web knowledge unavailable")

    def _web_search(self, req: AgentRequest, snap: Any) -> AgentResult:
        web = self._web_module()
        if web is None:
            return self._web_unavailable()
        q = _clean_web_query(req.raw_text)
        sr = web.search(q)
        snap.external_sources = [s.to_dict() for s in sr.results][:10]
        snap.research_timestamp = int(sr.current_as_of or 0)
        result = AgentResult(
            status=ResultStatus.OK if sr.ok else ResultStatus.UNAVAILABLE,
            data={"search": sr.to_dict()},
            facts=[{"kind": "web_search", "query": q, "ok": sr.ok,
                    "provider": sr.provider, "results": len(sr.results),
                    "cached": sr.cached}],
            assumptions=["web results are untrusted external data"])
        if not sr.ok:
            result.warnings.append(sr.error or "web search unavailable")
        return result

    def _web_research(self, req: AgentRequest, snap: Any) -> AgentResult:
        web = self._web_module()
        if web is None:
            return self._web_unavailable()
        q = _clean_web_query(req.raw_text)
        rr = web.research(q)
        self._attach_research(snap, rr)
        verified = bool(rr.external_verified)
        result = AgentResult(
            status=ResultStatus.OK if verified else ResultStatus.UNAVAILABLE,
            data={"research": rr.to_dict()},
            facts=[{"kind": "web_research", "query": q, "status": rr.status,
                    "confidence": rr.confidence, "sources": len(rr.sources),
                    "external_verified": verified, "used_cache": rr.used_cache}],
            warnings=list(rr.limitations),
            assumptions=["external sources are untrusted data; page content is "
                         "never treated as instructions"])
        if not verified:
            result.warnings.append("could not verify from available sources")
        return result

    def _web_fetch(self, req: AgentRequest, snap: Any) -> AgentResult:
        web = self._web_module()
        if web is None:
            return self._web_unavailable()
        url = ""
        if req.target is not None and req.target.type == EntityType.URL:
            url = req.target.name or req.target.id
        if not url:
            m = _URL_IN_TEXT.search(req.raw_text or "")
            url = m.group(0).rstrip(".,);]") if m else ""
        if not url:
            return AgentResult.ambiguous([Ambiguity(
                kind=AmbiguityKind.ENTITY, field_name="url",
                mention=(req.raw_text or "")[:60],
                reason="I need the web address you want me to open")])
        fr = web.fetch(url)
        if fr.source is not None:
            snap.external_sources = [fr.source.to_dict()]
            snap.research_timestamp = int(fr.source.retrieved_at or 0)
        result = AgentResult(
            status=ResultStatus.OK if fr.ok else ResultStatus.UNAVAILABLE,
            data={"fetch": fr.to_dict()},
            facts=[{"kind": "web_fetch", "url": fr.final_url or url,
                    "ok": fr.ok, "status": fr.status, "title": fr.title,
                    "cached": fr.cached,
                    "injection_flags": (fr.source.injection_flags
                                        if fr.source else [])}],
            warnings=([fr.error] if fr.error else []),
            assumptions=["page content is untrusted external data"])
        return result

    def _knowledge_lookup(self, req: AgentRequest, snap: Any) -> AgentResult:
        web = self._web_module()
        if web is None:
            return self._web_unavailable()
        q = _clean_web_query(req.raw_text)
        kr = web.knowledge_lookup(q)
        return AgentResult(
            status=ResultStatus.OK if kr.local_knowledge
            else ResultStatus.UNAVAILABLE,
            data={"knowledge": kr.to_dict()},
            facts=[{"kind": "local_knowledge",
                    "count": len(kr.local_knowledge),
                    "external_verified": False}],
            warnings=list(kr.limitations),
            assumptions=["answered from local Butler state only; not the web"])

    def _attach_research(self, snap: Any, rr: Any) -> None:
        snap.external_sources = [s.to_dict() for s in rr.sources][:10]
        snap.external_facts = [e.to_dict() for e in rr.evidence][:20]
        snap.research_summary = rr.answer
        snap.research_timestamp = int(rr.current_as_of or 0)

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


#: Longest-first so "check online whether" strips before "check online".
_WEB_STRIP = (
    "check online whether", "check online if", "check online",
    "search online for", "search online", "look online for", "look online",
    "search the web for", "search the web", "check the web for",
    "check the web", "look up online", "find online", "web search for",
    "web search", "google for", "google", "check the course page",
    "check the page", "check the website", "open this webpage",
    "open the webpage", "open this page", "what's the latest on",
    "whats the latest on", "what is the latest on", "what's the latest",
    "whats the latest", "what is the latest", "find information about",
    "find info about", "look up information about", "look up information",
    "search for", "look up", "what do you know about", "what do you know",
    "do you know about", "do you know", "research",
)

_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.I)


def _clean_web_query(text: str) -> str:
    """Strip a leading trigger phrase so the query sent to search is focused."""
    q = (text or "").strip()
    low = q.lower()
    for prefix in _WEB_STRIP:
        if low.startswith(prefix):
            q = q[len(prefix):].strip(" ?.,:;!-")
            break
    return q or (text or "").strip()


#: Longest-first so "forget that i prefer" strips before "forget that".
_MEMORY_STRIP = (
    "show me what you remember about", "show me what you remember",
    "what do you remember about", "what do you remember",
    "what have you learned about", "what have you learned",
    "what have you learnt about", "what have you learnt",
    "show me my learned routines", "show me my routines",
    "show my memories", "list my memories", "show me my memories",
    "search my memories for", "search my memory for", "search my memories",
    "search my memory", "look up in my memory", "find in my memory",
    "why do you think i", "why did you suggest", "why did you schedule this",
    "explain why you", "why do you remember",
    "please remember that", "remember that", "remember this",
    "note that i", "keep in mind that", "don't forget that",
    "do not forget that", "please remember", "remember",
    "forget that i", "forget that", "forget about", "forget my",
    "forget the", "stop remembering", "don't remember", "forget what i",
    "confirm that", "confirm my",
    "actually i prefer", "no i prefer", "correct that", "update my preference",
    "i changed my mind", "these days i prefer", "that's wrong about me",
)


def _clean_memory_query(text: str) -> str:
    q = (text or "").strip()
    low = q.lower()
    for prefix in _MEMORY_STRIP:
        if low.startswith(prefix):
            q = q[len(prefix):].strip(" ?.,:;!-")
            break
    return q or (text or "").strip()


#: Longest-first proactive trigger phrases.
_PROACTIVE_STRIP = (
    "what are you warning me about", "what are you reminding me about",
    "what should i know right now", "what should i know",
    "what's important right now", "whats important right now",
    "anything i should know", "what proactive recommendations do you have",
    "what recommendations do you have", "show me your recommendations",
    "show my proactive", "list proactive", "proactive recommendations",
    "why are you telling me this", "why are you reminding me",
    "why are you warning me", "why are you telling me about",
    "why did you warn me", "explain this warning", "why this warning",
    "stop reminding me about", "stop reminding me", "stop warning me",
    "don't remind me about", "don't remind me", "do not remind me",
    "stop telling me about", "stop notifying me", "remind me later",
    "remind me in", "remind me tomorrow", "remind me tonight", "snooze",
    "daily briefing", "morning briefing", "give me the briefing",
    "what proactive",
)


def _clean_proactive_query(text: str) -> str:
    q = (text or "").strip()
    low = q.lower()
    for prefix in _PROACTIVE_STRIP:
        if low.startswith(prefix):
            q = q[len(prefix):].strip(" ?.,:;!-")
            break
    return q or (text or "").strip()


#: Longest-first tracker trigger phrases (for resolving a tracker by name).
_TRACKER_STRIP = (
    "show what you're tracking", "show what you are tracking",
    "what am i tracking here", "what are you tracking here",
    "why did you notify me", "why did you alert me", "why did you tell me",
    "would this tracker fire", "would the tracker fire",
    "evaluate the tracker", "evaluate tracker", "dry run tracker",
    "test the tracker", "test this tracker",
    "show my trackers", "list my trackers", "what are you tracking",
    "my trackers", "list trackers", "show trackers",
    "stop tracking", "pause tracking", "resume tracking", "disable tracking",
    "pause the tracker", "resume the tracker", "stop the tracker",
    "forget the tracker", "delete the tracker",
    "tell me when", "notify me when", "warn me when", "warn me if",
    "let me know when", "i want to know when", "i want to know if",
    "alert me when", "alert me if", "track ", "watch ", "monitor ",
)


def _clean_tracker_query(text: str) -> str:
    q = (text or "").strip()
    low = q.lower()
    for prefix in _TRACKER_STRIP:
        if low.startswith(prefix):
            q = q[len(prefix):].strip(" ?.,:;!-")
            break
    return q or (text or "").strip()


#: Longest-first creation trigger phrases (for resolving a reference).
_CREATION_STRIP = (
    "put this in the right place", "show me what you'll", "what would you do",
    "preview this", "dry run this", "organize these", "organize this",
    "organise this", "sort these", "sort my", "archive old", "tidy up",
    "put this under", "put it under", "file this under", "link this to",
    "link this", "link it to", "link it", "connect this to", "connect this",
    "connect it", "attach this", "associate this", "link to", "connect to",
    "use my pantry", "change the deadline", "update the deadline",
    "change the name", "rename it", "rename this", "actually make it",
    "make it ", "edit this", "add this to", "add this", "create this",
    "save this", "create a note", "save a note", "add a reminder",
    "schedule this", "put two hours", "block two hours", "add a project",
    "create a project", "new project", "add a course", "create a course",
    "add a topic", "create a topic", "add to my pantry", "add to my food",
    "add to my groceries", "add to groceries", "what is this", "which project",
)


def _clean_creation_query(text: str) -> str:
    q = (text or "").strip()
    low = q.lower()
    for prefix in _CREATION_STRIP:
        if low.startswith(prefix):
            q = q[len(prefix):].strip(" ?.,:;!-")
            break
    return q or (text or "").strip()
