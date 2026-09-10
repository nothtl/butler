"""M6: long-term memory + learning.

Butler already had three adjacent pieces of infrastructure:

* :mod:`butler.timeline` — an append-only, zone-only event trail;
* :mod:`butler.routines` — a deterministic, *soft* routine detector;
* :mod:`butler.agent.session` — bounded, in-process conversation state.

M6 adds the missing durable, typed, provenance-aware **memory store**. It does
not replace any of the above: it reuses the timeline as evidence, mirrors
confirmed routines instead of running a second detector, uses the same
:class:`~butler.audit.Audit` redaction for mutations, and keeps the conversation
session in memory only.

Design principles:

* **The LLM may propose; deterministic code decides.** Every write passes through
  :class:`MemoryWriteGate`, which owns provenance, confidence, scope, expiry,
  duplication, contradiction and secret rejection.
* **Inferred is never authoritative.** Inferred memories (routine/llm) can never
  become hard constraints and can never override an explicit memory.
* **Trust is explicit.** :data:`TRUST` ranks provenance; retrieval ranks by it.
* **History is kept.** Superseding deactivates the old row (``active=0``) but
  never deletes it.
* **Staleness, not deletion.** Different types age differently; stale rows are
  marked, not dropped.
* **Bounded retrieval.** Only a capped, filtered candidate set is ever scored.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import affinity

log = logging.getLogger("butler.memory")

# ---------------------------------------------------------------------------
# memory types
# ---------------------------------------------------------------------------
CORE_FACT = "core_fact"
PREFERENCE = "preference"
ROUTINE = "routine"
PROJECT_FACT = "project_fact"
COURSE_FACT = "course_fact"
EPISODIC_EVENT = "episodic_event"
TEMPORAL_NOTE = "temporal_note"
USER_INSTRUCTION = "user_instruction"

TYPES: tuple[str, ...] = (
    CORE_FACT, PREFERENCE, ROUTINE, PROJECT_FACT, COURSE_FACT,
    EPISODIC_EVENT, TEMPORAL_NOTE, USER_INSTRUCTION,
)

# ---------------------------------------------------------------------------
# provenance + trust
# ---------------------------------------------------------------------------
EXPLICIT_USER = "explicit_user"
USER_CONFIRMED = "user_confirmed"
CALENDAR_OBSERVED = "calendar_observed"
TASK_OBSERVED = "task_observed"
COURSE_OBSERVED = "course_observed"
PROJECT_OBSERVED = "project_observed"
WEB_VERIFIED = "web_verified"
ROUTINE_INFERRED = "routine_inferred"
LLM_INFERRED = "llm_inferred"

PROVENANCE: tuple[str, ...] = (
    EXPLICIT_USER, USER_CONFIRMED, CALENDAR_OBSERVED, TASK_OBSERVED,
    COURSE_OBSERVED, PROJECT_OBSERVED, WEB_VERIFIED, ROUTINE_INFERRED,
    LLM_INFERRED,
)

#: Provenance -> trust weight (0..1). Highest is an explicit user statement.
TRUST: dict[str, float] = {
    EXPLICIT_USER: 1.0,
    USER_CONFIRMED: 0.95,
    CALENDAR_OBSERVED: 0.8,
    TASK_OBSERVED: 0.8,
    COURSE_OBSERVED: 0.8,
    PROJECT_OBSERVED: 0.8,
    WEB_VERIFIED: 0.7,
    ROUTINE_INFERRED: 0.4,
    LLM_INFERRED: 0.3,
}

#: Provenances that are inferred (never hard, never override explicit).
INFERRED_PROVENANCE: frozenset[str] = frozenset({
    ROUTINE_INFERRED, LLM_INFERRED,
})

#: Provenances that make a memory "confirmed" without further user action.
OBSERVED_PROVENANCE: frozenset[str] = frozenset({
    CALENDAR_OBSERVED, TASK_OBSERVED, COURSE_OBSERVED, PROJECT_OBSERVED,
    WEB_VERIFIED,
})

# ---------------------------------------------------------------------------
# confirmation states
# ---------------------------------------------------------------------------
UNCONFIRMED = "unconfirmed"
CONFIRMED = "confirmed"
INFERRED = "inferred"
REJECTED = "rejected"
STALE = "stale"

STATES: tuple[str, ...] = (UNCONFIRMED, CONFIRMED, INFERRED, REJECTED, STALE)

SCOPE_PERSONAL = "personal"
SCOPE_EXTERNAL = "external"

#: Per-type default staleness window in days (0 = never goes stale).
STALE_DAYS: dict[str, int] = {
    CORE_FACT: 0,
    PREFERENCE: 0,
    ROUTINE: 21,
    PROJECT_FACT: 30,
    COURSE_FACT: 120,
    EPISODIC_EVENT: 0,
    TEMPORAL_NOTE: 7,
    USER_INSTRUCTION: 0,
}

_COURSE_RE = re.compile(r"\b([A-Za-z]{2,6}\s?\d{2,4}[A-Za-z]?)\b")
_HOURS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", re.I)
_MINUTES_RE = re.compile(r"\b(\d+)\s*(?:minutes?|mins?|m)\b", re.I)

# Secret patterns: never store credentials of any kind.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token|bearer|"
               r"client[_-]?secret|private[_-]?key|access[_-]?token)\b\s*[:=]"),
    re.compile(r"(?i)\bauthorization\s*:"),
)

# Instruction-like text that must never be imported from untrusted content.
_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore (all )?(previous|prior|above) instructions"),
    re.compile(r"(?i)disregard (all )?(previous|prior|above)"),
    re.compile(r"(?i)you are now\b"),
    re.compile(r"(?i)\bsystem prompt\b"),
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bdelete (all|every) (tasks|files|memories)\b"),
)


def contains_secret(text: str) -> bool:
    text = text or ""
    return any(p.search(text) for p in _SECRET_PATTERNS)


def contains_injection(text: str) -> bool:
    text = text or ""
    return any(p.search(text) for p in _INJECTION_PATTERNS)


# ---------------------------------------------------------------------------
# serialisation
# ---------------------------------------------------------------------------
def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(t) > 2]


def _norm_tags(tags: Any) -> str:
    if not tags:
        return ""
    if isinstance(tags, str):
        parts = [t.strip().lower() for t in re.split(r"[,\s]+", tags) if t.strip()]
    else:
        parts = [str(t).strip().lower() for t in tags if str(t).strip()]
    return ",".join(sorted(set(parts)))


@dataclass
class MemoryRecord:
    id: int = 0
    type: str = CORE_FACT
    subject: str = ""
    key: str = ""
    value: str = ""
    source: str = ""
    source_detail: str = ""
    confidence: float = 0.0
    provenance: str = ""
    created_at: int = 0
    updated_at: int = 0
    observed_at: int = 0
    expires_at: int = 0
    last_confirmed_at: int = 0
    confirmation_state: str = UNCONFIRMED
    scope: str = SCOPE_PERSONAL
    tags: list[str] = field(default_factory=list)
    active: bool = True
    supersedes_id: int = 0
    superseded_by: int = 0
    usage_count: int = 0
    last_used_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["active"] = bool(self.active)
        return d

    @classmethod
    def from_row(cls, row: Any) -> "MemoryRecord":
        return cls(
            id=int(row["id"]), type=str(row["type"]),
            subject=str(row["subject"] or ""), key=str(row["key"] or ""),
            value=str(row["value"] or ""), source=str(row["source"] or ""),
            source_detail=str(row["source_detail"] or ""),
            confidence=float(row["confidence"] or 0.0),
            provenance=str(row["provenance"] or ""),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
            observed_at=int(row["observed_at"] or 0),
            expires_at=int(row["expires_at"] or 0),
            last_confirmed_at=int(row["last_confirmed_at"] or 0),
            confirmation_state=str(row["confirmation_state"] or UNCONFIRMED),
            scope=str(row["scope"] or SCOPE_PERSONAL),
            tags=[t for t in str(row["tags"] or "").split(",") if t],
            active=bool(row["active"]),
            supersedes_id=int(row["supersedes_id"] or 0),
            superseded_by=int(row["superseded_by"] or 0),
            usage_count=int(row["usage_count"] or 0),
            last_used_at=int(row["last_used_at"] or 0),
        )


@dataclass
class GateDecision:
    allow: bool
    action: str = "reject"          # reject | insert | merge | supersede
    reason: str = ""
    confirmation_state: str = ""
    supersede_id: int = 0
    record: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "action": self.action,
                "reason": self.reason,
                "confirmation_state": self.confirmation_state,
                "supersede_id": self.supersede_id}


# ---------------------------------------------------------------------------
# write gate
# ---------------------------------------------------------------------------
class MemoryWriteGate:
    """The single deterministic gate every memory write must pass through."""

    def __init__(self, memory: "Memory"):
        self.memory = memory
        self.cfg = memory.cfg

    def _min_conf(self) -> float:
        return float(getattr(self.cfg, "memory_min_confidence", 0.35))

    def _max_chars(self) -> int:
        return int(getattr(self.cfg, "memory_max_value_chars", 2000))

    def _ttl_days(self, mtype: str) -> int:
        base = STALE_DAYS.get(mtype, 0)
        if mtype == TEMPORAL_NOTE:
            return int(getattr(self.cfg, "memory_temporal_ttl_days",
                               base or 7))
        return base

    def evaluate(self, cand: dict[str, Any], *,
                 existing: MemoryRecord | None = None,
                 now: int | None = None) -> GateDecision:
        now = int(now if now is not None else self.memory._now())
        mtype = str(cand.get("type", "")).strip()
        provenance = str(cand.get("provenance", "")).strip()
        subject = str(cand.get("subject", "") or "")
        key = str(cand.get("key", "") or "")
        value = str(cand.get("value", "") or "")
        source_detail = str(cand.get("source_detail", "") or "")
        scope = str(cand.get("scope", SCOPE_PERSONAL) or SCOPE_PERSONAL)

        if not self.memory.enabled():
            return GateDecision(False, reason="memory disabled")
        if mtype not in TYPES:
            return GateDecision(False, reason=f"unknown memory type {mtype!r}")
        if provenance not in PROVENANCE:
            return GateDecision(False, reason=f"unknown provenance {provenance!r}")
        if not value.strip():
            return GateDecision(False, reason="empty value")

        # --- privacy: never store credentials -----------------------------
        blob = " ".join((subject, key, value, source_detail))
        if contains_secret(blob):
            return GateDecision(False, reason="contains a secret/credential")

        # --- untrusted content cannot become an instruction ----------------
        if provenance in (WEB_VERIFIED, LLM_INFERRED) and contains_injection(value):
            return GateDecision(False, reason="instruction-like untrusted content")
        if provenance == WEB_VERIFIED:
            if scope == SCOPE_PERSONAL:
                return GateDecision(
                    False, reason="web facts must stay external evidence")
            if mtype in (USER_INSTRUCTION, CORE_FACT):
                return GateDecision(
                    False, reason="web content cannot become a personal "
                                  "instruction or core fact")

        # --- size ---------------------------------------------------------
        if len(value) > self._max_chars():
            return GateDecision(False, reason="value too large to remember")

        # --- confidence / inferred restraint ------------------------------
        confidence = float(cand.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        if provenance in INFERRED_PROVENANCE and confidence < self._min_conf():
            return GateDecision(
                False, reason=f"inferred confidence {confidence:.2f} below "
                              f"{self._min_conf():.2f}")

        # --- confirmation state -------------------------------------------
        if provenance in (EXPLICIT_USER, USER_CONFIRMED):
            state = CONFIRMED
        elif provenance in OBSERVED_PROVENANCE:
            state = CONFIRMED
        else:
            state = INFERRED
        if str(cand.get("confirmation_state", "") or "") in STATES:
            state = str(cand["confirmation_state"])

        # --- expiry -------------------------------------------------------
        expires_at = int(cand.get("expires_at", 0) or 0)
        if mtype == TEMPORAL_NOTE and not expires_at:
            expires_at = now + self._ttl_days(mtype) * 86400
        if expires_at and expires_at <= now:
            return GateDecision(False, reason="already expired")

        # --- duplicate / contradiction ------------------------------------
        same = existing or self.memory._active_by_key(mtype, subject, key)
        if same is not None:
            if same.value.strip().lower() == value.strip().lower():
                return GateDecision(True, action="merge",
                                    reason="duplicate; refreshing",
                                    confirmation_state=state)
            if provenance in INFERRED_PROVENANCE \
                    and same.confirmation_state == CONFIRMED:
                return GateDecision(
                    False, reason="inferred memory cannot override a "
                                  "confirmed one", supersede_id=same.id)
            if provenance in INFERRED_PROVENANCE \
                    and same.provenance in (EXPLICIT_USER, USER_CONFIRMED):
                return GateDecision(
                    False, reason="inferred memory cannot override explicit "
                                  "user intent", supersede_id=same.id)
            return GateDecision(True, action="supersede",
                                reason="newer value supersedes the old one",
                                confirmation_state=state, supersede_id=same.id)

        return GateDecision(True, action="insert", reason="accepted",
                            confirmation_state=state)

    # ------------------------------------------------------------------
    def apply(self, cand: dict[str, Any], decision: GateDecision,
              *, now: int | None = None) -> MemoryRecord | None:
        now = int(now if now is not None else self.memory._now())
        if not decision.allow:
            self.memory._audit("memory_rejected", target=str(cand.get("key", "")),
                               detail={"reason": decision.reason, **cand})
            return None
        if decision.action == "merge":
            same = self.memory._active_by_key(
                str(cand["type"]), str(cand.get("subject", "")),
                str(cand.get("key", "")))
            if same is not None:
                self.memory.db.execute(
                    "UPDATE memories SET updated_at=?, observed_at=?, "
                    "last_confirmed_at=?, confidence=?, active=1 WHERE id=?",
                    (now, now, now,
                     max(same.confidence, float(cand.get("confidence", 0.0))),
                     same.id))
                self.memory._add_evidence(same.id, cand, now)
                self.memory._audit("memory_refresh", target=same.key,
                                   detail={"id": same.id, **cand})
                return self.memory.get(same.id)
        supersede_id = int(decision.supersede_id or 0)
        if decision.action == "supersede" and supersede_id:
            self.memory.db.execute(
                "UPDATE memories SET active=0, superseded_by=?, updated_at=? "
                "WHERE id=?", (0, now, supersede_id))
            # superseded_by is filled after insert; store intent on the new row
        record = self.memory._insert(cand, decision.confirmation_state,
                                     supersede_id if decision.action == "supersede"
                                     else 0, now)
        if supersede_id:
            self.memory.db.execute(
                "UPDATE memories SET superseded_by=? WHERE id=?",
                (record.id, supersede_id))
        self.memory._audit(
            "memory_supersede" if supersede_id else "memory_insert",
            target=record.key,
            detail={"id": record.id, "type": record.type,
                    "subject": record.subject, "value": record.value,
                    "provenance": record.provenance,
                    "confidence": record.confidence,
                    "supersedes_id": supersede_id})
        return record


# ---------------------------------------------------------------------------
# the memory store
# ---------------------------------------------------------------------------
class Memory:
    """Durable, typed, provenance-aware memory with deterministic retrieval."""

    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db = getattr(container, "db", None)
        self.audit = getattr(container, "audit", None)
        self.routines = getattr(container, "routines", None)
        self.timeline = getattr(container, "timeline", None)
        self.web = getattr(container, "web", None)
        self.gate = MemoryWriteGate(self)

    # ------------------------------------------------------------------ time
    def _now(self) -> int:
        cfg = self.cfg
        if cfg is not None and hasattr(cfg, "now_local"):
            try:
                return int(cfg.now_local().timestamp())
            except Exception:  # noqa: BLE001
                pass
        return int(time.time())

    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "memory_enabled", True)) and self.db is not None

    def _audit(self, action: str, *, target: str = "",
               detail: Any = None, decision: str = "allowed") -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action, actor="memory", target=target,
                              decision=decision, reason="",
                              outcome="ok" if decision == "allowed" else "not_applied",
                              detail=detail or {})
        except Exception:  # noqa: BLE001 — audit must never break memory
            log.debug("memory audit failed", exc_info=True)

    # ------------------------------------------------------------- low-level
    def _active_by_key(self, mtype: str, subject: str,
                       key: str) -> MemoryRecord | None:
        if not key:
            return None
        row = self.db.one(
            "SELECT * FROM memories WHERE active=1 AND type=? AND subject=? "
            "AND key=? ORDER BY updated_at DESC, id DESC LIMIT 1",
            (mtype, subject or "", key))
        return MemoryRecord.from_row(row) if row else None

    def _insert(self, cand: dict[str, Any], state: str,
                supersedes_id: int, now: int) -> MemoryRecord:
        mtype = str(cand["type"])
        expires_at = int(cand.get("expires_at", 0) or 0)
        if mtype == TEMPORAL_NOTE and not expires_at:
            expires_at = now + int(getattr(self.cfg, "memory_temporal_ttl_days", 7)) * 86400
        cur = self.db.execute(
            "INSERT INTO memories(type,subject,key,value,source,source_detail,"
            "confidence,provenance,created_at,updated_at,observed_at,expires_at,"
            "last_confirmed_at,confirmation_state,scope,tags,active,supersedes_id,"
            "superseded_by,usage_count,last_used_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mtype, str(cand.get("subject", "") or ""),
             str(cand.get("key", "") or ""), str(cand.get("value", "") or ""),
             str(cand.get("source", "") or ""),
             str(cand.get("source_detail", "") or ""),
             float(cand.get("confidence", 0.0) or 0.0),
             str(cand.get("provenance", "") or ""),
             now, now, int(cand.get("observed_at", 0) or now), expires_at,
             now if state == CONFIRMED else 0, state,
             str(cand.get("scope", SCOPE_PERSONAL) or SCOPE_PERSONAL),
             _norm_tags(cand.get("tags")), 1, int(supersedes_id or 0), 0, 0, 0))
        mid = int(cur.lastrowid)
        self._add_evidence(mid, cand, now)
        return self.get(mid)

    def _add_evidence(self, memory_id: int, cand: dict[str, Any],
                      now: int) -> None:
        for ev in cand.get("evidence") or []:
            self.db.execute(
                "INSERT INTO memory_evidence(memory_id,kind,ref,detail,"
                "observed_at,created_at) VALUES(?,?,?,?,?,?)",
                (int(memory_id), str(ev.get("kind", "")),
                 str(ev.get("ref", "")), str(ev.get("detail", "")),
                 int(ev.get("observed_at", now) or now), now))

    def get(self, memory_id: int) -> MemoryRecord | None:
        row = self.db.one("SELECT * FROM memories WHERE id=?", (int(memory_id),))
        return MemoryRecord.from_row(row) if row else None

    # --------------------------------------------------------------- writes
    def remember(self, *, type: str = CORE_FACT, subject: str = "",
                 key: str = "", value: str = "",
                 provenance: str = EXPLICIT_USER, confidence: float = 0.9,
                 source: str = "user", source_detail: str = "",
                 scope: str = SCOPE_PERSONAL, tags: Any = None,
                 observed_at: int = 0, expires_at: int = 0,
                 evidence: list[dict[str, Any]] | None = None,
                 confirmation_state: str = "",
                 now: int | None = None) -> dict[str, Any]:
        """Explicit/observed write through the gate. Returns a result dict."""
        now = int(now if now is not None else self._now())
        cand = {"type": type, "subject": subject, "key": key, "value": value,
                "provenance": provenance, "confidence": confidence,
                "source": source, "source_detail": source_detail,
                "scope": scope, "tags": tags, "observed_at": observed_at,
                "expires_at": expires_at, "evidence": evidence or [],
                "confirmation_state": confirmation_state}
        decision = self.gate.evaluate(cand, now=now)
        if not decision.allow:
            return {"ok": False, "reason": decision.reason, "stored": None,
                    "action": "reject"}
        record = self.gate.apply(cand, decision, now=now)
        return {"ok": True, "reason": decision.reason,
                "action": decision.action,
                "stored": record.to_dict() if record else None}

    def observe(self, *, type: str, subject: str = "", key: str = "",
                value: str = "", provenance: str = ROUTINE_INFERRED,
                confidence: float = 0.5, source: str = "observation",
                source_detail: str = "", tags: Any = None,
                evidence: list[dict[str, Any]] | None = None,
                now: int | None = None) -> dict[str, Any]:
        """Inferred write. Always stored as inferred/unconfirmed, never hard."""
        return self.remember(type=type, subject=subject, key=key, value=value,
                             provenance=provenance, confidence=confidence,
                             source=source, source_detail=source_detail,
                             tags=tags, evidence=evidence,
                             confirmation_state=INFERRED, now=now)

    def remember_explicit(self, text: str, *,
                          now: int | None = None) -> dict[str, Any]:
        """Parse a natural-language explicit statement and store it."""
        now = int(now if now is not None else self._now())
        cand = self.parse_explicit(text)
        if cand is None:
            return {"ok": False, "reason": "could not parse a memory", "stored": None}
        cand["evidence"] = [{"kind": "user_statement", "ref": "",
                             "detail": text[:200], "observed_at": now}]
        decision = self.gate.evaluate(cand, now=now)
        if not decision.allow:
            return {"ok": False, "reason": decision.reason, "stored": None,
                    "action": "reject"}
        record = self.gate.apply(cand, decision, now=now)
        return {"ok": True, "reason": decision.reason,
                "action": decision.action,
                "stored": record.to_dict() if record else None}

    def remember_web(self, *, subject: str, key: str, value: str,
                     source_url: str, type: str = COURSE_FACT,
                     observed_at: int = 0, expires_at: int = 0,
                     confidence: float = 0.7,
                     now: int | None = None) -> dict[str, Any]:
        """Store an external, web-derived fact (never a personal instruction)."""
        if not bool(getattr(self.cfg, "memory_allow_web_facts", True)):
            return {"ok": False, "reason": "web facts disabled", "stored": None}
        now = int(now if now is not None else self._now())
        if type in (USER_INSTRUCTION, CORE_FACT):
            type = COURSE_FACT
        return self.remember(
            type=type, subject=subject, key=key, value=value,
            provenance=WEB_VERIFIED, confidence=confidence,
            source="web", source_detail=source_url, scope=SCOPE_EXTERNAL,
            tags=["web"], observed_at=observed_at or now, expires_at=expires_at,
            evidence=[{"kind": "web_source", "ref": source_url,
                       "detail": value[:200], "observed_at": observed_at or now}],
            now=now)

    # ------------------------------------------------------------ lifecycle
    def confirm(self, ident: Any = None, *, now: int | None = None) -> dict[str, Any]:
        """Promote an inferred/unconfirmed memory to confirmed (explicit only)."""
        now = int(now if now is not None else self._now())
        rec = self._resolve_one(ident)
        if rec is None:
            return {"ok": False, "error": "no memory matched"}
        if rec.confirmation_state == CONFIRMED:
            return {"ok": True, "memory": rec.to_dict(), "already": True}
        self.db.execute(
            "UPDATE memories SET confirmation_state=?, last_confirmed_at=?, "
            "updated_at=?, provenance=?, confidence=? WHERE id=?",
            (CONFIRMED, now, now, USER_CONFIRMED,
             max(rec.confidence, 0.9), rec.id))
        self._audit("memory_confirm", target=rec.key,
                    detail={"id": rec.id, "old_state": rec.confirmation_state,
                            "new_state": CONFIRMED})
        return {"ok": True, "memory": self.get(rec.id).to_dict()}

    def forget(self, ident: Any = None, *, now: int | None = None) -> dict[str, Any]:
        """Deactivate a memory (history is retained). Ambiguity is refused."""
        now = int(now if now is not None else self._now())
        matches = self._resolve(ident)
        if not matches:
            return {"ok": False, "error": "no memory matched", "forgotten": 0}
        if len(matches) > 1:
            return {"ok": False, "error": "ambiguous", "ambiguous": True,
                    "candidates": [m.to_dict() for m in matches[:6]],
                    "forgotten": 0}
        rec = matches[0]
        self.db.execute(
            "UPDATE memories SET active=0, confirmation_state=?, updated_at=? "
            "WHERE id=?", (STALE, now, rec.id))
        self._audit("memory_forget", target=rec.key,
                    detail={"id": rec.id, "value": rec.value,
                            "provenance": rec.provenance})
        return {"ok": True, "forgotten": 1, "memory": self.get(rec.id).to_dict()}

    def correct(self, text: str, *, now: int | None = None) -> dict[str, Any]:
        """Store a corrected explicit memory; the old value is superseded."""
        return self.remember_explicit(text, now=now)

    def reject(self, ident: Any = None, *, now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        matches = self._resolve(ident)
        if not matches:
            return {"ok": False, "error": "no memory matched"}
        rec = matches[0]
        self.db.execute(
            "UPDATE memories SET confirmation_state=?, active=0, updated_at=? "
            "WHERE id=?", (REJECTED, now, rec.id))
        self._audit("memory_reject", target=rec.key, detail={"id": rec.id})
        return {"ok": True, "memory": self.get(rec.id).to_dict()}

    # ------------------------------------------------------------- resolve
    def _resolve(self, ident: Any) -> list[MemoryRecord]:
        if ident is None:
            return []
        if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
            rec = self.get(int(ident))
            return [rec] if rec else []
        if isinstance(ident, MemoryRecord):
            return [ident]
        if isinstance(ident, dict):
            if ident.get("id"):
                rec = self.get(int(ident["id"]))
                return [rec] if rec else []
            ident = ident.get("query") or ident.get("value") or ""
        q = str(ident).strip().lower()
        if not q:
            return []
        tokens = _tokens(q)
        rows = self.db.query(
            "SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC "
            "LIMIT ?", (int(getattr(self.cfg, "memory_max_scan", 500)),))
        out = []
        for r in rows:
            rec = MemoryRecord.from_row(r)
            hay = f"{rec.subject} {rec.key} {rec.value} {' '.join(rec.tags)}".lower()
            if q in hay:
                out.append(rec)
                continue
            if tokens:
                words = set(re.findall(r"[a-z0-9]+", hay))
                hits = sum(1 for t in tokens
                           if t in hay or any(
                               (w.startswith(t) or t.startswith(w))
                               for w in words if len(w) >= 3))
                if hits >= max(1, int(0.6 * len(tokens) + 0.999)):
                    out.append(rec)
        return out

    def _resolve_one(self, ident: Any) -> MemoryRecord | None:
        matches = self._resolve(ident)
        return matches[0] if matches else None

    # ------------------------------------------------------------ retrieval
    def _row_to_record(self, row: Any) -> MemoryRecord:
        return MemoryRecord.from_row(row)

    def _expired(self, rec: MemoryRecord, now: int) -> bool:
        return bool(rec.expires_at and rec.expires_at <= now)

    def _score(self, rec: MemoryRecord, tokens: list[str], now: int,
               subject_hint: str = "") -> float:
        hay = f"{rec.subject} {rec.key} {rec.value} {' '.join(rec.tags)}".lower()
        overlap = sum(1 for t in tokens if t in hay) if tokens else 0
        base = (overlap / len(tokens)) if tokens else 0.0
        recency = self._recency(rec, now)
        trust = TRUST.get(rec.provenance, 0.3)
        subj = 0.2 if (subject_hint and subject_hint.lower() in
                       f"{rec.subject} {rec.value}".lower()) else 0.0
        score = (0.45 * base + 0.2 * recency + 0.2 * rec.confidence
                 + 0.15 * trust + subj)
        return round(score, 4)

    def _recency(self, rec: MemoryRecord, now: int) -> float:
        ts = rec.last_confirmed_at or rec.observed_at or rec.updated_at or rec.created_at
        age_days = max(0.0, (now - ts) / 86400.0)
        return round(1.0 / (1.0 + age_days / 30.0), 4)

    def search(self, query: str, *, types: list[str] | None = None,
               active_only: bool = True, include_stale: bool = False,
               scope: str = "", subject: str = "",
               limit: int | None = None, now: int | None = None
               ) -> list[dict[str, Any]]:
        now = int(now if now is not None else self._now())
        limit = int(limit or getattr(self.cfg, "memory_search_limit", 8))
        tokens = _tokens(query)
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if active_only:
            sql += " AND active=1"
        if types:
            marks = ",".join("?" for _ in types)
            sql += f" AND type IN ({marks})"
            params += list(types)
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(getattr(self.cfg, "memory_max_scan", 500)))
        rows = self.db.query(sql, tuple(params))
        out = []
        for r in rows:
            rec = MemoryRecord.from_row(r)
            if self._expired(rec, now):
                continue
            if rec.confirmation_state == STALE and not include_stale:
                continue
            if tokens:
                hay = f"{rec.subject} {rec.key} {rec.value} " \
                      f"{' '.join(rec.tags)}".lower()
                if not any(t in hay for t in tokens):
                    continue
            score = self._score(rec, tokens, now, subject_hint=subject)
            if tokens and score <= 0.15:
                continue
            d = rec.to_dict()
            d["score"] = score
            out.append(d)
        out.sort(key=lambda d: (-d["score"], -d["confidence"], -d["updated_at"]))
        return out[:limit]

    def get_relevant(self, context: dict[str, Any] | None = None, *,
                     limit: int | None = None,
                     now: int | None = None) -> list[dict[str, Any]]:
        """Bounded retrieval for a request context (never a full dump)."""
        context = context or {}
        limit = int(limit or getattr(self.cfg, "memory_context_limit", 5))
        subject = str(context.get("subject", "") or "")
        query = str(context.get("query", "") or "")
        entities = context.get("entities") or []
        tags = context.get("tags") or []
        terms = [subject, query] + [str(e) for e in entities] + [str(t) for t in tags]
        combined = " ".join(t for t in terms if t)
        if not combined.strip():
            return []
        return self.search(combined, types=context.get("types"),
                           scope=str(context.get("scope", "") or ""),
                           subject=subject, limit=limit, now=now)

    def list(self, *, types: list[str] | None = None,
             state: str = "", active_only: bool = False,
             scope: str = "", limit: int = 50,
             now: int | None = None) -> list[dict[str, Any]]:
        now = int(now if now is not None else self._now())
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if types:
            marks = ",".join("?" for _ in types)
            sql += f" AND type IN ({marks})"
            params += list(types)
        if state:
            sql += " AND confirmation_state=?"
            params.append(state)
        if active_only:
            sql += " AND active=1"
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [MemoryRecord.from_row(r).to_dict()
                for r in self.db.query(sql, tuple(params))]

    def history(self, *, subject: str = "", key: str = "",
                limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []
        if subject:
            sql += " AND subject=?"
            params.append(subject)
        if key:
            sql += " AND key=?"
            params.append(key)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [MemoryRecord.from_row(r).to_dict()
                for r in self.db.query(sql, tuple(params))]

    def explain(self, ident: Any = None, *, now: int | None = None
                ) -> dict[str, Any] | None:
        now = int(now if now is not None else self._now())
        rec = self._resolve_one(ident)
        if rec is None and not isinstance(ident, (int, MemoryRecord)):
            query = str(ident or "")
            rows = self.search(query, limit=1, now=now)
            if rows:
                rec = self.get(rows[0]["id"])
        if rec is None:
            return None
        evidence = [dict(r) for r in self.db.query(
            "SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY id",
            (rec.id,))]
        chain = []
        cur = rec
        seen: set[int] = set()
        while cur is not None and cur.supersedes_id and cur.supersedes_id not in seen:
            seen.add(cur.id)
            prev = self.get(cur.supersedes_id)
            if prev is None:
                break
            chain.append({"id": prev.id, "value": prev.value,
                          "provenance": prev.provenance,
                          "active": prev.active})
            cur = prev
        reason = self._explain_text(rec, evidence, now)
        return {"memory": rec.to_dict(), "evidence": evidence,
                "supersedes_chain": chain, "reason": reason,
                "confirmed": rec.confirmation_state == CONFIRMED,
                "inferred": rec.provenance in INFERRED_PROVENANCE}

    def _explain_text(self, rec: MemoryRecord, evidence: list[dict[str, Any]],
                      now: int) -> str:
        trust = TRUST.get(rec.provenance, 0.3)
        if rec.provenance in (EXPLICIT_USER, USER_CONFIRMED):
            return f"You told me: {rec.value}."
        if rec.provenance == ROUTINE_INFERRED:
            n = len(evidence)
            return (f"I noticed this pattern {n} time(s); it is an inferred "
                    f"routine (confidence {int(rec.confidence * 100)}%), not a "
                    f"rule.")
        if rec.provenance == WEB_VERIFIED:
            return (f"I read this from {rec.source_detail or 'the web'}; it is "
                    f"external evidence, not a personal fact.")
        return (f"I recorded this from {rec.provenance or 'observation'} "
                f"(confidence {int(rec.confidence * 100)}%, trust "
                f"{int(trust * 100)}%).")

    # ------------------------------------------------------- staleness/decay
    def freshness(self, rec: MemoryRecord, now: int | None = None) -> float:
        now = int(now if now is not None else self._now())
        if self._expired(rec, now):
            return 0.0
        if rec.type == EPISODIC_EVENT:
            return 0.3
        days = STALE_DAYS.get(rec.type, 0)
        if rec.type == ROUTINE:
            days = int(getattr(self.cfg, "memory_routine_stale_days", days or 21))
        elif rec.type == COURSE_FACT:
            days = int(getattr(self.cfg, "memory_course_stale_days", days or 120))
        elif rec.type == PROJECT_FACT:
            days = int(getattr(self.cfg, "memory_project_stale_days", days or 30))
        if not days:
            return 1.0
        anchor = rec.last_confirmed_at or rec.observed_at or rec.updated_at
        age = max(0.0, (now - anchor) / 86400.0)
        if age <= days:
            return 1.0
        if age >= 2 * days:
            return 0.2
        return round(1.0 - 0.8 * (age - days) / days, 4)

    def refresh_staleness(self, *, now: int | None = None) -> int:
        """Mark expired/stale memories. Keeps history; never deletes."""
        now = int(now if now is not None else self._now())
        rows = self.db.query(
            "SELECT * FROM memories WHERE active=1 ORDER BY updated_at DESC "
            "LIMIT ?", (int(getattr(self.cfg, "memory_max_scan", 500)),))
        changed = 0
        for r in rows:
            rec = MemoryRecord.from_row(r)
            if rec.confirmation_state == STALE:
                continue
            if self._expired(rec, now):
                self.db.execute(
                    "UPDATE memories SET active=0, confirmation_state=?, "
                    "updated_at=? WHERE id=?", (STALE, now, rec.id))
                changed += 1
                continue
            if self.freshness(rec, now) < 1.0 and rec.type in (
                    ROUTINE, COURSE_FACT, PROJECT_FACT):
                self.db.execute(
                    "UPDATE memories SET confirmation_state=?, updated_at=? "
                    "WHERE id=?", (STALE, now, rec.id))
                changed += 1
        if changed:
            self._audit("memory_decay", target="", detail={"stale": changed})
        return changed

    # ------------------------------------------------------- learning: effort
    def _observation(self, signature: str) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM memory_observations WHERE signature=?",
                          (signature,))
        return dict(row) if row else None

    def record_estimate_sample(self, *, category: str, estimated_minutes: int,
                               actual_minutes: int, ref: str = "",
                               now: int | None = None) -> dict[str, Any]:
        """Record one estimate-vs-actual sample and learn a soft adjustment."""
        now = int(now if now is not None else self._now())
        category = (category or "general").strip().lower() or "general"
        estimated = max(1, int(estimated_minutes))
        actual = max(1, int(actual_minutes))
        signature = f"estimate:{category}"
        obs = self._observation(signature)
        if obs is None:
            self.db.execute(
                "INSERT INTO memory_observations(kind,subject,key,value,"
                "signature,count,first_seen,last_seen,confidence,source_events,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("estimate", category, "effort_factor", "", signature, 1,
                 now, now, 0.0, _dumps([ref] if ref else []), now, now))
            obs = self._observation(signature)
        else:
            events = _loads(obs["source_events"], [])
            if ref:
                events.append(ref)
            events = events[-50:]
            self.db.execute(
                "UPDATE memory_observations SET count=count+1, last_seen=?, "
                "source_events=?, updated_at=? WHERE signature=?",
                (now, _dumps(events), now, signature))
            obs = self._observation(signature)
        # accumulate sums in value JSON
        acc = _loads(obs["value"], {}) if obs else {}
        sum_est = float(acc.get("sum_est", 0.0)) + estimated
        sum_act = float(acc.get("sum_act", 0.0)) + actual
        count = int(obs["count"]) if obs else 1
        mean_ratio = round(sum_act / max(1.0, sum_est), 4)
        confidence = round(min(0.9, 0.3 + 0.12 * count), 4)
        self.db.execute(
            "UPDATE memory_observations SET value=?, confidence=? WHERE signature=?",
            (_dumps({"sum_est": sum_est, "sum_act": sum_act,
                     "mean_ratio": mean_ratio, "samples": count}),
             confidence, signature))
        learned = None
        min_samples = int(getattr(self.cfg, "memory_estimate_min_samples", 3))
        min_ratio = float(getattr(self.cfg, "memory_estimate_min_ratio", 1.15))
        if count >= min_samples and (mean_ratio >= min_ratio
                                     or mean_ratio <= 1.0 / max(1.0, min_ratio)):
            res = self.observe(
                type=PREFERENCE, subject=category, key="effort_factor",
                value=_dumps({"factor": mean_ratio, "sample_count": count,
                              "mean_ratio": mean_ratio}),
                provenance=TASK_OBSERVED, confidence=confidence,
                source="estimate_learning",
                tags=["estimate_learning", category],
                evidence=[{"kind": "estimate_sample", "ref": ref,
                           "detail": f"est={estimated} actual={actual}",
                           "observed_at": now}],
                now=now)
            learned = res.get("stored")
        return {"ok": True, "observation": self._observation(signature),
                "mean_ratio": mean_ratio, "samples": count,
                "learned": learned}

    def effort_factor(self, title: str = "", tags: str = "") -> float:
        """The soft planning multiplier learned from estimate samples (1.0 if none)."""
        if not self.enabled():
            return 1.0
        cats = affinity.classify(title, tags)
        candidates = list(cats) if cats else ["general"]
        candidates.append("general")
        for cat in candidates:
            rec = self._active_by_key(PREFERENCE, cat, "effort_factor")
            if rec is None:
                continue
            if rec.provenance in INFERRED_PROVENANCE and \
                    rec.confirmation_state != CONFIRMED and rec.confidence < 0.3:
                continue
            data = _loads(rec.value, {})
            try:
                factor = float(data.get("factor", 1.0))
            except (TypeError, ValueError):
                continue
            return max(0.5, min(2.0, round(factor, 3)))
        return 1.0

    def observe_task_completion(self, task_id: int, *,
                                now: int | None = None) -> dict[str, Any]:
        """Deterministically learn from a completed task's estimate vs elapsed."""
        now = int(now if now is not None else self._now())
        row = self.db.task_by_id(int(task_id))
        if row is None:
            return {"ok": False, "error": "task not found"}
        estimated = int(row["est_minutes"] or 0)
        if estimated <= 0:
            return {"ok": False, "error": "no estimate to learn from"}
        # actual = elapsed since the task was started (task_history 'doing').
        start = None
        for h in self.db.task_history(int(task_id), limit=50):
            if str(h["to_status"]) in ("doing",):
                start = int(h["ts"])
        if not start:
            return {"ok": False, "error": "no start timestamp"}
        actual = max(1, int((now - start) / 60))
        cats = affinity.classify(str(row["title"] or ""), str(row["tags"] or ""))
        category = sorted(cats)[0] if cats else "general"
        return self.record_estimate_sample(
            category=category, estimated_minutes=estimated,
            actual_minutes=actual, ref=f"task:{task_id}", now=now)

    # ---------------------------------------------------- learning: routines
    def sync_routines(self, *, now: int | None = None) -> dict[str, Any]:
        """Mirror the existing routine subsystem into typed ROUTINE memories.

        This is a bridge, not a second detector: it reads ``routines.rows()``.
        """
        now = int(now if now is not None else self._now())
        if self.routines is None or not hasattr(self.routines, "rows"):
            return {"ok": False, "error": "routines unavailable", "synced": 0}
        synced = 0
        for r in self.routines.rows():
            state = str(r.get("state", ""))
            if state in ("declined", "disabled"):
                continue
            title = str(r.get("title") or r.get("category") or "routine")
            signature = str(r.get("signature") or f"routine:{r.get('id')}")
            provenance = USER_CONFIRMED if str(r.get("source")) == "explicit" \
                else ROUTINE_INFERRED
            res = self.remember(
                type=ROUTINE, subject=str(r.get("category", "") or ""),
                key=f"routine:{signature}", value=title,
                provenance=provenance,
                confidence=float(r.get("confidence") or 0.5),
                source="routine_learning",
                source_detail=signature,
                tags=["routine", str(r.get("kind", ""))],
                observed_at=int(r.get("last_ts") or now),
                evidence=[{"kind": "routine", "ref": signature,
                           "detail": f"{r.get('count')} observations",
                           "observed_at": int(r.get("last_ts") or now)}],
                now=now)
            if res.get("ok"):
                synced += 1
        if synced:
            self._audit("memory_routine_sync", detail={"synced": synced})
        return {"ok": True, "synced": synced}

    # --------------------------------------------------------------- parsing
    def parse_explicit(self, text: str) -> dict[str, Any] | None:
        """Deterministically turn an explicit statement into a memory candidate.

        This is intentionally conservative: casual wording ("I'm tired today")
        is NOT turned into a permanent preference.
        """
        raw = (text or "").strip()
        if not raw:
            return None
        # strip a leading "remember that ..." / "note that ..." trigger, but
        # keep the original casing for the stored value (so secret patterns and
        # readable values survive).
        stripped = re.sub(r"^(please\s+)?(remember|note|keep in mind|"
                          r"don'?t forget)(\s+that|\s+this)?[:,]?\s*", "",
                          raw, flags=re.I).strip()
        low = stripped.lower()
        subject = ""
        m = _COURSE_RE.search(raw)
        if m:
            subject = m.group(1).strip().upper()

        # explicit hard instruction
        if re.search(r"\b(don'?t|do not|never|stop)\b", low) and \
                re.search(r"\b(schedule|scheduling|study|studying|blocks?|"
                          r"before|after|gym|cook|bother|remind|notify|"
                          r"priority|tasks?)\b", low):
            return {"type": USER_INSTRUCTION, "subject": subject,
                    "key": "instruction", "value": stripped,
                    "provenance": EXPLICIT_USER, "confidence": 0.95,
                    "tags": ["instruction"]}

        # explicit preference
        if re.search(r"\b(i prefer|i'?d rather|i like|i want|i would like|"
                     r"works better for me|i find)\b", low):
            return {"type": PREFERENCE, "subject": subject,
                    "key": "preference", "value": stripped,
                    "provenance": EXPLICIT_USER, "confidence": 0.9,
                    "tags": ["preference"]}

        # project / course facts
        if subject and re.search(r"\b(project|assignment|hw|homework|exam|"
                                 r"quiz|midterm|final)\b", low):
            kind = PROJECT_FACT if "project" in low else COURSE_FACT
            key = "effort" if _HOURS_RE.search(low) else "fact"
            return {"type": kind, "subject": subject, "key": key,
                    "value": stripped, "provenance": EXPLICIT_USER,
                    "confidence": 0.9, "tags": [kind]}

        # temporal note
        if re.search(r"\b(today|tonight|tomorrow|this (week|weekend|saturday|"
                     r"sunday|monday|tuesday|wednesday|thursday|friday)|next "
                     r"(week|weekend|monday|tuesday|wednesday|thursday|friday))\b",
                     low):
            return {"type": TEMPORAL_NOTE, "subject": subject,
                    "key": "note", "value": stripped,
                    "provenance": EXPLICIT_USER, "confidence": 0.85,
                    "tags": ["temporal"]}

        # generic core fact
        if re.search(r"\b(i (study|live|work|am|have|use)|my )\b", low):
            return {"type": CORE_FACT, "subject": subject,
                    "key": "fact", "value": stripped,
                    "provenance": EXPLICIT_USER, "confidence": 0.85,
                    "tags": ["fact"]}
        return None

    # ---------------------------------------------------------------- stats
    def stats(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        rows = self.db.query(
            "SELECT type, confirmation_state, COUNT(*) AS n FROM memories "
            "GROUP BY type, confirmation_state")
        by_type: dict[str, int] = {}
        by_state: dict[str, int] = {}
        total = 0
        for r in rows:
            n = int(r["n"])
            total += n
            by_type[str(r["type"])] = by_type.get(str(r["type"]), 0) + n
            by_state[str(r["confirmation_state"])] = \
                by_state.get(str(r["confirmation_state"]), 0) + n
        active = self.db.one("SELECT COUNT(*) AS n FROM memories WHERE active=1")
        return {"total": total, "active": int(active["n"]) if active else 0,
                "by_type": by_type, "by_state": by_state,
                "enabled": self.enabled()}
