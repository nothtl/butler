"""Q1: generic clarification engine (slot filling + buttons).

One engine, no per-domain clarifiers. It turns a missing/ambiguous slot into a
structured :class:`ClarificationRequest`, parses natural-language or button
answers deterministically, and applies the chosen value back onto the typed
``AgentRequest``. Clarification (missing information) is kept strictly separate
from confirmation (approval of an understood action).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .actions import SlotSpec, required_slots, slot_spec, slots_for
from .semantic import (
    ActionKind, AgentRequest, EntityRef, EntityType, Scope, ScopeKind,
    TemporalRange, TemporalResolution,
)


class ClarificationKind(str, Enum):
    CHOICE = "choice"
    TARGET = "target"
    VALUE = "value"
    TIME = "time"
    SCOPE = "scope"
    CONFIRMATION = "confirmation"


#: slots that are answered by live-state candidates rather than static options
_DYNAMIC_TARGET = "candidates"
_DYNAMIC_SCOPE = "topic_scope"


def _oid(label: str, value: Any) -> str:
    raw = f"{label}|{value}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:8]


@dataclass
class ClarificationOption:
    label: str
    value: Any = None
    option_id: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.option_id:
            self.option_id = _oid(self.label, self.value)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "value": self.value,
                "option_id": self.option_id, "description": self.description}


@dataclass
class ClarificationRequest:
    interaction_id: str = ""
    slot_name: str = ""
    question: str = ""
    kind: str = ClarificationKind.VALUE.value
    options: list[ClarificationOption] = field(default_factory=list)
    free_text_allowed: bool = True
    default: Any = None
    topic_scope: dict[str, Any] = field(default_factory=dict)
    created_at: int = 0
    expires_at: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "interaction_id": self.interaction_id, "slot_name": self.slot_name,
            "question": self.question, "kind": self.kind,
            "options": [o.to_dict() for o in self.options],
            "free_text_allowed": self.free_text_allowed,
            "default": self.default, "topic_scope": self.topic_scope,
            "created_at": self.created_at, "expires_at": self.expires_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClarificationRequest":
        return cls(
            interaction_id=str(d.get("interaction_id", "")),
            slot_name=str(d.get("slot_name", "")),
            question=str(d.get("question", "")),
            kind=str(d.get("kind", ClarificationKind.VALUE.value)),
            options=[ClarificationOption(**o) for o in (d.get("options") or [])],
            free_text_allowed=bool(d.get("free_text_allowed", True)),
            default=d.get("default"), topic_scope=dict(d.get("topic_scope") or {}),
            created_at=int(d.get("created_at", 0) or 0),
            expires_at=int(d.get("expires_at", 0) or 0),
            reason=str(d.get("reason", "")))


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def build_clarification(interaction_id: str, spec: SlotSpec, *,
                        candidates: list[dict[str, Any]] | None = None,
                        topic_scope: dict[str, Any] | None = None,
                        question: str = "", reason: str = "",
                        ttl: int = 900) -> ClarificationRequest:
    now = int(time.time())
    options: list[ClarificationOption] = []
    if spec.kind == ClarificationKind.TARGET.value and candidates:
        for c in candidates:
            label = str(c.get("label") or c.get("display") or c.get("name") or "")
            options.append(ClarificationOption(label=label, value=c))
        options.append(ClarificationOption(label="None of these", value=None))
    elif spec.options:
        for label in spec.options:
            options.append(ClarificationOption(label=label, value=label))
    return ClarificationRequest(
        interaction_id=interaction_id, slot_name=spec.name,
        question=question or spec.question or f"Please choose {spec.name}.",
        kind=spec.kind, options=options,
        free_text_allowed=spec.free_text, default=spec.default,
        topic_scope=dict(topic_scope or {}), created_at=now,
        expires_at=now + int(ttl), reason=reason)


# ---------------------------------------------------------------------------
# slot detection / application
# ---------------------------------------------------------------------------

_ALIASES = {
    "capability": ("capability", "setting", "cap"),
    "state": ("state", "value", "enabled"),
    "scope": ("scope",),
    "duration": ("duration", "minutes", "duration_minutes"),
    "event_types": ("event_types", "events", "watch"),
    "cadence": ("cadence", "frequency"),
    "destination": ("destination", "notify_where"),
    "notification_policy": ("notification_policy", "notify"),
    "title": ("title", "name"),
    "content": ("content", "text", "fact"),
    "deadline": ("deadline", "due"),
    "project": ("project", "project_id"),
}


def _param(req: AgentRequest, slot: str) -> Any:
    for key in _ALIASES.get(slot, (slot,)):
        if key in (req.parameters or {}):
            val = req.parameters[key]
            if val not in (None, "", []):
                return val
    return None


def slot_is_filled(req: AgentRequest, spec: SlotSpec) -> bool:
    name = spec.name
    if name == "target":
        return req.target is not None and bool(req.target.name)
    if name == "project":
        return (req.target is not None and req.target.type == EntityType.PROJECT) \
            or _param(req, name) is not None
    if name == "scope":
        if _param(req, "scope") not in (None, ""):
            return True
        return req.scope.kind not in (ScopeKind.UNKNOWN,)
    if name == "time_window" or name == "deadline":
        return bool(req.temporal and req.temporal.phrase) \
            or req.scope.kind not in (ScopeKind.UNKNOWN,)
    if name in ("title", "content"):
        return _param(req, name) is not None \
            or (req.target is not None and bool(req.target.name))
    if name == "duration":
        return _param(req, name) is not None
    if name == "event_types":
        return (_param(req, name) is not None
                or _param(req, "condition") is not None
                or _param(req, "events") is not None)
    if name == "persistence":
        # Q13: persistence must be explicitly resolved (ask, never assume).
        return _param(req, name) in ("always", "once")
    return _param(req, name) is not None


def missing_slots(req: AgentRequest) -> list[SlotSpec]:
    """Required slots with no value and no safe default, in schema order."""
    out: list[SlotSpec] = []
    for spec in required_slots(req.action.value):
        if slot_is_filled(req, spec):
            continue
        if spec.default is not None:
            continue
        out.append(spec)
    return out


def apply_slot(req: AgentRequest, name: str, value: Any) -> None:
    if value is None:
        return
    if name == "target":
        if isinstance(value, dict):
            req.target = EntityRef.from_dict(value)
            req.target.resolved = True
        else:
            req.target = EntityRef(type=EntityType.UNKNOWN, name=str(value))
        return
    if name == "project":
        req.target = EntityRef(type=EntityType.PROJECT, name=str(value))
        return
    if name == "scope":
        text = str(value).lower()
        req.parameters["scope"] = ("global" if "global" in text
                                   else "current_topic")
        req.scope = Scope(kind=ScopeKind.UNKNOWN)
        return
    if name in ("time_window", "deadline"):
        req.parameters[name] = value
        req.temporal = TemporalRange(phrase=str(value))
        return
    if name == "persistence":
        text = str(value).lower()
        req.parameters["persistence"] = (
            "once" if ("once" in text or "time" in text) else "always")
        return
    req.parameters[name] = value


# ---------------------------------------------------------------------------
# answer parsing (natural language + buttons)
# ---------------------------------------------------------------------------

_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2,
             "3rd": 2, "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}
_NEGATIVE = {"none", "none of these", "none of them", "cancel", "never mind",
             "nevermind", "no", "nope", "stop"}
_AFFIRM = {"yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "do it"}


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(
        ch if ch.isalnum() else " " for ch in (text or "").lower()).split()
        if len(t) > 1]


def parse_answer(text: str, clar: ClarificationRequest
                 ) -> tuple[Any, str] | None:
    """Return ``(value, option_id)`` for an answer, or None if unrecognised.

    Order: explicit option id, exact label, ordinal, qualifier/name match,
    negative, free text (when allowed).
    """
    raw = (text or "").strip()
    low = raw.lower()
    if not low:
        return None
    # explicit option id (button callback value)
    for o in clar.options:
        if low == o.option_id.lower():
            return o.value, o.option_id
    # exact label
    for o in clar.options:
        if low == o.label.lower():
            return o.value, o.option_id
    # negative / cancel
    if low in _NEGATIVE:
        return None, "__cancel"
    if clar.kind == ClarificationKind.CONFIRMATION.value and low in _AFFIRM:
        return True, "__yes"
    # ordinal
    toks = set(_tokens(low))
    for word, idx in _ORDINALS.items():
        if word in toks and 0 <= idx < len(clar.options):
            o = clar.options[idx]
            return o.value, o.option_id
    # qualifier/name match (targets): a distinguishing token unique to one
    # option is enough (e.g. "the CS188 one" -> "Project 2 (CS188)").
    _generic = {"project", "task", "course", "topic", "the", "one", "item",
                "this", "that", "other", "none", "of", "these"}
    hits = []
    for o in clar.options:
        label_tokens = set(_tokens(o.label))
        overlap = label_tokens & toks
        if not overlap:
            continue
        if label_tokens <= toks:
            hits.append(o)
        elif any(t not in _generic for t in overlap):
            hits.append(o)
    if len(hits) == 1:
        return hits[0].value, hits[0].option_id
    # numeric choice ("2")
    if low.isdigit():
        idx = int(low) - 1
        if 0 <= idx < len(clar.options):
            return clar.options[idx].value, clar.options[idx].option_id
    # free text (never for a target: it must be a real candidate or cancel)
    if clar.free_text_allowed and clar.kind != ClarificationKind.TARGET.value:
        return raw, ""
    return None


def explain(clar: ClarificationRequest) -> str:
    """A short, evidence-based reason for asking (never invented)."""
    if clar.reason:
        return clar.reason
    if clar.kind == ClarificationKind.TARGET.value and len(clar.options) > 2:
        n = len(clar.options) - 1  # exclude "None of these"
        return f"I found {n} matches and need to know which one you mean."
    if clar.options:
        return "I need one more detail before I can do that."
    return "I need a little more information."


# ---------------------------------------------------------------------------
# slot-text compatibility (Q6): a meta question must not fill a purpose slot
# ---------------------------------------------------------------------------

_PURPOSE_META: set[str] = set()  # structural fallback only; no phrase list


def classify_slot_text(chat: Any, text: str, *,
                       slot: str = "purpose") -> str:
    """Return ``"purpose" | "meta" | "vague"`` for text aimed at a slot.

    Primary path is semantic (the configured model). A tiny deterministic
    fallback (commands / obvious questions / obvious vagueness) is used only
    when no model is available. Not a natural-language regex router.
    """
    raw = (text or "").strip()
    if not raw:
        return "vague"
    if chat is not None and getattr(chat, "_llm_ready", lambda: False)():
        system = (
            "Classify a user's message sent while Butler is asking what a new "
            "Telegram topic is for. Reply ONLY JSON: "
            '{"kind":"purpose|meta|vague"}. '
            "purpose = it describes what the topic is for (e.g. a course, a "
            "project, a club, meal planning). "
            "meta = a general question about Butler's abilities, help, or how "
            "this works (e.g. 'what can you do?'). "
            "vague = too generic to be a purpose (e.g. 'stuff', 'everything').")
        try:
            out = chat.complete(system, raw, json_mode=True)
            if out:
                kind = json.loads(out).get("kind")
                if kind in ("purpose", "meta", "vague"):
                    return kind
        except Exception:  # noqa: BLE001 — fall through to the small fallback
            pass
    low = raw.lower().rstrip("?!. ")
    words = low.split()
    if raw.startswith("/"):
        return "meta"
    first = words[0] if words else ""
    if raw.endswith("?") or first in ("what", "how", "why", "can", "who"):
        return "meta"
    if len(words) < 2:
        return "vague"
    return "purpose"
