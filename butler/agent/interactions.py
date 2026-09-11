"""P2: explicit pending-interaction state.

Conversation follow-ups must not rely on the model's memory. A clarification
("which Project 2?") and a pending proposal ("track CS188 — confirm?") are
first-class, expiring state objects. Resolution precedence is deterministic:

    1. active clarification   (resolve against stored candidates FIRST)
    2. active confirmation    (a follow-up may modify the pending proposal)
    3. current topic
    4. recent entities
    5. recent conversation
    6. bounded global lookup
    7. ask

Stale state never applies: interactions expire and are purged on access.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .semantic import EntityRef


class InteractionStatus(str, Enum):
    ACTIVE = "active"
    WAITING_FOR_CLARIFICATION = "waiting_for_clarification"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class InteractionKind(str, Enum):
    CLARIFICATION = "clarification"
    CONFIRMATION = "confirmation"


DEFAULT_TTL = 900  # 15 minutes


@dataclass
class Interaction:
    id: str
    kind: str
    status: str
    original_text: str = ""
    request: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    proposal: dict[str, Any] = field(default_factory=dict)
    expected_slot: str = ""
    ambiguity_type: str = ""
    created_at: int = 0
    expires_at: int = 0

    def is_expired(self, now: int | None = None) -> bool:
        now = int(now if now is not None else time.time())
        return bool(self.expires_at) and now >= self.expires_at

    def is_open(self, now: int | None = None) -> bool:
        return self.status in (InteractionStatus.ACTIVE,
                               InteractionStatus.WAITING_FOR_CLARIFICATION,
                               InteractionStatus.WAITING_FOR_CONFIRMATION) \
            and not self.is_expired(now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "original_text": self.original_text, "request": self.request,
            "candidates": self.candidates, "proposal": self.proposal,
            "expected_slot": self.expected_slot,
            "ambiguity_type": self.ambiguity_type,
            "created_at": self.created_at, "expires_at": self.expires_at,
        }


class InteractionStore:
    """Bounded, expiring pending-interaction state attached to a session."""

    _KEY = "interactions"

    def __init__(self, ttl: int = DEFAULT_TTL, max_open: int = 4):
        self.ttl = int(ttl)
        self.max_open = int(max_open)

    # ------------------------------------------------------------- helpers
    def _all(self, session: Any) -> list[Interaction]:
        store = getattr(session, "data", None)
        if store is None:
            return []
        items = store.setdefault(self._KEY, [])
        return items

    def purge_expired(self, session: Any,
                      now: int | None = None) -> list[Interaction]:
        now = int(now if now is not None else time.time())
        items = self._all(session)
        expired = [i for i in items if i.is_expired(now)]
        for i in expired:
            i.status = InteractionStatus.EXPIRED
        if expired:
            session.data[self._KEY] = [i for i in items if not i.is_expired(now)]
        return expired

    def active(self, session: Any, kind: str | None = None,
               now: int | None = None) -> Interaction | None:
        now = int(now if now is not None else time.time())
        open_items = [i for i in self._all(session)
                      if i.is_open(now) and (kind is None or i.kind == kind)]
        if not open_items:
            return None
        return max(open_items, key=lambda i: i.created_at)

    def open(self, session: Any, *, kind: str, original_text: str = "",
             request: dict[str, Any] | None = None,
             candidates: list[dict[str, Any]] | None = None,
             proposal: dict[str, Any] | None = None,
             expected_slot: str = "", ambiguity_type: str = "",
             ttl: int | None = None, now: int | None = None) -> Interaction:
        now = int(now if now is not None else time.time())
        self.purge_expired(session, now)
        status = InteractionStatus.WAITING_FOR_CLARIFICATION \
            if kind == InteractionKind.CLARIFICATION \
            else InteractionStatus.WAITING_FOR_CONFIRMATION
        item = Interaction(
            id=uuid.uuid4().hex[:12], kind=kind, status=status,
            original_text=original_text, request=dict(request or {}),
            candidates=list(candidates or []), proposal=dict(proposal or {}),
            expected_slot=expected_slot, ambiguity_type=ambiguity_type,
            created_at=now, expires_at=now + int(ttl or self.ttl))
        items = self._all(session)
        items.append(item)
        # bound the number of open interactions
        open_items = [i for i in items if i.is_open(now)]
        if len(open_items) > self.max_open:
            for stale in open_items[:-self.max_open]:
                stale.status = InteractionStatus.CANCELLED
        session.data[self._KEY] = [i for i in items if i.is_open(now)]
        return item

    def close(self, session: Any, item: Interaction | None,
              status: str = InteractionStatus.COMPLETED) -> None:
        if item is None:
            return
        item.status = status
        session.data[self._KEY] = [i for i in self._all(session)
                                   if i.id != item.id]


# ---------------------------------------------------------------------------
# deterministic candidate matching (no global search first)
# ---------------------------------------------------------------------------


def candidate_label(candidate: dict[str, Any]) -> str:
    label = str(candidate.get("label") or candidate.get("name") or "")
    extra = candidate.get("qualifiers") or []
    if extra:
        label = label + " " + " ".join(str(x) for x in extra)
    return label.lower()


def _tokens(text: str) -> list[str]:
    return [t for t in "".join(
        ch if ch.isalnum() else " " for ch in (text or "").lower()).split()
        if len(t) > 1]


def match_candidates(text: str, candidates: list[dict[str, Any]]
                     ) -> dict[str, Any] | None:
    """Return the single candidate the follow-up refers to, else None.

    Deterministic precedence: a unique distinguishing *qualifier* match wins
    (e.g. "the CS188 one" against "Project 2 (CS188)"), then a unique full-name
    match, then an ordinal ("the second one"). Never a global search.
    """
    toks = set(_tokens(text))
    if not toks or not candidates:
        return None
    qual_hits: list[dict[str, Any]] = []
    name_hits: list[dict[str, Any]] = []
    for c in candidates:
        name_tokens = set(_tokens(str(c.get("name", ""))))
        qual_tokens: set[str] = set()
        for q in (c.get("qualifiers") or []):
            qual_tokens |= set(_tokens(str(q)))
        if qual_tokens and qual_tokens <= toks:
            qual_hits.append(c)
        elif name_tokens and name_tokens <= toks:
            name_hits.append(c)
    if len(qual_hits) == 1:
        return qual_hits[0]
    if len(name_hits) == 1:
        return name_hits[0]
    ordinals = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2,
                "3rd": 2, "last": len(candidates) - 1}
    for word, idx in ordinals.items():
        if word in toks and 0 <= idx < len(candidates):
            return candidates[idx]
    return None


def ref_from_candidate(candidate: dict[str, Any]) -> EntityRef:
    ref = EntityRef.from_dict(candidate)
    # A candidate came from live state, so it is resolved by construction.
    ref.resolved = True
    if not ref.confidence:
        ref.confidence = float(candidate.get("confidence") or 0.9)
    ref.source = ref.source or "interaction_candidate"
    return ref
