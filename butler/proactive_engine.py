"""M7: proactive executive behavior.

This module adds a *deterministic* proactive loop on top of M1–M6:

    OBSERVE -> DETECT -> SCORE -> DECIDE -> EXPLAIN/PROPOSE -> NOTIFY
            -> WAIT -> (optionally) EXECUTE THROUGH THE EXISTING SAFETY PATH

It does **not** create a second scheduler, memory, notification system or
autonomous agent:

* the existing :mod:`butler.scheduler` drives the cadence;
* the existing :class:`butler.proactive.Proactive` alert loop is untouched;
* project risk comes from M3, feasibility from M5, facts/preferences from M6,
  external evidence from M4, delivery/audit from the Phase 6 layer;
* the LLM may phrase a message but never decides a side effect — every mutation
  still goes through safety -> permission -> idempotency -> execution -> audit.

The engine is safe to run repeatedly: candidates are keyed deterministically,
persisted with a state hash, and a notification policy (cooldown, dedup, daily
budget, quiet hours, suppressions, snoozes) prevents spam.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

log = logging.getLogger("butler.proactive_engine")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------
CAT_DEADLINE_RISK = "deadline_risk"
CAT_FREE_TIME = "free_time"
CAT_MISSED_TASK = "missed_task"
CAT_SCHEDULE_CONFLICT = "schedule_conflict"
CAT_PROJECT_RISK = "project_risk"
CAT_ESTIMATE = "estimate"
CAT_ROUTINE = "routine"
CAT_TRAVEL = "travel"
CAT_WEB_CHANGE = "web_change"
CAT_FOOD = "food"
CAT_COURSE = "course"

CATEGORIES: tuple[str, ...] = (
    CAT_DEADLINE_RISK, CAT_FREE_TIME, CAT_MISSED_TASK, CAT_SCHEDULE_CONFLICT,
    CAT_PROJECT_RISK, CAT_ESTIMATE, CAT_ROUTINE, CAT_TRAVEL, CAT_WEB_CHANGE,
    CAT_FOOD, CAT_COURSE,
)

PRIORITIES: tuple[str, ...] = ("critical", "high", "medium", "low")
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_PRIORITY_ORDER = ("low", "medium", "high", "critical")

# candidate / notification states
PENDING = "pending"
ACCEPTED = "accepted"
DISMISSED = "dismissed"
SNOOZED = "snoozed"
IGNORED = "ignored"
EXPIRED = "expired"
BASELINE = "baseline"

RESPONSES: tuple[str, ...] = (ACCEPTED, DISMISSED, SNOOZED, IGNORED, EXPIRED)

_TRAVEL_WORDS = ("flight", "airport", "train", "station", "appointment",
                 "interview", "exam", "midterm", "final", "meeting", "class",
                 "lecture", "dentist", "doctor")
_DEADLINE_WORDS = ("project", "assignment", "essay", "paper", "pset", "hw",
                   "homework", "report", "exam", "quiz", "midterm", "final")


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


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _hm(minutes: int) -> str:
    minutes = max(0, min(int(minutes), 1439))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _slug(text: Any) -> str:
    """A Telegram-callback-safe token (lowercase, [a-z0-9-])."""
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40]


@dataclass
class ProactiveCandidate:
    key: str
    category: str
    title: str
    summary: str = ""
    priority: str = "low"
    score: float = 0.0
    confidence: float = 0.0
    detected_at: int = 0
    relevant_entities: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    proposed_action: dict[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = True
    expires_at: int = 0
    state: str = PENDING
    explanation: str = ""
    #: N2: optional per-candidate destination (chat_id/thread_id) so a tracker
    #: can notify a specific Telegram topic through the existing M7 policy.
    destination: dict[str, Any] = field(default_factory=dict)
    # ranking inputs (0..1)
    urgency: float = 0.0
    risk: float = 0.0
    deficit: float = 0.0
    impact: float = 0.0
    novelty: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        return d

    def state_hash(self) -> str:
        payload = _dumps({"c": self.category, "k": self.key, "s": self.summary,
                          "p": self.priority, "e": self.evidence})
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class ProactiveEngine:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db = getattr(container, "db", None)
        self.audit = getattr(container, "audit", None)
        self.context = getattr(container, "context", None)
        self.planner = getattr(container, "planner", None)
        self.projects = getattr(container, "projects", None)
        self.optimizer = getattr(container, "optimizer", None)
        self.memory = getattr(container, "memory", None)
        self.web = getattr(container, "web", None)
        self.routines = getattr(container, "routines", None)
        self.courses = getattr(container, "courses", None)
        self.food = getattr(container, "food", None)
        self.foodplan = getattr(container, "foodplan", None)

    # ------------------------------------------------------------------ time
    def _now(self) -> int:
        cfg = self.cfg
        if cfg is not None and hasattr(cfg, "now_local"):
            try:
                return int(cfg.now_local().timestamp())
            except Exception:  # noqa: BLE001
                pass
        import time
        return int(time.time())

    def _midnight(self, ts: int) -> int:
        cfg = self.cfg
        if cfg is not None and hasattr(cfg, "local_midnight"):
            try:
                return int(cfg.local_midnight(ts))
            except Exception:  # noqa: BLE001
                pass
        d = datetime.fromtimestamp(ts)
        return int(datetime(d.year, d.month, d.day).timestamp())

    def enabled(self) -> bool:
        return (bool(getattr(self.cfg, "proactive_enabled", True))
                and bool(getattr(self.cfg, "proactive_engine_enabled", True))
                and self.db is not None)

    def _cat_enabled(self, category: str) -> bool:
        return bool(getattr(self.cfg, f"proactive_cat_{category}", True))

    def _audit(self, action: str, *, target: str = "", detail: Any = None,
               decision: str = "allowed") -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action, actor="proactive", target=target,
                              decision=decision,
                              outcome="ok" if decision == "allowed" else "not_applied",
                              detail=detail or {})
        except Exception:  # noqa: BLE001
            log.debug("proactive audit failed", exc_info=True)

    # ==================================================================
    # 1. candidate generation (deterministic)
    # ==================================================================
    def generate_candidates(self, *, now: int | None = None,
                            snapshot: dict[str, Any] | None = None,
                            web_values: dict[str, str] | None = None
                            ) -> list[ProactiveCandidate]:
        now = int(now if now is not None else self._now())
        snap = snapshot if snapshot is not None else self._snapshot()
        cands: list[ProactiveCandidate] = []
        detectors: list[tuple[str, Callable[..., list[ProactiveCandidate]]]] = [
            (CAT_DEADLINE_RISK, self._deadline_risk),
            (CAT_COURSE, self._course_deadlines),
            (CAT_PROJECT_RISK, self._project_risk),
            (CAT_FREE_TIME, self._free_time),
            (CAT_MISSED_TASK, self._missed_task),
            (CAT_SCHEDULE_CONFLICT, self._schedule_conflict),
            (CAT_ESTIMATE, self._estimate_issue),
            (CAT_ROUTINE, self._routine_opportunity),
            (CAT_TRAVEL, self._travel_prep),
            (CAT_FOOD, self._food_gap),
            (CAT_WEB_CHANGE, self._web_change),
        ]
        for category, fn in detectors:
            if not self._cat_enabled(category):
                continue
            try:
                cands += fn(now=now, snapshot=snap, web_values=web_values or {})
            except Exception as exc:  # noqa: BLE001 — one detector must not kill the pass
                log.debug("detector %s failed: %s", category, exc, exc_info=True)
        cap = int(getattr(self.cfg, "proactive_max_candidates", 50))
        return cands[:cap]

    def _snapshot(self) -> dict[str, Any]:
        if self.context is None or not hasattr(self.context, "snapshot"):
            return {}
        try:
            return dict(self.context.snapshot())
        except Exception:  # noqa: BLE001
            return {}

    def _active_tasks(self) -> list[Any]:
        try:
            return list(self.db.tasks("active"))
        except Exception:  # noqa: BLE001
            return []

    # ---- A. deadline risk -------------------------------------------------
    def _deadline_risk(self, *, now: int, snapshot: dict[str, Any],
                       web_values: dict[str, str]) -> list[ProactiveCandidate]:
        out: list[ProactiveCandidate] = []
        threshold = float(getattr(self.cfg, "proactive_deadline_risk_ratio", 1.0))
        for row in self._active_tasks():
            deadline = int(row["deadline"] or 0)
            if not deadline or deadline <= now:
                continue
            remaining = int(row["remaining_minutes"] or 0) or int(row["est_minutes"] or 0)
            if remaining <= 0:
                continue
            avail = self._capacity_before(remaining, deadline, now)
            ratio = (remaining / avail) if avail and avail > 0 else 2.0
            if avail > 0 and ratio < threshold:
                continue
            deficit = _clamp((remaining - max(0, avail)) / max(1, remaining))
            days = max(0.0, (deadline - now) / 86400.0)
            urgency = _clamp(1.0 - days / 7.0)
            title = f"⏳ {row['title']} is at risk"
            summary = (f"{remaining}m of work remain but only "
                       f"{max(0, avail)}m of usable time before "
                       f"{datetime.fromtimestamp(deadline).strftime('%a %H:%M')}.")
            out.append(ProactiveCandidate(
                key=f"deadline-risk:{int(row['id'])}",
                category=CAT_DEADLINE_RISK, title=title, summary=summary,
                confidence=0.85, detected_at=now, urgency=urgency,
                deficit=deficit, impact=0.8,
                relevant_entities=[{"type": "task", "id": int(row["id"]),
                                    "name": str(row["title"])}],
                evidence=[
                    {"kind": "remaining_minutes", "value": remaining},
                    {"kind": "usable_minutes_before_deadline",
                     "value": max(0, avail)},
                    {"kind": "deadline", "value": deadline},
                    {"kind": "risk_ratio", "value": round(ratio, 3)},
                ],
                proposed_action={"action": "schedule_task",
                                 "task_id": int(row["id"]),
                                 "minutes": min(remaining, 90)},
                requires_confirmation=True,
                explanation=(f"{row['title']} has {remaining}m remaining and "
                             f"{max(0, avail)}m of usable capacity before its "
                             f"deadline.")))
        return out

    def _optimizer_ok(self, task_id: int, now: int) -> bool:
        """Avoid recommending a block M5 cannot actually place (degrades open)."""
        if self.optimizer is None or not hasattr(self.optimizer,
                                                 "optimize_from_state"):
            return True
        try:
            res = self.optimizer.optimize_from_state(days=1, now=now)
            return any(s.task_id == task_id for s in res.sessions)
        except Exception:  # noqa: BLE001 — degrade to the safe baseline
            return True

    def _capacity_before(self, minutes: int, deadline: int, now: int) -> int:
        if self.planner is not None and hasattr(self.planner, "capacity_before"):
            try:
                res = self.planner.capacity_before(minutes, deadline)
                return int(res.get("available", 0) or 0)
            except Exception:  # noqa: BLE001
                pass
        # fallback: raw free minutes across the window (no buffer modelling)
        return 0

    # ---- course deadlines -------------------------------------------------
    def _course_deadlines(self, *, now: int, snapshot: dict[str, Any],
                          web_values: dict[str, str]) -> list[ProactiveCandidate]:
        out = []
        for c in snapshot.get("courses", []) or []:
            for d in c.get("upcoming_deadlines", []) or []:
                d = int(d)
                if d <= now or d - now > 3 * 86400:
                    continue
                days = (d - now) / 86400.0
                out.append(ProactiveCandidate(
                    key=f"course-deadline:{_slug(c.get('code'))}:{d}",
                    category=CAT_COURSE,
                    title=f"🎓 {c.get('code')} deadline soon",
                    summary=(f"{c.get('code')} has a deadline "
                             f"{datetime.fromtimestamp(d).strftime('%a %H:%M')}."),
                    confidence=0.8, detected_at=now,
                    urgency=_clamp(1.0 - days / 3.0), impact=0.7,
                    relevant_entities=[{"type": "course",
                                        "name": str(c.get("code", ""))}],
                    evidence=[{"kind": "course_deadline", "value": d},
                              {"kind": "course", "value": c.get("code")}],
                    proposed_action={"action": "plan_day"},
                    explanation=f"{c.get('code')} has an upcoming deadline."))
        return out

    # ---- E. project risk increase ----------------------------------------
    def _project_risk(self, *, now: int, snapshot: dict[str, Any],
                      web_values: dict[str, str]) -> list[ProactiveCandidate]:
        if self.projects is None or not hasattr(self.projects, "list_projects"):
            return []
        out = []
        delta_min = float(getattr(self.cfg, "proactive_risk_increase", 0.15))
        for p in self.projects.list_projects():
            pid = int(p.get("id") or 0)
            score = float(p.get("risk") or 0.0)
            level = str(p.get("risk_level") or "low")
            if score <= 0:
                continue
            prev = self._get_candidate(f"project-risk:{pid}")
            old_score = 0.0
            if prev:
                for ev in _loads(prev["evidence"], []):
                    if ev.get("kind") == "risk_score":
                        old_score = float(ev.get("value") or 0.0)
            increased = score - old_score >= delta_min
            if prev is not None and not increased:
                continue
            if prev is None and level not in ("high", "critical"):
                # seed a baseline without notifying
                self._seed_baseline(f"project-risk:{pid}", CAT_PROJECT_RISK,
                                    "project_risk", {"risk_score": score}, now)
                continue
            out.append(ProactiveCandidate(
                key=f"project-risk:{pid}", category=CAT_PROJECT_RISK,
                title=f"📈 {p.get('name')} risk is {level}",
                summary=(f"Project risk is {level} "
                         f"({int(score * 100)}%), up from "
                         f"{int(old_score * 100)}%."),
                confidence=0.85, detected_at=now,
                urgency=_clamp(score), risk=_clamp(score),
                impact=0.8, novelty=1.0 if prev else 0.5,
                relevant_entities=[{"type": "project", "id": pid,
                                    "name": str(p.get("name", ""))}],
                evidence=[{"kind": "risk_score", "value": round(score, 4)},
                          {"kind": "previous_risk_score",
                           "value": round(old_score, 4)},
                          {"kind": "risk_level", "value": level},
                          {"kind": "remaining_minutes",
                           "value": int(p.get("remaining_minutes") or 0)}],
                proposed_action={"action": "optimize_week"},
                explanation=(f"{p.get('name')} risk rose to {level} "
                             f"({int(score * 100)}%).")))
        return out

    # ---- B. underutilized free time --------------------------------------
    def _free_time(self, *, now: int, snapshot: dict[str, Any],
                   web_values: dict[str, str]) -> list[ProactiveCandidate]:
        free = int(snapshot.get("free_minutes_today") or 0)
        min_window = int(getattr(self.cfg, "proactive_min_free_window_minutes", 60))
        if free < min_window:
            return []
        top = self._top_work()
        if top is None:
            return []
        task, minutes, why = top
        if not self._optimizer_ok(task["id"], now):
            return []
        day = self._midnight(now)
        block = min(90, free, max(30, minutes))
        return [ProactiveCandidate(
            key=f"free-window:{day}:{task['id']}",
            category=CAT_FREE_TIME,
            title=f"🕑 {free}m open — {task['title']}",
            summary=(f"You have {free}m free today. {task['title']} "
                     f"({why})."),
            confidence=0.7, detected_at=now, urgency=0.5, impact=0.6,
            relevant_entities=[{"type": "task", "id": int(task["id"]),
                                "name": str(task["title"])}],
            evidence=[{"kind": "free_minutes_today", "value": free},
                      {"kind": "task", "value": task["title"]},
                      {"kind": "reason", "value": why}],
            proposed_action={"action": "schedule_task",
                             "task_id": int(task["id"]), "minutes": block},
            explanation=(f"{free}m of free time and {task['title']} is the "
                         f"highest-value unfinished work ({why})."))]

    def _top_work(self) -> tuple[dict[str, Any], int, str] | None:
        """The best unfinished task to recommend, or None."""
        tasks = self._active_tasks()
        if not tasks:
            return None
        scored = []
        for r in tasks:
            pr = int(r["priority"] or 3)
            rem = int(r["remaining_minutes"] or 0) or int(r["est_minutes"] or 0)
            dl = int(r["deadline"] or 0)
            score = pr * 10 + (5 if dl else 0) + min(rem, 120) / 60.0
            scored.append((score, r, rem))
        scored.sort(key=lambda x: (-x[0], int(x[1]["id"])))
        _, row, rem = scored[0]
        why = f"priority {int(row['priority'] or 3)}"
        if row["deadline"]:
            why += f", due {datetime.fromtimestamp(int(row['deadline'])).strftime('%a')}"
        return {"id": int(row["id"]), "title": str(row["title"])}, max(30, rem), why

    # ---- C. missed / skipped work ----------------------------------------
    def _missed_task(self, *, now: int, snapshot: dict[str, Any],
                     web_values: dict[str, str]) -> list[ProactiveCandidate]:
        row = None
        try:
            row = self.db.latest_plan()
        except Exception:  # noqa: BLE001
            return []
        if row is None:
            return []
        from . import schedule as sch
        try:
            state = sch.PlanState.from_json(str(row["json"]))
        except Exception:  # noqa: BLE001
            return []
        day = self._midnight(int(row["created"]))
        now_min = (now - day) // 60 if day <= now < day + 86400 else 1440
        out = []
        for s in state.slots:
            if s.end_min > now_min:
                continue
            task = self.db.task_by_id(int(s.task_id))
            if task is None:
                continue
            status = str(task["status"] or "todo")
            if status not in ("todo", "doing", "scheduled"):
                continue
            dl = int(task["deadline"] or 0)
            out.append(ProactiveCandidate(
                key=f"missed:{s.task_id}:{day}",
                category=CAT_MISSED_TASK,
                title=f"↩︎ Planned block not finished: {s.title}",
                summary=(f"The {_hm(s.start_min)}–{_hm(s.end_min)} block for "
                         f"{s.title} passed without completion."),
                confidence=0.8, detected_at=now, urgency=0.6,
                impact=0.6 if dl else 0.4,
                relevant_entities=[{"type": "task", "id": int(s.task_id),
                                    "name": s.title}],
                evidence=[{"kind": "planned_start", "value": s.start_min},
                          {"kind": "planned_end", "value": s.end_min},
                          {"kind": "task_status", "value": status},
                          {"kind": "deadline", "value": dl}],
                proposed_action={"action": "reschedule_task",
                                 "task_id": int(s.task_id)},
                explanation=(f"{s.title} was planned {_hm(s.start_min)}–"
                             f"{_hm(s.end_min)} but is still {status}.")))
        return out

    # ---- D. schedule conflict --------------------------------------------
    def _schedule_conflict(self, *, now: int, snapshot: dict[str, Any],
                           web_values: dict[str, str]) -> list[ProactiveCandidate]:
        row = None
        try:
            row = self.db.latest_plan()
        except Exception:  # noqa: BLE001
            return []
        if row is None or self.planner is None:
            return []
        from . import schedule as sch
        try:
            state = sch.PlanState.from_json(str(row["json"]))
        except Exception:  # noqa: BLE001
            return []
        day = self._midnight(int(row["created"]))
        try:
            events = self.planner._day_events(day)
        except Exception:  # noqa: BLE001
            return []
        out = []
        for s in state.slots:
            for e in events:
                if s.start_min < e.end_min and s.end_min > e.start_min:
                    out.append(ProactiveCandidate(
                        key=f"schedule-conflict:{getattr(e, 'id', 0)}:{s.task_id}",
                        category=CAT_SCHEDULE_CONFLICT,
                        title=f"⚡ Conflict: {e.title} vs {s.title}",
                        summary=(f"{e.title} ({_hm(e.start_min)}–"
                                 f"{_hm(e.end_min)}) overlaps the planned "
                                 f"{s.title} block."),
                        confidence=0.9, detected_at=now, urgency=0.7,
                        impact=0.7,
                        relevant_entities=[
                            {"type": "event", "id": int(getattr(e, "id", 0)),
                             "name": e.title},
                            {"type": "task", "id": int(s.task_id),
                             "name": s.title}],
                        evidence=[{"kind": "event", "value": e.title},
                                  {"kind": "event_start", "value": e.start_min},
                                  {"kind": "event_end", "value": e.end_min},
                                  {"kind": "planned_start", "value": s.start_min},
                                  {"kind": "planned_end", "value": s.end_min}],
                        proposed_action={"action": "reschedule"},
                        explanation=(f"{e.title} now conflicts with the "
                                     f"committed {s.title} block.")))
        return out

    # ---- F. estimate issue -----------------------------------------------
    def _estimate_issue(self, *, now: int, snapshot: dict[str, Any],
                        web_values: dict[str, str]) -> list[ProactiveCandidate]:
        if self.memory is None or not hasattr(self.memory, "effort_factor"):
            return []
        out = []
        seen: set[str] = set()
        for row in self._active_tasks():
            from . import affinity
            cats = affinity.classify(str(row["title"] or ""),
                                     str(row["tags"] or ""))
            cat = sorted(cats)[0] if cats else "general"
            if cat in seen:
                continue
            seen.add(cat)
            factor = float(self.memory.effort_factor(str(row["title"] or ""),
                                                     str(row["tags"] or "")))
            if abs(factor - 1.0) < 0.15:
                continue
            obs = None
            try:
                obs = self.memory._observation(f"estimate:{cat}")
            except Exception:  # noqa: BLE001
                obs = None
            samples = int((obs or {}).get("count") or 0)
            out.append(ProactiveCandidate(
                key=f"estimate:{cat}", category=CAT_ESTIMATE,
                title=f"📐 Your {cat} estimates look off",
                summary=(f"Recent {cat} tasks took about "
                         f"{int(round(factor * 100))}% of your estimate."),
                confidence=min(0.9, 0.4 + 0.1 * samples), detected_at=now,
                urgency=0.3, impact=0.4,
                evidence=[{"kind": "effort_factor", "value": round(factor, 3)},
                          {"kind": "sample_count", "value": samples}],
                proposed_action={"action": "adjust_estimates", "category": cat},
                explanation=(f"Across {samples} samples your {cat} tasks "
                             f"consistently took about "
                             f"{int(round(factor * 100))}% of the estimate.")))
        return out

    # ---- G. routine opportunity ------------------------------------------
    def _routine_opportunity(self, *, now: int, snapshot: dict[str, Any],
                             web_values: dict[str, str]) -> list[ProactiveCandidate]:
        if self.routines is None or not hasattr(self.routines, "active"):
            return []
        free = int(snapshot.get("free_minutes_today") or 0)
        min_window = int(getattr(self.cfg, "proactive_min_free_window_minutes", 60))
        if free < min_window:
            return []
        try:
            routines = self.routines.active()
        except Exception:  # noqa: BLE001
            return []
        day = self._midnight(now)
        now_min = (now - day) // 60
        out = []
        for r in routines[:5]:
            start = int(r.get("start_min") or 0)
            end = int(r.get("end_min") or 0)
            if not (start - 90 <= now_min <= end + 90):
                continue
            label = str(r.get("title") or r.get("category") or "routine")
            out.append(ProactiveCandidate(
                key=f"routine-opportunity:{int(r.get('id') or 0)}:{day}",
                category=CAT_ROUTINE,
                title=f"🔁 Good time for {label}",
                summary=(f"This is when you usually {label} and you have "
                         f"{free}m free."),
                confidence=float(r.get("confidence") or 0.5), detected_at=now,
                urgency=0.3, impact=0.4, novelty=0.7,
                evidence=[{"kind": "routine", "value": label},
                          {"kind": "confidence",
                           "value": float(r.get("confidence") or 0.5)},
                          {"kind": "free_minutes_today", "value": free}],
                proposed_action={"action": "recommend_routine",
                                 "routine_id": int(r.get("id") or 0)},
                explanation=(f"You usually {label} around now and have "
                             f"{free}m free.")))
        return out

    # ---- H. travel / preparation -----------------------------------------
    def _travel_prep(self, *, now: int, snapshot: dict[str, Any],
                     web_values: dict[str, str]) -> list[ProactiveCandidate]:
        lead = int(getattr(self.cfg, "proactive_prep_lead_minutes", 60))
        out = []
        for e in snapshot.get("events_today", []) or []:
            start = int(e.get("start_ts") or 0)
            if start <= now or start - now > lead * 60:
                continue
            text = f"{e.get('title', '')} {e.get('location', '')}".lower()
            if not any(w in text for w in _TRAVEL_WORDS):
                continue
            out.append(ProactiveCandidate(
                key=f"travel-prep:{e.get('id')}",
                category=CAT_TRAVEL,
                title=f"🚗 Prepare for {e.get('title')}",
                summary=(f"{e.get('title')} starts at "
                         f"{datetime.fromtimestamp(start).strftime('%H:%M')} "
                         f"(within {lead}m)."),
                confidence=0.7, detected_at=now, urgency=0.8, impact=0.6,
                evidence=[{"kind": "event", "value": e.get("title")},
                          {"kind": "event_start", "value": start},
                          {"kind": "location", "value": e.get("location", "")},
                          {"kind": "lead_minutes", "value": lead}],
                proposed_action={"action": "prepare", "event_id": e.get("id")},
                explanation=(f"{e.get('title')} is within the configured "
                             f"{lead}m preparation window.")))
        return out

    # ---- food gap --------------------------------------------------------
    def _food_gap(self, *, now: int, snapshot: dict[str, Any],
                  web_values: dict[str, str]) -> list[ProactiveCandidate]:
        fp = self.foodplan
        if fp is None or not hasattr(fp, "peek"):
            return []
        try:
            res = fp.peek()
        except Exception:  # noqa: BLE001
            return []
        plan = (res or {}).get("plan") or {}
        recipe = plan.get("recipe")
        missing = plan.get("missing") or plan.get("missing_ingredients") or []
        if not recipe or not missing:
            return []
        return [ProactiveCandidate(
            key=f"food:{_slug(recipe)}", category=CAT_FOOD,
            title=f"🛒 Missing ingredients for {recipe}",
            summary=(f"You planned {recipe} but are missing "
                     f"{', '.join(str(m) for m in missing[:4])}."),
            confidence=0.75, detected_at=now, urgency=0.3, impact=0.4,
            evidence=[{"kind": "recipe", "value": recipe},
                      {"kind": "missing", "value": list(missing)[:6]}],
            proposed_action={"action": "add_groceries",
                             "items": list(missing)[:6]},
            explanation=(f"{recipe} needs ingredients you don't have."))]

    # ---- I. web change ---------------------------------------------------
    def _web_change(self, *, now: int, snapshot: dict[str, Any],
                    web_values: dict[str, str]) -> list[ProactiveCandidate]:
        if self.memory is None or not web_values:
            return []
        rows = self.memory.list(types=["course_fact", "project_fact"],
                                scope="external", active_only=True, limit=50)
        out = []
        for m in rows:
            ident = f"{m.get('subject')}:{m.get('key')}"
            current = web_values.get(ident) or web_values.get(str(m.get("id")))
            if current is None:
                continue
            if str(current).strip() == str(m.get("value") or "").strip():
                continue
            out.append(ProactiveCandidate(
                key=f"web-change:{_slug(m.get('subject'))}:"
                    f"{_slug(m.get('key'))}",
                category=CAT_WEB_CHANGE,
                title=f"🌐 {m.get('subject')} information changed",
                summary=(f"{m.get('subject')} {m.get('key')} changed from "
                         f"'{m.get('value')}' to '{current}'."),
                confidence=0.8, detected_at=now, urgency=0.5, impact=0.5,
                evidence=[{"kind": "old_value", "value": m.get("value")},
                          {"kind": "new_value", "value": current},
                          {"kind": "source_url", "value": m.get("source_detail")},
                          {"kind": "provenance", "value": m.get("provenance")}],
                proposed_action={"action": "review_web_fact",
                                 "memory_id": m.get("id")},
                explanation=(f"Verified external information for "
                             f"{m.get('subject')} changed.")))
        return out

    # ==================================================================
    # 2. ranking
    # ==================================================================
    def rank_candidates(self, cands: list[ProactiveCandidate], *,
                        now: int | None = None) -> list[ProactiveCandidate]:
        now = int(now if now is not None else self._now())
        prefs = self._memory_prefs(now)
        reactions = self._reaction_rates()
        for c in cands:
            pref_boost = 0.0
            if prefs.get("boost_risk") and c.category in (
                    CAT_PROJECT_RISK, CAT_DEADLINE_RISK):
                pref_boost = 0.15
            if prefs.get("mute_low") and c.priority == "low":
                pref_boost -= 0.3
            dismiss_rate = reactions.get(c.category, {}).get("dismiss_rate", 0.0)
            penalty = 0.0
            if c.priority != "critical":
                penalty = 0.15 * dismiss_rate
            score = (0.30 * c.urgency + 0.20 * c.risk + 0.15 * c.deficit
                     + 0.10 * c.impact + 0.10 * c.novelty
                     + 0.10 * c.confidence + 0.05 * pref_boost - penalty)
            c.score = round(_clamp(score), 4)
            c.priority = self._priority_for(c.score, c.category)
        cands.sort(key=lambda x: (_PRIORITY_RANK.get(x.priority, 3),
                                  -x.score, x.key))
        return cands

    def _priority_for(self, score: float, category: str) -> str:
        if score >= 0.72:
            return "critical"
        if score >= 0.52:
            return "high"
        if score >= 0.34:
            return "medium"
        return "low"

    # ==================================================================
    # 3. notification policy
    # ==================================================================
    def policy_filter(self, cands: list[ProactiveCandidate], *,
                      now: int | None = None
                      ) -> tuple[list[ProactiveCandidate], list[dict[str, Any]]]:
        now = int(now if now is not None else self._now())
        min_rank = _PRIORITY_RANK.get(
            str(getattr(self.cfg, "proactive_min_priority", "medium")), 2)
        min_conf = float(getattr(self.cfg, "proactive_min_confidence", 0.6))
        cooldown = int(getattr(self.cfg, "proactive_cooldown_minutes", 180)) * 60
        budget = int(getattr(self.cfg, "proactive_daily_budget", 5))
        crit_budget = int(getattr(self.cfg, "proactive_critical_budget", 2))
        quiet = self._in_quiet_hours(now)
        quiet_critical = bool(getattr(self.cfg, "proactive_quiet_critical", True))
        day = self._midnight(now)
        used = self._notified_today(day)
        allowed: list[ProactiveCandidate] = []
        suppressed: list[dict[str, Any]] = []
        for c in cands:
            reason = self._suppress_reason(c, now, min_rank, min_conf,
                                           cooldown, quiet, quiet_critical)
            if reason:
                suppressed.append({"key": c.key, "category": c.category,
                                   "reason": reason})
                continue
            if c.priority == "critical":
                if used["critical"] >= crit_budget and crit_budget >= 0:
                    suppressed.append({"key": c.key, "category": c.category,
                                       "reason": "critical_budget"})
                    continue
                used["critical"] += 1
            else:
                if budget >= 0 and used["normal"] >= budget:
                    suppressed.append({"key": c.key, "category": c.category,
                                       "reason": "daily_budget"})
                    continue
                used["normal"] += 1
            allowed.append(c)
        return allowed, suppressed

    def _suppress_reason(self, c: ProactiveCandidate, now: int, min_rank: int,
                         min_conf: float, cooldown: int, quiet: bool,
                         quiet_critical: bool) -> str:
        if c.expires_at and c.expires_at <= now:
            return "expired"
        if c.confidence < min_conf:
            return "low_confidence"
        if _PRIORITY_RANK.get(c.priority, 3) > min_rank:
            return "below_threshold"
        if quiet and not (quiet_critical and c.priority == "critical"):
            return "quiet_hours"
        if self._is_suppressed(c.key, c.category, now):
            return "user_suppressed"
        snooze = self._snooze_until(c.key)
        if snooze and snooze > now:
            return "snoozed"
        row = self._get_candidate(c.key)
        if row is not None:
            state = str(row["state"] or PENDING)
            if state in (ACCEPTED, DISMISSED, EXPIRED):
                return f"state_{state}"
            last_notified = int(row["last_notified"] or 0)
            last_hash = str(row["last_state_hash"] or "")
            if last_notified:
                if last_hash == c.state_hash() and (now - last_notified) < cooldown:
                    return "cooldown"
                if last_hash == c.state_hash() and (now - last_notified) >= cooldown:
                    return "dedup"
        return ""

    def _in_quiet_hours(self, now: int) -> bool:
        quiet_end = int(getattr(self.cfg, "notify_quiet_end", 8 * 60))
        quiet_start = int(getattr(self.cfg, "notify_quiet_start", 22 * 60))
        if quiet_start <= 0 or quiet_end <= 0:
            return False
        dt = datetime.fromtimestamp(now)
        m = dt.hour * 60 + dt.minute
        if quiet_start <= quiet_end:
            return not (quiet_end <= m < quiet_start)
        return m >= quiet_start or m < quiet_end

    def _notified_today(self, day: int) -> dict[str, int]:
        try:
            rows = self.db.query(
                "SELECT priority, COUNT(*) AS n FROM proactive_notifications "
                "WHERE ts>=? AND ts<? GROUP BY priority",
                (day, day + 86400))
        except Exception:  # noqa: BLE001
            return {"normal": 0, "critical": 0}
        normal = critical = 0
        for r in rows:
            n = int(r["n"])
            if str(r["priority"]) == "critical":
                critical += n
            else:
                normal += n
        return {"normal": normal, "critical": critical}

    # ==================================================================
    # 4. persistence
    # ==================================================================
    def _get_candidate(self, key: str) -> Any:
        try:
            return self.db.one("SELECT * FROM proactive_candidates WHERE key=?",
                               (key,))
        except Exception:  # noqa: BLE001
            return None

    def _seed_baseline(self, key: str, category: str, title: str,
                       evidence: dict[str, Any], now: int) -> None:
        ev = [{"kind": k, "value": v} for k, v in evidence.items()]
        self.db.execute(
            "INSERT OR IGNORE INTO proactive_candidates(key,category,title,"
            "summary,priority,score,confidence,detected_at,updated_at,first_seen,"
            "last_seen,state,evidence,explanation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, category, title, "", "low", 0.0, 0.5, now, now, now, now,
             BASELINE, _dumps(ev), ""))

    def _persist_candidate(self, c: ProactiveCandidate, now: int) -> bool:
        """Upsert a candidate. Returns True when it is new or changed."""
        row = self._get_candidate(c.key)
        h = c.state_hash()
        if row is None:
            self.db.execute(
                "INSERT INTO proactive_candidates(key,category,title,summary,"
                "priority,score,confidence,detected_at,updated_at,first_seen,"
                "last_seen,state,evidence,proposed_action,requires_confirmation,"
                "expires_at,relevant_entities,explanation,last_state_hash,"
                "last_notified,notification_count) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (c.key, c.category, c.title, c.summary, c.priority, c.score,
                 c.confidence, c.detected_at, now, now, now, PENDING,
                 _dumps(c.evidence), _dumps(c.proposed_action),
                 1 if c.requires_confirmation else 0, c.expires_at,
                 _dumps(c.relevant_entities), c.explanation, h, 0, 0))
            self._audit("proactive_candidate", target=c.key,
                        detail={"category": c.category, "priority": c.priority,
                                "new": True})
            return True
        changed = str(row["last_state_hash"] or "") != h
        state = str(row["state"] or PENDING)
        # a resolved candidate that re-appears unchanged stays resolved
        new_state = state if (not changed and state in
                              (ACCEPTED, DISMISSED, EXPIRED)) else PENDING
        self.db.execute(
            "UPDATE proactive_candidates SET title=?, summary=?, priority=?, "
            "score=?, confidence=?, updated_at=?, last_seen=?, state=?, "
            "evidence=?, proposed_action=?, requires_confirmation=?, "
            "expires_at=?, relevant_entities=?, explanation=?, "
            "last_state_hash=? WHERE key=?",
            (c.title, c.summary, c.priority, c.score, c.confidence, now, now,
             new_state, _dumps(c.evidence), _dumps(c.proposed_action),
             1 if c.requires_confirmation else 0, c.expires_at,
             _dumps(c.relevant_entities), c.explanation, h, c.key))
        if changed:
            self._audit("proactive_candidate", target=c.key,
                        detail={"category": c.category, "changed": True})
        return changed

    def _record_notification(self, c: ProactiveCandidate, now: int,
                             channel: str, message: str) -> None:
        self.db.execute(
            "INSERT INTO proactive_notifications(candidate_key,ts,channel,"
            "priority,state,message) VALUES(?,?,?,?,?,?)",
            (c.key, now, channel, c.priority, "sent", message[:2000]))
        self.db.execute(
            "UPDATE proactive_candidates SET last_notified=?, "
            "notification_count=notification_count+1, state=?, updated_at=? "
            "WHERE key=?", (now, PENDING, now, c.key))

    # ==================================================================
    # 5. run cycle
    # ==================================================================
    def run_cycle(self, *, now: int | None = None, deliver: bool = True,
                  web_values: dict[str, str] | None = None,
                  persist: bool = True) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        if not self.enabled():
            return {"ok": False, "reason": "proactive engine disabled",
                    "generated": 0, "notified": 0}
        if persist:
            self.expire_stale(now=now)
        cands = self.generate_candidates(now=now, web_values=web_values)
        ranked = self.rank_candidates(cands, now=now)
        allowed, suppressed = self.policy_filter(ranked, now=now)
        if not persist:
            # pure preview: no candidate/notification/audit writes
            return {"ok": True, "generated": len(ranked), "notified": 0,
                    "allowed": [c.key for c in allowed],
                    "suppressed": suppressed, "messages": [],
                    "candidates": [c.to_dict() for c in ranked]}
        # persist every candidate (so status/history is complete)
        for c in ranked:
            self._persist_candidate(c, now)
        for s in suppressed:
            self._audit("proactive_suppressed", target=s["key"],
                        detail={"reason": s["reason"], "category": s["category"]})
        messages: list[dict[str, Any]] = []
        if deliver:
            for c in allowed:
                msg = self.format_message(c)
                channel = "telegram" if (self.cfg.telegram_token
                                         and (self.cfg.notify_chat
                                              or self.cfg.digest_chat)) else "log"
                self._send(c, msg, channel, now)
                self._record_notification(c, now, channel, msg)
                self._audit("proactive_notified", target=c.key,
                            detail={"category": c.category,
                                    "priority": c.priority, "channel": channel})
                messages.append({"key": c.key, "priority": c.priority,
                                 "message": msg})
        return {"ok": True, "generated": len(ranked), "notified": len(messages),
                "allowed": [c.key for c in allowed],
                "suppressed": suppressed, "messages": messages,
                "candidates": [c.to_dict() for c in ranked]}

    # ==================================================================
    # 6. responses / snooze / suppress
    # ==================================================================
    def respond(self, key: str, response: str, *, now: int | None = None,
                note: str = "", minutes: int = 0) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        response = str(response or "").lower()
        if response not in RESPONSES:
            return {"ok": False, "error": f"unknown response {response!r}"}
        row = self._get_candidate(key)
        if row is None:
            return {"ok": False, "error": "unknown candidate", "key": key}
        if response == SNOOZED:
            mins = int(minutes or 180)
            self.snooze(key, mins, now=now)
        state = SNOOZED if response == SNOOZED else response
        self.db.execute(
            "UPDATE proactive_candidates SET state=?, updated_at=? WHERE key=?",
            (state, now, key))
        self.db.execute(
            "INSERT INTO proactive_responses(candidate_key,response,ts,note) "
            "VALUES(?,?,?,?)", (key, response, now, note[:500]))
        self._audit("proactive_response", target=key,
                    detail={"response": response, "note": note})
        # explicit suppression through the memory system (where appropriate)
        if response == DISMISSED and note == "stop":
            self.suppress(key, now=now, reason="user_stop", scope="candidate")
        return {"ok": True, "key": key, "response": response,
                "candidate": self.get_candidate(key)}

    def snooze(self, key: str, minutes: int, *, now: int | None = None
               ) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        minutes = max(1, int(minutes))
        until = now + minutes * 60
        self.db.execute(
            "INSERT INTO proactive_snoozes(key,until,created_at) "
            "VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "until=excluded.until, created_at=excluded.created_at",
            (key, until, now))
        self.db.execute(
            "UPDATE proactive_candidates SET state=?, updated_at=? WHERE key=?",
            (SNOOZED, now, key))
        self._audit("proactive_snooze", target=key,
                    detail={"minutes": minutes, "until": until})
        return {"ok": True, "key": key, "until": until, "minutes": minutes}

    def suppress(self, target: str, *, now: int | None = None,
                 reason: str = "user", scope: str = "candidate",
                 until: int = 0) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        self.db.execute(
            "INSERT INTO proactive_suppressions(key,scope,reason,created_at,"
            "until) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "scope=excluded.scope, reason=excluded.reason, "
            "created_at=excluded.created_at, until=excluded.until",
            (target, scope, reason, now, until))
        self._audit("proactive_suppress", target=target,
                    detail={"reason": reason, "scope": scope})
        # remember the explicit instruction (M6) when it names a category
        if self.memory is not None and scope == "category":
            try:
                self.memory.remember_explicit(
                    f"stop reminding me about {target}", now=now)
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "key": target, "scope": scope, "until": until}

    def unsuppress(self, target: str, *, now: int | None = None) -> dict[str, Any]:
        self.db.execute("DELETE FROM proactive_suppressions WHERE key=?",
                        (target,))
        return {"ok": True, "key": target}

    def _is_suppressed(self, key: str, category: str, now: int) -> bool:
        rows = self.db.query(
            "SELECT * FROM proactive_suppressions WHERE key IN (?,?)",
            (key, category))
        for r in rows:
            until = int(r["until"] or 0)
            if until == 0 or until > now:
                return True
        return False

    def _snooze_until(self, key: str) -> int:
        row = self.db.one("SELECT until FROM proactive_snoozes WHERE key=?",
                          (key,))
        return int(row["until"] or 0) if row else 0

    def expire_stale(self, *, now: int | None = None) -> int:
        """Mark unanswered pending notifications past expiry as ignored."""
        now = int(now if now is not None else self._now())
        expiry = int(getattr(self.cfg, "proactive_candidate_expiry_minutes",
                             1440)) * 60
        cutoff = now - expiry
        rows = self.db.query(
            "SELECT key, detected_at FROM proactive_candidates "
            "WHERE state=? AND last_notified>0 AND last_notified<?",
            (PENDING, cutoff))
        n = 0
        for r in rows:
            self.db.execute(
                "UPDATE proactive_candidates SET state=?, updated_at=? "
                "WHERE key=?", (IGNORED, now, r["key"]))
            self.db.execute(
                "INSERT INTO proactive_responses(candidate_key,response,ts,note) "
                "VALUES(?,?,?,?)", (r["key"], IGNORED, now, "expired"))
            n += 1
        return n

    # ==================================================================
    # 7. queries / explanations / briefing
    # ==================================================================
    def get_candidate(self, key: str) -> dict[str, Any] | None:
        row = self._get_candidate(key)
        if row is None:
            return None
        d = dict(row)
        for k in ("evidence", "proposed_action", "relevant_entities"):
            d[k] = _loads(d.get(k), [] if k != "proposed_action" else {})
        return d

    def list_candidates(self, *, state: str = "", category: str = "",
                        limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM proactive_candidates WHERE 1=1"
        params: list[Any] = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        out = []
        for r in self.db.query(sql, tuple(params)):
            d = dict(r)
            d["evidence"] = _loads(d.get("evidence"), [])
            d["proposed_action"] = _loads(d.get("proposed_action"), {})
            d["relevant_entities"] = _loads(d.get("relevant_entities"), [])
            out.append(d)
        return out

    def explain(self, key: str) -> dict[str, Any] | None:
        cand = self.get_candidate(key)
        if cand is None:
            return None
        return self.explain_candidate(cand)

    def explain_candidate(self, cand: dict[str, Any]) -> dict[str, Any]:
        return {
            "key": cand["key"], "category": cand["category"],
            "title": cand["title"], "summary": cand.get("summary", ""),
            "priority": cand.get("priority"), "score": cand.get("score"),
            "confidence": cand.get("confidence"),
            "evidence": cand.get("evidence", []),
            "explanation": cand.get("explanation", ""),
            "state": cand.get("state", PENDING),
            "why": self._why_text(cand),
        }

    def _why_text(self, cand: dict[str, Any]) -> str:
        bits = [f"{cand.get('title')}"]
        for ev in cand.get("evidence", []):
            bits.append(f"{ev.get('kind')}={ev.get('value')}")
        return "Because " + ", ".join(bits[1:]) + "." if len(bits) > 1 \
            else str(cand.get("explanation") or cand.get("summary") or "")

    def status(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        rows = self.db.query(
            "SELECT state, COUNT(*) AS n FROM proactive_candidates GROUP BY state")
        by_state = {str(r["state"]): int(r["n"]) for r in rows}
        day = self._midnight(now)
        used = self._notified_today(day)
        return {
            "enabled": self.enabled(),
            "quiet_hours": self._in_quiet_hours(now),
            "candidates": sum(by_state.values()),
            "by_state": by_state,
            "notified_today": used["normal"] + used["critical"],
            "daily_budget": int(getattr(self.cfg, "proactive_daily_budget", 5)),
            "critical_budget": int(getattr(self.cfg, "proactive_critical_budget", 2)),
            "suppressions": [dict(r) for r in self.db.query(
                "SELECT * FROM proactive_suppressions ORDER BY id DESC LIMIT 20")],
            "snoozes": [dict(r) for r in self.db.query(
                "SELECT * FROM proactive_snoozes WHERE until>? ORDER BY until",
                (now,))],
        }

    # ==================================================================
    # 8. formatting / delivery
    # ==================================================================
    def format_message(self, c: ProactiveCandidate) -> str:
        icon = {"critical": "🔴", "high": "🟠", "medium": "🟡",
                "low": "⚪"}.get(c.priority, "•")
        lines = [f"{icon} {c.title}", "", c.summary]
        if c.explanation and c.explanation != c.summary:
            lines.append(c.explanation)
        return "\n".join(x for x in lines if x)

    def buttons(self, c: ProactiveCandidate) -> list[list[tuple[str, str]]]:
        return [[("✅ Accept", f"pro:accept:{c.key}"),
                 ("⏰ Snooze", f"pro:snooze:{c.key}"),
                 ("✖️ Dismiss", f"pro:dismiss:{c.key}"),
                 ("ℹ️ Details", f"pro:details:{c.key}")]]

    def _send(self, c: ProactiveCandidate, text: str, channel: str,
              now: int) -> bool:
        if channel != "telegram":
            log.info("proactive candidate %s: %s", c.key, text)
            return True
        dest = c.destination or {}
        chat = int(dest.get("chat_id") or 0) or self.cfg.notify_chat \
            or self.cfg.digest_chat
        thread = int(dest.get("thread_id") or 0)
        try:
            import requests
            keyboard = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in self.buttons(c)]}
            payload = {"chat_id": chat, "text": text,
                       "reply_markup": keyboard}
            if thread:
                payload["message_thread_id"] = thread
            r = requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json=payload, timeout=15)
            return bool(r.ok)
        except Exception as exc:  # noqa: BLE001
            log.warning("proactive send failed: %s", exc)
            return False

    # --------------------------------------------------- N2: external ingest
    def ingest(self, candidates: list[ProactiveCandidate], *,
               now: int | None = None, deliver: bool = True
               ) -> list[dict[str, Any]]:
        """Accept externally generated candidates (e.g. from the tracker engine)
        and run them through the SAME ranking/policy/notification path.

        This is the single integration point that keeps tracking from becoming a
        second notification engine.
        """
        now = int(now if now is not None else self._now())
        if not candidates:
            return []
        ranked = self.rank_candidates(list(candidates), now=now)
        allowed, suppressed = self.policy_filter(ranked, now=now)
        for c in ranked:
            self._persist_candidate(c, now)
        for s in suppressed:
            self._audit("proactive_suppressed", target=s["key"],
                        detail={"reason": s["reason"], "category": s["category"],
                                "origin": "tracker"})
        out: list[dict[str, Any]] = []
        if deliver:
            for c in allowed:
                msg = self.format_message(c)
                channel = "telegram" if (self.cfg.telegram_token
                                         and (self.cfg.notify_chat
                                              or self.cfg.digest_chat
                                              or c.destination)) else "log"
                self._send(c, msg, channel, now)
                self._record_notification(c, now, channel, msg)
                self._audit("proactive_notified", target=c.key,
                            detail={"category": c.category,
                                    "priority": c.priority, "channel": channel,
                                    "origin": "tracker"})
                out.append({"key": c.key, "priority": c.priority,
                            "message": msg})
        return out

    # ==================================================================
    # 9. daily briefing
    # ==================================================================
    def briefing(self, *, now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else self._now())
        if not bool(getattr(self.cfg, "proactive_briefing_enabled", True)):
            return {"ok": False, "reason": "briefing disabled", "text": ""}
        snap = self._snapshot()
        cands = self.rank_candidates(self.generate_candidates(now=now,
                                                              snapshot=snap),
                                     now=now)
        day = datetime.fromtimestamp(now).strftime("%A %d %b")
        lines = ["☀️ Good morning.", "", f"Today is {day}."]
        events = snap.get("events_today", []) or []
        if events:
            lines.append("")
            lines.append("📅 Hard commitments:")
            for e in events[:6]:
                start = int(e.get("start_ts") or 0)
                lines.append(f"  • {e.get('title')} "
                             f"{datetime.fromtimestamp(start).strftime('%H:%M')}")
        free = int(snap.get("free_minutes_today") or 0)
        lines.append("")
        lines.append(f"🕑 Usable free time: ~{free}m")
        top = [c for c in cands if c.priority in ("critical", "high")][:3]
        if top:
            lines.append("")
            lines.append("⚠️ Worth your attention:")
            for c in top:
                lines.append(f"  • {c.title}")
        tasks = self._active_tasks()
        outstanding = [t for t in tasks
                       if str(t["status"]) in ("todo", "doing")]
        if outstanding:
            lines.append("")
            lines.append(f"📋 {len(outstanding)} task(s) outstanding")
        if top:
            lines.append("")
            lines.append(f"👉 Recommendation: {top[0].explanation or top[0].summary}")
        else:
            lines.append("")
            lines.append("👉 Nothing urgent right now.")
        text = "\n".join(lines)
        self._audit("proactive_briefing", detail={"candidates": len(cands)})
        return {"ok": True, "text": text, "candidates": [c.to_dict()
                                                         for c in cands]}

    # ==================================================================
    # 10. integrations
    # ==================================================================
    def _memory_prefs(self, now: int) -> dict[str, bool]:
        prefs = {"mute_low": False, "boost_risk": False}
        if self.memory is None or not hasattr(self.memory, "list"):
            return prefs
        try:
            rows = self.memory.list(active_only=True, limit=100, now=now)
        except Exception:  # noqa: BLE001
            return prefs
        for m in rows:
            val = str(m.get("value") or "").lower()
            if "don't bother" in val and "low" in val:
                prefs["mute_low"] = True
            if "know when" in val and "risk" in val:
                prefs["boost_risk"] = True
        return prefs

    def _reaction_rates(self) -> dict[str, dict[str, float]]:
        try:
            rows = self.db.query(
                "SELECT c.category AS category, r.response AS response, "
                "COUNT(*) AS n FROM proactive_responses r "
                "JOIN proactive_candidates c ON c.key=r.candidate_key "
                "GROUP BY c.category, r.response")
        except Exception:  # noqa: BLE001
            return {}
        agg: dict[str, dict[str, int]] = {}
        for r in rows:
            cat = str(r["category"])
            agg.setdefault(cat, {})[str(r["response"])] = int(r["n"])
        out = {}
        for cat, counts in agg.items():
            total = sum(counts.values()) or 1
            out[cat] = {"dismiss_rate": round(counts.get(DISMISSED, 0) / total, 4),
                        "accept_rate": round(counts.get(ACCEPTED, 0) / total, 4)}
        return out

    def check_web_change(self, *, subject: str, key: str, current_value: str,
                         source_url: str = "", now: int | None = None
                         ) -> dict[str, Any]:
        """Compare a verified external value with stored memory (M4 + M6)."""
        now = int(now if now is not None else self._now())
        if self.memory is None:
            return {"ok": False, "error": "memory unavailable"}
        rows = self.memory.search(f"{subject} {key}", scope="external", now=now)
        for m in rows:
            if str(m.get("subject")) == subject and str(m.get("key")) == key:
                if str(m.get("value") or "").strip() == str(current_value).strip():
                    return {"ok": True, "changed": False}
                self.memory.remember_web(
                    subject=subject, key=key, value=current_value,
                    source_url=source_url or str(m.get("source_detail") or ""),
                    observed_at=now, now=now)
                return {"ok": True, "changed": True, "old": m.get("value")}
        return {"ok": True, "changed": False, "reason": "no stored fact"}
