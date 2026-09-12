"""Q13: generic conversation coordination (active-task continuity).

The problem this solves is general, not a phrase: Butler had no canonical
representation of *what we are doing right now*, so a follow-up turn was
interpreted as an unrelated new request. Mature conversational systems keep an
explicit, persistent active state and classify each turn relative to it:

* Rasa keeps an ``active_loop`` (a running form) with ``required_slots`` and a
  ``requested_slot``; every turn is first offered to the active loop, which may
  fill a slot, be interrupted, or be cancelled (unhappy paths).
* Dialogflow CX keeps form/session parameters and, when the user says something
  that is not a slot value, fires ``sys.no-match`` and reprompts from the same
  page rather than starting a new flow.

Butler's version is deliberately minimal and server-authoritative: a single
transient :class:`ConversationTask` per session, plus a semantic
:class:`TurnClassifier` that decides how the current turn relates to it
(answer / modify / query / continue / cancel / supersede / meta-question). The
model interprets language; this state determines what the language refers to.

No phrase lists, no per-domain clarifiers: classification is semantic, with a
small *structural* fallback (punctuation, candidate matching, action comparison)
used only when no model is configured.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .semantic import ActionKind, RequestIntent

DEFAULT_TASK_TTL = 1800  # 30 minutes of inactivity ends an active task


class TurnKind(str, Enum):
    """How the current user turn relates to the active conversation task."""

    NEW_REQUEST = "new_request"
    ANSWER_CLARIFICATION = "answer_clarification"
    ANSWER_CONFIRMATION = "answer_confirmation"
    MODIFY_ACTIVE_REQUEST = "modify_active_request"
    QUERY_ACTIVE_REQUEST = "query_active_request"
    CONTINUE_ACTIVE_REQUEST = "continue_active_request"
    CANCEL_ACTIVE_REQUEST = "cancel_active_request"
    INTERRUPT_WITH_NEW_REQUEST = "interrupt_with_new_request"
    META_QUESTION = "meta_question"          # asks about the active question
    UNKNOWN = "unknown"


class TaskStatus(str, Enum):
    ACTIVE = "active"
    WAITING_CLARIFICATION = "waiting_clarification"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


#: intents whose requests are worth keeping active across turns (they can be
#: refined). Pure reads/chat finish immediately.
_CONTINUABLE_INTENTS = frozenset({
    RequestIntent.ADVISE, RequestIntent.PLAN,
    RequestIntent.MUTATE, RequestIntent.EVALUATE,
})


@dataclass
class ConversationTask:
    """Transient conversational intent/state — never permanent domain truth.

    Deliberately shallow: it stores the *typed request* that is in progress and
    which slots remain, not copies of domain objects. Targets keep their ids so
    the server can re-validate them; they are never trusted blindly.
    """

    id: str = ""
    status: str = TaskStatus.ACTIVE.value
    original_request: str = ""
    normalized_request: str = ""
    current_action: str = ActionKind.UNKNOWN.value
    filled_slots: dict[str, Any] = field(default_factory=dict)
    missing_slots: list[str] = field(default_factory=list)
    target: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=dict)
    constraints: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    source_turn: int = 0
    created_at: int = 0
    last_updated: int = 0
    expires_at: int = 0
    #: the typed request as last seen (used to resume/refine without re-deriving)
    request: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        now = int(time.time())
        self.created_at = self.created_at or now
        self.last_updated = self.last_updated or now
        if not self.expires_at:
            self.expires_at = now + DEFAULT_TASK_TTL

    def is_expired(self, now: int | None = None) -> bool:
        now = int(now if now is not None else time.time())
        return bool(self.expires_at) and now >= self.expires_at

    def is_open(self, now: int | None = None) -> bool:
        return self.status in (TaskStatus.ACTIVE.value,
                               TaskStatus.WAITING_CLARIFICATION.value,
                               TaskStatus.WAITING_CONFIRMATION.value) \
            and not self.is_expired(now)

    def touch(self, ttl: int = DEFAULT_TASK_TTL) -> None:
        now = int(time.time())
        self.last_updated = now
        self.expires_at = now + int(ttl)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        for key in ("filled_slots", "target", "scope", "request"):
            d[key] = dict(d.get(key) or {})
        d["missing_slots"] = list(self.missing_slots or [])
        d["constraints"] = [dict(c) for c in (self.constraints or [])]
        d["proposals"] = [dict(p) for p in (self.proposals or [])]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConversationTask":
        return cls(
            id=str(d.get("id", "")),
            status=str(d.get("status", TaskStatus.ACTIVE.value)),
            original_request=str(d.get("original_request", "")),
            normalized_request=str(d.get("normalized_request", "")),
            current_action=str(d.get("current_action",
                                     ActionKind.UNKNOWN.value)),
            filled_slots=dict(d.get("filled_slots") or {}),
            missing_slots=[str(s) for s in (d.get("missing_slots") or [])],
            target=dict(d.get("target") or {}),
            scope=dict(d.get("scope") or {}),
            constraints=[dict(c) for c in (d.get("constraints") or [])
                         if isinstance(c, dict)],
            proposals=[dict(p) for p in (d.get("proposals") or [])
                       if isinstance(p, dict)],
            source_turn=int(d.get("source_turn", 0) or 0),
            created_at=int(d.get("created_at", 0) or 0),
            last_updated=int(d.get("last_updated", 0) or 0),
            expires_at=int(d.get("expires_at", 0) or 0),
            request=dict(d.get("request") or {}),
        )

    def summary(self) -> dict[str, Any]:
        """Compact, model-facing view (never the whole request dump)."""
        return {
            "task_id": self.id,
            "status": self.status,
            "action": self.current_action,
            "original_request": self.original_request,
            "filled_slots": dict(self.filled_slots),
            "missing_slots": list(self.missing_slots),
            "target": self.target,
            "scope": self.scope,
            "constraints": list(self.constraints),
            "has_proposals": bool(self.proposals),
        }


class ConversationTaskStore:
    """The single canonical active task, attached to the session."""

    _KEY = "active_task"

    def active(self, session: Any, now: int | None = None
               ) -> ConversationTask | None:
        data = getattr(session, "data", None)
        if data is None:
            return None
        raw = data.get(self._KEY)
        if not raw:
            return None
        task = ConversationTask.from_dict(raw) if isinstance(raw, dict) else raw
        if task.is_expired(now):
            task.status = TaskStatus.CANCELLED.value
            data.pop(self._KEY, None)
            return None
        if not task.is_open(now):
            return None
        return task

    def set(self, session: Any, task: ConversationTask) -> ConversationTask:
        task.touch()
        if getattr(session, "data", None) is not None:
            session.data[self._KEY] = task.to_dict()
        return task

    def clear(self, session: Any, status: str = TaskStatus.COMPLETED.value
              ) -> None:
        data = getattr(session, "data", None)
        if data is None:
            return
        raw = data.get(self._KEY)
        if isinstance(raw, dict):
            raw["status"] = status
        data.pop(self._KEY, None)

    # ------------------------------------------------------------- recording
    def begin(self, session: Any, req: Any, *,
              status: str = TaskStatus.ACTIVE.value) -> ConversationTask:
        """Create/replace the active task from a typed request."""
        task = ConversationTask(
            status=status,
            original_request=req.raw_text or "",
            normalized_request=(req.raw_text or "").strip(),
            current_action=req.action.value,
            filled_slots=dict(req.parameters or {}),
            target=(req.target.to_dict() if req.target is not None else {}),
            scope=(req.scope.to_dict() if req.scope is not None else {}),
            constraints=[c.to_dict() for c in (req.constraints or [])],
            request=req.to_dict(),
            source_turn=1,
        )
        return self.set(session, task)

    def update_from_request(self, session: Any, task: ConversationTask,
                            req: Any) -> ConversationTask:
        """Merge a refinement into the active task (same request, new slots)."""
        params = dict(task.filled_slots)
        params.update(req.parameters or {})
        task.filled_slots = params
        if req.target is not None and req.target.name:
            task.target = req.target.to_dict()
        if req.scope is not None and req.scope.kind.value != "unknown":
            task.scope = req.scope.to_dict()
        for c in (req.constraints or []):
            cd = c.to_dict()
            if cd not in task.constraints:
                task.constraints.append(cd)
        task.request = req.to_dict()
        task.original_request = task.original_request or req.raw_text
        task.current_action = req.action.value
        task.status = TaskStatus.ACTIVE.value
        return self.set(session, task)

    def set_status(self, session: Any, status: str, *,
                   missing_slots: list[str] | None = None) -> None:
        data = getattr(session, "data", None)
        if not data:
            return
        raw = data.get(self._KEY)
        if not isinstance(raw, dict):
            return
        raw["status"] = status
        if missing_slots is not None:
            raw["missing_slots"] = list(missing_slots)
        raw["last_updated"] = int(time.time())
        data[self._KEY] = raw

    def should_persist(self, req: Any) -> bool:
        """Whether a successfully-handled request should stay active.

        Behavior-management actions are discrete commands, not evolving
        requests, so they never become a lingering active task.
        """
        if req.action in (ActionKind.CREATE_TOPIC_BEHAVIOR,
                          ActionKind.TOPIC_BEHAVIOR_CONTROL,
                          ActionKind.TOPIC_BEHAVIOR_QUERY):
            return False
        return req.intent in _CONTINUABLE_INTENTS


# ---------------------------------------------------------------------------
# turn classification
# ---------------------------------------------------------------------------

_TURN_VALUES = [k.value for k in TurnKind]


class TurnClassifier:
    """Decide how the current turn relates to the active task.

    Semantic by default (one compact model call); a *structural* fallback is
    used when no model is available. The fallback never matches phrases — it
    reasons from punctuation, candidate matching, action identity and the
    server's own resolution, so it stays general across domains.
    """

    def __init__(self, chat: Any = None):
        self.chat = chat

    def available(self) -> bool:
        return bool(self.chat is not None
                    and getattr(self.chat, "_llm_ready", lambda: False)())

    def classify(self, text: str, task: ConversationTask | None, *,
                 interpreted: Any = None,
                 candidates: list[dict[str, Any]] | None = None,
                 expected_slot: str = "",
                 awaiting_confirmation: bool = False) -> TurnKind:
        raw = (text or "").strip()
        if task is None and not candidates and not awaiting_confirmation:
            return TurnKind.NEW_REQUEST
        if not raw:
            return TurnKind.UNKNOWN
        if self.available():
            kind = self._semantic(raw, task, interpreted, candidates,
                                  expected_slot, awaiting_confirmation)
            if kind is not None:
                return kind
        return self._structural(raw, task, interpreted, candidates,
                                expected_slot, awaiting_confirmation)

    # ------------------------------------------------------------- semantic
    def _semantic(self, text: str, task: ConversationTask | None,
                  interpreted: Any, candidates: list[dict[str, Any]] | None,
                  expected_slot: str,
                  awaiting_confirmation: bool) -> TurnKind | None:
        context: dict[str, Any] = {
            "active_task": task.summary() if task is not None else None,
            "pending_question": expected_slot or None,
            "pending_options": [str(c.get("label") or c.get("name") or "")
                                for c in (candidates or [])][:6] or None,
            "awaiting_confirmation": awaiting_confirmation,
        }
        if interpreted is not None:
            context["preliminary_interpretation"] = {
                "action": getattr(interpreted.action, "value",
                                  str(interpreted.action)),
                "confidence": round(float(getattr(interpreted, "confidence",
                                                  0.0) or 0.0), 2),
                "target": (getattr(interpreted, "target", None).name
                           if getattr(interpreted, "target", None) else ""),
            }
        system = (
            "You classify ONE user turn relative to an assistant's active "
            "conversation state. The user may answer a pending question, ask "
            "about that question, modify/continue the active request, cancel "
            "it, ask about the active request, or start an unrelated request. "
            "Reply ONLY JSON: {\"kind\":<one of " + json.dumps(_TURN_VALUES)
            + ">, \"confidence\":<0..1>}.\n"
            "Definitions: new_request = unrelated fresh request; "
            "answer_clarification = answers the pending question, including "
            "choosing one of pending_options; "
            "answer_confirmation = approves/rejects the pending proposal; "
            "modify_active_request = changes a detail of the active request; "
            "query_active_request = asks what the assistant is doing/will do; "
            "continue_active_request = adds a refinement without restating it; "
            "cancel_active_request = abandons the active task; "
            "interrupt_with_new_request = a clearly different new request "
            "(including a new standing instruction) while a question is "
            "pending; meta_question = asks about the pending question itself "
            "rather than answering it.\n"
            "A turn that is itself a question and does not select among "
            "pending_options is NOT answer_clarification; it is a query or a "
            "new/interrupting request.\n"
            "STATE: " + json.dumps(context, default=str))
        try:
            out = self.chat.complete(system, text, json_mode=True)
            kind = json.loads(out or "{}").get("kind")
            if kind in _TURN_VALUES:
                return TurnKind(kind)
        except Exception:  # noqa: BLE001 — fall through to structural
            return None
        return None

    # ----------------------------------------------------------- structural
    def _structural(self, text: str, task: ConversationTask | None,
                    interpreted: Any, candidates: list[dict[str, Any]] | None,
                    expected_slot: str,
                    awaiting_confirmation: bool) -> TurnKind:
        from .interactions import match_candidates

        action = getattr(getattr(interpreted, "action", None), "value",
                         getattr(interpreted, "action", None))
        try:
            confidence = float(getattr(interpreted, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        question = text.strip().endswith("?")
        unknown = action in (None, "", ActionKind.UNKNOWN.value,
                             RequestIntent.CHAT.value)
        modify = action in (ActionKind.UPDATE.value, ActionKind.UPDATE_ITEM.value,
                            ActionKind.SETTINGS_UPDATE.value)
        query = action in (ActionKind.STATUS.value,
                           ActionKind.TRACKER_LIST.value,
                           ActionKind.SETTINGS_VIEW.value)

        # 0. An explicit cancellation action (the model mapped it) always wins.
        if action == ActionKind.CANCEL.value:
            return TurnKind.CANCEL_ACTIVE_REQUEST
        # A refinement of an existing request (update-style action) modifies it.
        if modify and (task is not None or awaiting_confirmation):
            return TurnKind.MODIFY_ACTIVE_REQUEST

        # 1. A concrete answer to the stored candidate set wins first.
        if candidates:
            if match_candidates(text, candidates) is not None:
                return TurnKind.ANSWER_CLARIFICATION
            if question:
                return TurnKind.META_QUESTION
            if unknown:
                return TurnKind.ANSWER_CLARIFICATION
            if action != getattr(task, "current_action", None) \
                    and confidence >= 0.6:
                return TurnKind.INTERRUPT_WITH_NEW_REQUEST
            return TurnKind.ANSWER_CLARIFICATION

        # 2. A pending confirmation.
        if awaiting_confirmation:
            if question and (unknown or query):
                return TurnKind.META_QUESTION
            if action not in (None, "") and not unknown \
                    and task is not None \
                    and action != getattr(task, "current_action", None) \
                    and confidence >= 0.6:
                return TurnKind.INTERRUPT_WITH_NEW_REQUEST
            return TurnKind.ANSWER_CONFIRMATION

        # 3. A free-text answer to a non-candidate slot.
        if expected_slot:
            if question and unknown:
                return TurnKind.META_QUESTION
            if action not in (None, "") and not unknown \
                    and action != getattr(task, "current_action", None) \
                    and confidence >= 0.6:
                return TurnKind.INTERRUPT_WITH_NEW_REQUEST
            return TurnKind.ANSWER_CLARIFICATION

        # 4. An active task with no pending question.
        if task is not None:
            if question and (query or unknown):
                return TurnKind.QUERY_ACTIVE_REQUEST
            same = action == getattr(task, "current_action", None)
            # Adding a capability (web) to the active task is a continuation,
            # not an unrelated new request.
            if action in (ActionKind.WEB_SEARCH.value,
                          ActionKind.WEB_RESEARCH.value):
                return TurnKind.CONTINUE_ACTIVE_REQUEST
            if unknown or (same and confidence < 0.95):
                return TurnKind.CONTINUE_ACTIVE_REQUEST
            if same:
                return TurnKind.MODIFY_ACTIVE_REQUEST
            if confidence >= 0.6:
                return TurnKind.INTERRUPT_WITH_NEW_REQUEST
            return TurnKind.CONTINUE_ACTIVE_REQUEST

        return TurnKind.NEW_REQUEST
