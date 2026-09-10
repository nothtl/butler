"""M5: advanced schedule optimization (deterministic).

Butler's :mod:`butler.schedule` module is a pure, deterministic solver that fits
tasks into the free gaps of a *single* day. M5 builds an **optimization layer
around** it rather than replacing it:

* the baseline solver stays exactly as it is (hard geometry is reused verbatim
  via :func:`butler.schedule.free_intervals` / :class:`~butler.schedule.Event`);
* this module adds a multi-day horizon, project/dependency awareness, an
  explicit hard-vs-soft constraint split, a configurable weighted objective and
  human-readable explanations;
* it is **read-only**: optimization never writes a plan, never touches Google
  Calendar and never executes a side effect. A proposal is data; committing it
  stays behind the existing safety / confirmation / undo path.

The engine is intentionally simple and bounded: a deterministic greedy
placement in dependency order, seeded by existing (already-committed) blocks so
re-optimization minimises churn, followed by a small local-improvement pass.
The :meth:`ScheduleOptimizer.optimize` interface is stable so a future
CP-SAT / Timefold backend can be dropped in without changing the executive
service, semantic models, MCP surface or safety layer.

HARD constraints are absolute and are never traded away for a soft objective:

* never overlap a hard calendar event;
* never schedule during sleep or outside the waking window;
* never exceed the real free capacity (buffer preserved);
* never place completed / cancelled / blocked tasks;
* respect explicit task dependencies and a valid project DAG;
* never place more than the task's remaining effort (never negative);
* no two sessions for the same task overlap, and a session never overlaps a
  hard event;
* respect deadlines where feasible, otherwise report an explicit violation.

Inferred preferences (routines, location, learned habits) are SOFT and can never
become hard constraints — the :class:`~butler.agent.semantic.Constraint` model
already enforces that structurally.
"""

from __future__ import annotations

import heapq
import logging
from dataclasses import dataclass, field, is_dataclass, fields
from datetime import datetime
from typing import Any

from . import schedule as sch

log = logging.getLogger("butler.optimizer")

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

#: Only these task statuses may be scheduled (mirrors ``schedule.ACTIVE_STATUSES``).
ACTIVE_STATUSES: tuple[str, ...] = ("todo", "doing", "scheduled")
#: Statuses that must never be scheduled.
BLOCKED_STATUSES: tuple[str, ...] = ("completed", "cancelled", "skipped", "blocked")

STRATEGIES: tuple[str, ...] = (
    "baseline", "deadline_first", "risk_first", "priority_first", "balanced",
)

#: Default weighted objective (sums to 1.0). Configurable per request.
DEFAULT_WEIGHTS: dict[str, float] = {
    "deadline_safety": 0.40,
    "risk_reduction": 0.20,
    "priority": 0.15,
    "dependency_progress": 0.10,
    "continuity": 0.05,
    "preference": 0.05,
    "churn": 0.05,
}

#: Weights used only to order tasks under the ``balanced`` strategy. Deadline
#: safety dominates; context affinity is the smallest term so it can never
#: override feasibility or a nearer deadline.
_ORDER_WEIGHTS: dict[str, float] = {
    "deadline": 0.45,
    "risk": 0.20,
    "priority": 0.20,
    "dependency": 0.10,
    "affinity": 0.05,
}

_OBJECTIVE_DESCRIPTIONS: dict[str, str] = {
    "deadline_safety": "deadline work fully scheduled before its deadline",
    "risk_reduction": "scheduled minutes weighted by project risk",
    "priority": "scheduled minutes weighted by priority",
    "dependency_progress": "dependency-bearing tasks completed",
    "continuity": "fewer, larger sessions (anti-fragmentation)",
    "preference": "explicit preferred windows honoured",
    "churn": "existing committed blocks preserved",
}

_HARD_VIOLATIONS = frozenset({
    "dependency_cycle", "deadline_miss", "pinned_conflict", "overdue",
    "hard_constraint_violation",
})


# ---------------------------------------------------------------------------
# serialisation helper
# ---------------------------------------------------------------------------
def _dump(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _dump(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _dump(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(v) for v in value]
    return value


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _rel_min(day_ts: int, ts: int) -> int:
    """Minute within ``day_ts`` (a local midnight) — timezone-safe."""
    return int((int(ts) - int(day_ts)) // 60)


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(int(minutes), 1439))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _iso(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------
@dataclass
class TaskItem:
    """A schedulable unit of work, independent of the DB row shape."""

    task_id: int
    title: str
    remaining_minutes: int
    priority: int = 3
    deadline: int = 0              # absolute epoch; 0 = none
    status: str = "todo"
    project_id: int = 0
    milestone_id: int = 0
    project_deadline: int = 0      # absolute epoch; 0 = none
    project_risk: float = 0.0      # 0..1 from ProjectIntelligence.risk
    project_risk_level: str = "unknown"
    deps: list[int] = field(default_factory=list)   # explicit prerequisites only
    affinity: int = 0
    effort_known: bool = True
    tags: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)


@dataclass
class SoftPreference:
    """A *soft* preferred window. Explicit user intent outranks inferred."""

    task_id: int
    start_min: int = 0
    end_min: int = 0
    source: str = "explicit_user"   # explicit_user | routine_inferred | ...
    weight: float = 1.0

    def is_explicit(self) -> bool:
        return self.source == "explicit_user"

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)


@dataclass
class ScheduledSession:
    task_id: int
    title: str
    start_ts: int
    end_ts: int
    duration: int
    start_min: int = 0              # minute within the session's day
    end_min: int = 0
    project_id: int = 0
    milestone_id: int = 0
    reason: str = ""
    pinned: bool = False
    objective_contributions: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = _dump(self)
        d["start"] = _hhmm(self.start_min)
        d["end"] = _hhmm(self.end_min)
        return d


@dataclass
class ScheduleConstraint:
    name: str
    hard: bool = True
    detail: str = ""
    satisfied: bool = True

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)


@dataclass
class ScheduleObjective:
    """A named soft objective, its configured weight and achieved value."""

    name: str
    weight: float = 0.0
    value: float = 0.0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)


@dataclass
class ScheduleExplanation:
    task_id: int
    text: str
    factors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)


@dataclass
class ScheduleRequest:
    """A complete, self-contained optimization request.

    ``day_starts`` are absolute local-midnight timestamps (computed by the
    caller) so the engine itself is timezone-agnostic and deterministic.
    """

    day_starts: list[int] = field(default_factory=list)
    tasks: list[TaskItem] = field(default_factory=list)
    events_by_day: dict[int, list[Any]] = field(default_factory=dict)
    day_start: int = 7 * 60
    day_end: int = 23 * 60
    sleep_start: int = 23 * 60
    sleep_end: int = 7 * 60
    buffer_fraction: float = 0.20
    buffer_minutes: int = 15
    min_slot_minutes: int = 25
    max_session_minutes: int = 90
    strategy: str = "balanced"
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    preferences: list[SoftPreference] = field(default_factory=list)
    existing: list[ScheduledSession] = field(default_factory=list)
    pinned: dict[int, list[int]] = field(default_factory=dict)  # task_id -> [start_ts, end_ts]
    max_iterations: int = 50
    now: int = 0
    label: str = ""

    def horizon_end(self) -> int:
        if not self.day_starts:
            return int(self.now or 0)
        return int(self.day_starts[-1]) + 86400

    def to_dict(self) -> dict[str, Any]:
        return _dump(self)


@dataclass
class OptimizationResult:
    feasible: bool
    strategy: str
    sessions: list[ScheduledSession] = field(default_factory=list)
    score: float = 0.0
    objective_breakdown: dict[str, float] = field(default_factory=dict)
    objectives: list[ScheduleObjective] = field(default_factory=list)
    unscheduled_items: list[dict[str, Any]] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    explanations: list[ScheduleExplanation] = field(default_factory=list)
    risk_summary: dict[str, Any] = field(default_factory=dict)
    churn: dict[str, Any] = field(default_factory=dict)
    constraints: list[ScheduleConstraint] = field(default_factory=list)
    horizon: dict[str, Any] = field(default_factory=dict)
    label: str = ""

    def slots(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.sessions]

    def to_dict(self) -> dict[str, Any]:
        d = _dump(self)
        d["slots"] = [s.to_dict() for s in self.sessions]
        return d


# ---------------------------------------------------------------------------
# interval pool helpers (pure)
# ---------------------------------------------------------------------------
def _usable_intervals(req: ScheduleRequest, day_ts: int) -> list[list[int]]:
    events = req.events_by_day.get(day_ts, [])
    gaps = sch.free_intervals(req.day_start, req.day_end,
                              req.sleep_start, req.sleep_end, events)
    out: list[list[int]] = []
    for a, b in gaps:
        b2 = b - req.buffer_minutes
        if b2 - a > 0:
            out.append([a, b2])
    return out


def _day_capacity(req: ScheduleRequest, intervals: list[list[int]]) -> int:
    total = sum(b - a for a, b in intervals)
    return max(0, int(total * (1.0 - req.buffer_fraction)))


def _find_place(intervals: list[list[int]], cap_left: int, earliest_min: int,
                deadline_min: int | None, want: int, max_len: int,
                min_slot: int) -> tuple[int, int, int] | None:
    if cap_left <= 0:
        return None
    for idx, (a, b) in enumerate(intervals):
        end_limit = b if deadline_min is None else min(b, deadline_min)
        start = max(a, earliest_min)
        if end_limit <= start:
            continue
        chunk = min(want, max_len, end_limit - start, cap_left)
        if chunk <= 0:
            continue
        if chunk < min_slot and chunk < want:
            continue
        return start, start + chunk, idx
    return None


def _find_window(intervals: list[list[int]], cap_left: int, pstart: int,
                 pend: int, earliest_min: int, deadline_min: int | None,
                 want: int, max_len: int,
                 min_slot: int) -> tuple[int, int, int] | None:
    if cap_left <= 0:
        return None
    for idx, (a, b) in enumerate(intervals):
        if pstart < a or pend > b:
            continue
        start = max(pstart, earliest_min)
        end_limit = pend if deadline_min is None else min(pend, deadline_min)
        if end_limit <= start:
            continue
        chunk = min(want, max_len, end_limit - start, cap_left)
        if chunk <= 0:
            continue
        if chunk < min_slot and chunk < want:
            continue
        return start, start + chunk, idx
    return None


def _consume(intervals: list[list[int]], idx: int, start: int, end: int) -> None:
    a, b = intervals[idx]
    if start <= a and end >= b:
        intervals.pop(idx)
    elif start <= a:
        intervals[idx][0] = end
    elif end >= b:
        intervals[idx][1] = start
    else:
        intervals[idx] = [a, start]
        intervals.insert(idx + 1, [end, b])


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------
def _deadline_urgency(t: TaskItem, now: int) -> float:
    if not t.deadline:
        return 0.0
    hours = (t.deadline - now) / 3600.0
    if hours <= 0:
        return 1.0
    return _clamp(1.0 / (1.0 + hours / 24.0))


def _strategy_score(t: TaskItem, strategy: str, now: int,
                    inferred_pref: float = 0.0) -> tuple:
    deadline = t.deadline or (1 << 62)
    if strategy == "deadline_first":
        return (deadline, -t.priority, t.remaining_minutes, t.task_id)
    if strategy == "priority_first":
        return (-t.priority, deadline, t.remaining_minutes, t.task_id)
    if strategy == "risk_first":
        return (-round(t.project_risk, 6), deadline, -t.priority, t.task_id)
    if strategy == "baseline":
        return (deadline, -t.priority, t.remaining_minutes, -t.affinity, t.task_id)
    # balanced
    du = _deadline_urgency(t, now)
    risk = _clamp(t.project_risk)
    prio = (int(t.priority) - 1) / 4.0
    dep = 1.0 if t.deps else 0.5
    aff = _clamp(t.affinity / 8.0 + inferred_pref)
    score = (_ORDER_WEIGHTS["deadline"] * du + _ORDER_WEIGHTS["risk"] * risk
             + _ORDER_WEIGHTS["priority"] * prio
             + _ORDER_WEIGHTS["dependency"] * dep
             + _ORDER_WEIGHTS["affinity"] * aff)
    return (-round(score, 6), deadline, -t.priority, t.task_id)


def _topological_order(tasks: list[TaskItem], strategy: str, now: int,
                       inferred_prefs: dict[int, float]
                       ) -> tuple[list[TaskItem], list[list[int]]]:
    """Kahn topological sort with a strategy-keyed ready heap.

    Returns ``(order, cycles)``. Explicit dependencies only; inferred edges are
    advisory and never constrain the order.
    """
    by_id = {t.task_id: t for t in tasks}
    indeg: dict[int, int] = {t.task_id: 0 for t in tasks}
    dependents: dict[int, list[int]] = {}
    for t in tasks:
        for d in t.deps:
            if d in by_id:
                indeg[t.task_id] += 1
                dependents.setdefault(d, []).append(t.task_id)
    heap: list[tuple] = []
    for t in tasks:
        if indeg[t.task_id] == 0:
            heapq.heappush(heap, (_strategy_score(t, strategy, now,
                                                  inferred_prefs.get(t.task_id, 0.0)),
                                  t.task_id))
    order: list[TaskItem] = []
    while heap:
        _, tid = heapq.heappop(heap)
        t = by_id[tid]
        order.append(t)
        for nxt in dependents.get(tid, []):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                n = by_id[nxt]
                heapq.heappush(heap, (_strategy_score(
                    n, strategy, now, inferred_prefs.get(nxt, 0.0)), nxt))
    cycles: list[list[int]] = []
    if len(order) != len(tasks):
        remaining = [t for t in tasks if t not in order]
        cycles.append([t.task_id for t in remaining])
    return order, cycles


# ---------------------------------------------------------------------------
# the optimizer
# ---------------------------------------------------------------------------
class ScheduleOptimizer:
    """Deterministic multi-day schedule optimizer (read-only)."""

    def __init__(self, container: Any = None, *, clock: Any = None):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self._clock = clock

    # ------------------------------------------------------------- interface
    def optimize(self, request: ScheduleRequest) -> OptimizationResult:
        """Run the deterministic optimizer. Never mutates the request."""
        return _optimize(request)

    # --------------------------------------------------------- state -> request
    def build_request(self, *, days: int = 1, strategy: str = "balanced",
                      day_ts: int | None = None, now: int | None = None,
                      include_existing: bool = True,
                      pinned: dict[int, list[int]] | None = None
                      ) -> ScheduleRequest:
        """Read live state and produce a self-contained optimization request."""
        req = ScheduleRequest(strategy=strategy)
        c = self.container
        planner = getattr(c, "planner", None)
        db = getattr(c, "db", None)
        cfg = self.cfg
        if planner is None or db is None:
            return req
        try:
            now = int(now if now is not None else self._now())
            day_ts = int(day_ts if day_ts is not None else planner._today())
            base = int(planner._day_start_ts(day_ts))
            day_starts = []
            d = base
            for _ in range(max(1, int(days))):
                day_starts.append(d)
                d = int(planner._day_start_ts(d + 86400))
            req.day_starts = day_starts
            req.now = now
            req.day_start = int(planner.day_start)
            req.day_end = int(planner.day_end)
            req.sleep_start = int(cfg.sleep_start)
            req.sleep_end = int(cfg.sleep_end)
            req.buffer_fraction = float(cfg.buffer_fraction)
            req.buffer_minutes = int(cfg.buffer_minutes)
            req.min_slot_minutes = int(cfg.min_slot_minutes)
            req.max_session_minutes = int(getattr(cfg, "optimizer_max_session_minutes", 90))
            req.max_iterations = int(getattr(cfg, "optimizer_max_iterations", 50))
            req.pinned = dict(pinned or {})

            req.events_by_day = {}
            for day in day_starts:
                try:
                    req.events_by_day[day] = list(planner._day_events(day))
                except Exception:  # noqa: BLE001
                    req.events_by_day[day] = []

            req.tasks = self._read_tasks(db, now)
            if include_existing:
                req.existing = self._read_existing(db, day_starts)
        except Exception as exc:  # noqa: BLE001 — degrade to an empty request
            log.warning("optimizer build_request failed: %s", exc, exc_info=True)
        return req

    def optimize_from_state(self, **kwargs: Any) -> OptimizationResult:
        return self.optimize(self.build_request(**kwargs))

    # -------------------------------------------------------------- readers
    def _read_tasks(self, db: Any, now: int) -> list[TaskItem]:
        out: list[TaskItem] = []
        pmod = getattr(self.container, "projects", None)
        for row in db.tasks("active"):
            try:
                status = str(row["status"] or "todo")
                if status not in ACTIVE_STATUSES:
                    continue
                est = int(row["est_minutes"] or 0)
                rem = int(row["remaining_minutes"] or 0)
                remaining = rem if rem > 0 else max(1, est or 60)
                pid = int(row["project_id"] or 0)
                mid = int(row["milestone_id"] or 0)
                deps = [int(d["depends_on"]) for d in db.task_dependencies(int(row["id"]))
                        if not int(d["inferred"] or 0)]
                risk = 0.0
                level = "unknown"
                pdeadline = 0
                if pid:
                    if pmod is not None and hasattr(pmod, "risk"):
                        r = pmod.risk(pid, now=now)
                        if r:
                            risk = float(r.get("score") or 0.0)
                            level = str(r.get("level") or "unknown")
                    proj = db.project_by_id(pid)
                    if proj is not None:
                        pdeadline = int(proj["deadline"] or 0)
                out.append(TaskItem(
                    task_id=int(row["id"]), title=str(row["title"] or ""),
                    remaining_minutes=int(remaining), priority=int(row["priority"] or 3),
                    deadline=int(row["deadline"] or 0), status=status,
                    project_id=pid, milestone_id=mid,
                    project_deadline=pdeadline, project_risk=risk,
                    project_risk_level=level, deps=deps,
                    affinity=0, effort_known=bool(est or rem),
                    tags=str(row["tags"] or "")))
            except Exception:  # noqa: BLE001 — one bad row must not kill the plan
                continue
        return out

    def _read_existing(self, db: Any, day_starts: list[int]) -> list[ScheduledSession]:
        if not day_starts:
            return []
        try:
            row = db.latest_plan()
            if row is None:
                return []
            state = sch.PlanState.from_json(str(row["json"]))
            midnight = int(self.container.planner._day_start_ts(int(row["created"])))
            if midnight not in day_starts:
                return []
            out: list[ScheduledSession] = []
            for s in state.slots:
                out.append(ScheduledSession(
                    task_id=int(s.task_id), title=str(s.title),
                    start_ts=midnight + int(s.start_min) * 60,
                    end_ts=midnight + int(s.end_min) * 60,
                    duration=int(s.end_min - s.start_min), pinned=True,
                    reason="kept in place to avoid unnecessary churn"))
            return out
        except Exception:  # noqa: BLE001
            return []

    def _now(self) -> int:
        if self._clock is not None and hasattr(self._clock, "now_ts"):
            try:
                return int(self._clock.now_ts())
            except Exception:  # noqa: BLE001
                pass
        cfg = self.cfg
        if cfg is not None and hasattr(cfg, "now_local"):
            try:
                return int(cfg.now_local().timestamp())
            except Exception:  # noqa: BLE001
                pass
        import time
        return int(time.time())


# ---------------------------------------------------------------------------
# core algorithm (pure)
# ---------------------------------------------------------------------------
def _optimize(req: ScheduleRequest) -> OptimizationResult:
    weights = dict(DEFAULT_WEIGHTS)
    weights.update(req.weights or {})
    strategy = req.strategy if req.strategy in STRATEGIES else "balanced"
    now = int(req.now or (req.day_starts[0] if req.day_starts else 0))
    day_starts = sorted(int(d) for d in req.day_starts)

    result = OptimizationResult(feasible=True, strategy=strategy, label=req.label)
    result.horizon = {
        "days": len(day_starts),
        "start": day_starts[0] if day_starts else 0,
        "end": (day_starts[-1] + 86400) if day_starts else 0,
        "start_iso": _iso(day_starts[0]) if day_starts else "",
        "end_iso": _iso(day_starts[-1] + 86400) if day_starts else "",
    }
    if not day_starts:
        result.feasible = False
        result.violations.append({"kind": "hard_constraint_violation",
                                  "detail": "empty horizon"})
        return result

    active = [t for t in req.tasks if t.status in ACTIVE_STATUSES
              and t.remaining_minutes > 0]
    # Hard structural constraints -------------------------------------------------
    result.constraints = [
        ScheduleConstraint("no_overlap_with_hard_events", True, "events are immovable"),
        ScheduleConstraint("sleep_protected", True, "no work during sleep"),
        ScheduleConstraint("waking_bounds", True, "inside the working window"),
        ScheduleConstraint("capacity_respected", True, "buffer never consumed"),
        ScheduleConstraint("active_tasks_only", True, "completed/cancelled excluded"),
        ScheduleConstraint("dependencies_ordered", True, "explicit deps before dependents"),
        ScheduleConstraint("remaining_effort", True, "never exceed remaining work"),
    ]

    inferred_prefs: dict[int, float] = {}
    explicit_prefs: dict[int, list[SoftPreference]] = {}
    for p in req.preferences:
        if p.is_explicit():
            explicit_prefs.setdefault(p.task_id, []).append(p)
        else:
            inferred_prefs[p.task_id] = max(inferred_prefs.get(p.task_id, 0.0),
                                            float(p.weight))

    order, cycles = _topological_order(active, strategy, now, inferred_prefs)
    if cycles:
        result.feasible = False
        result.violations.append({
            "kind": "dependency_cycle", "hard": True,
            "tasks": cycles[0],
            "detail": "explicit dependencies contain a cycle"})

    # Per-day mutable state -------------------------------------------------------
    pools: dict[int, list[list[int]]] = {}
    caps: dict[int, int] = {}

    def pool(day: int) -> list[list[int]]:
        if day not in pools:
            pools[day] = _usable_intervals(req, day)
            caps[day] = _day_capacity(req, pools[day])
        return pools[day]

    sessions: list[ScheduledSession] = []
    finish: dict[int, int] = {}
    placed: dict[int, int] = {}
    remaining_by_id = {t.task_id: t.remaining_minutes for t in active}
    by_id = {t.task_id: t for t in active}

    def _day_of(ts: int) -> int | None:
        for d in day_starts:
            if d <= ts < d + 86400:
                return d
        return None

    # 1. Pin forced windows (hard directive) -------------------------------------
    for tid, window in (req.pinned or {}).items():
        t = by_id.get(int(tid))
        if t is None:
            continue
        try:
            start_ts, end_ts = int(window[0]), int(window[1])
        except (TypeError, IndexError, ValueError):
            result.violations.append({"kind": "pinned_conflict", "hard": True,
                                      "task_id": int(tid),
                                      "detail": "malformed pinned window"})
            result.feasible = False
            continue
        day = _day_of(start_ts)
        if day is None or end_ts <= start_ts:
            result.feasible = False
            result.violations.append({"kind": "pinned_conflict", "hard": True,
                                      "task_id": int(tid),
                                      "detail": "pinned window is outside the horizon"})
            continue
        sm, em = _rel_min(day, start_ts), _rel_min(day, end_ts)
        ivs = pool(day)
        placed_ok = False
        for idx, (a, b) in enumerate(ivs):
            if sm >= a and em <= b and (caps[day] - (em - sm)) >= 0:
                _consume(ivs, idx, sm, em)
                caps[day] -= (em - sm)
                sessions.append(ScheduledSession(
                    task_id=t.task_id, title=t.title, start_ts=start_ts,
                    end_ts=end_ts, duration=em - sm, start_min=sm, end_min=em,
                    project_id=t.project_id, milestone_id=t.milestone_id,
                    pinned=True, reason="forced to the requested window"))
                finish[t.task_id] = end_ts
                placed[t.task_id] = placed.get(t.task_id, 0) + (em - sm)
                placed_ok = True
                break
        if not placed_ok:
            result.feasible = False
            result.violations.append({
                "kind": "pinned_conflict", "hard": True, "task_id": t.task_id,
                "detail": "requested window overlaps a hard event or is full"})

    # 2. Pin still-valid existing blocks (minimise churn) ------------------------
    for ex in req.existing:
        t = by_id.get(ex.task_id)
        if t is None:
            continue
        if placed.get(ex.task_id, 0) >= t.remaining_minutes:
            continue
        day = _day_of(ex.start_ts)
        if day is None:
            continue
        sm, em = _rel_min(day, ex.start_ts), _rel_min(day, ex.end_ts)
        if em <= sm:
            continue
        if t.deadline and ex.end_ts > t.deadline:
            continue
        ivs = pool(day)
        for idx, (a, b) in enumerate(ivs):
            if sm >= a and em <= b and (caps[day] - (em - sm)) >= 0:
                _consume(ivs, idx, sm, em)
                caps[day] -= (em - sm)
                sessions.append(ScheduledSession(
                    task_id=t.task_id, title=t.title, start_ts=ex.start_ts,
                    end_ts=ex.end_ts, duration=em - sm, start_min=sm, end_min=em,
                    project_id=t.project_id, milestone_id=t.milestone_id,
                    pinned=True, reason="kept in place to avoid unnecessary churn"))
                finish[t.task_id] = max(finish.get(t.task_id, 0), ex.end_ts)
                placed[t.task_id] = placed.get(t.task_id, 0) + (em - sm)
                break

    # 3. Greedy placement in dependency order ------------------------------------
    for t in order:
        want = remaining_by_id[t.task_id] - placed.get(t.task_id, 0)
        if want <= 0:
            continue
        # explicit dependencies must be fully scheduled first
        blocked = [d for d in t.deps
                   if d in by_id and placed.get(d, 0) < remaining_by_id[d]]
        if blocked:
            result.unscheduled_items.append({
                "task_id": t.task_id, "title": t.title,
                "remaining_minutes": want, "reason": "waiting on dependency",
                "blocked_by": blocked})
            if t.deadline:
                result.violations.append({
                    "kind": "deadline_miss", "hard": True,
                    "task_id": t.task_id, "title": t.title,
                    "detail": "prerequisite unfinished"})
            continue
        deadline_ts = int(t.deadline or 0)
        if deadline_ts and deadline_ts <= now:
            result.unscheduled_items.append({
                "task_id": t.task_id, "title": t.title,
                "remaining_minutes": want, "reason": "deadline already passed"})
            result.violations.append({
                "kind": "overdue", "hard": True, "task_id": t.task_id,
                "title": t.title, "detail": "deadline is in the past"})
            continue

        earliest = now
        for d in t.deps:
            if d in finish:
                earliest = max(earliest, finish[d])

        # explicit preferred windows are honoured before the default earliest fit
        for pref in sorted(explicit_prefs.get(t.task_id, []),
                           key=lambda p: (p.start_min, p.end_min)):
            if want <= 0:
                break
            got = _place_pref(req, t, pref, want, earliest, deadline_ts, pool, caps,
                              sessions)
            if got is not None:
                used, end_ts = got
                want -= used
                placed[t.task_id] = placed.get(t.task_id, 0) + used
                finish[t.task_id] = max(finish.get(t.task_id, 0), end_ts)

        while want > 0:
            got = _place_next(req, t, want, earliest, deadline_ts, pool, caps,
                              sessions)
            if got is None:
                break
            used, end_ts = got
            want -= used
            placed[t.task_id] = placed.get(t.task_id, 0) + used
            finish[t.task_id] = max(finish.get(t.task_id, 0), end_ts)

        if want > 0:
            result.unscheduled_items.append({
                "task_id": t.task_id, "title": t.title,
                "remaining_minutes": want,
                "reason": "not enough free capacity before the deadline"
                if t.deadline else "not enough free capacity in the horizon"})
            if t.deadline:
                result.violations.append({
                    "kind": "deadline_miss", "hard": True,
                    "task_id": t.task_id, "title": t.title,
                    "detail": f"{want}m of work does not fit before "
                              f"{_iso(t.deadline)}"})

    sessions.sort(key=lambda s: (s.start_ts, s.task_id))
    result.sessions = sessions

    hard = [v for v in result.violations if v.get("kind") in _HARD_VIOLATIONS]
    result.feasible = not hard

    # 4. Explainability ----------------------------------------------------------
    for s in sessions:
        t = by_id.get(s.task_id)
        if t is None:
            continue
        factors: list[str] = []
        if s.pinned:
            factors.append("preserved from the current schedule")
        if t.deadline:
            factors.append(f"deadline {_iso(t.deadline)}")
        if t.project_id:
            factors.append(f"project risk {t.project_risk_level}")
        if t.deps:
            factors.append("after its prerequisites")
        if t.priority >= 4:
            factors.append(f"priority {t.priority}")
        reason = _reason_for(s, t, now)
        result.explanations.append(ScheduleExplanation(
            task_id=s.task_id, text=reason, factors=factors))

    result.churn = _churn(req.existing, sessions)
    result.objective_breakdown = _objective_breakdown(req, active, sessions,
                                                       weights, result)
    result.objectives = [
        ScheduleObjective(name=k, weight=float(weights.get(k, 0.0)),
                          value=float(result.objective_breakdown.get(k, 0.0)),
                          description=_OBJECTIVE_DESCRIPTIONS.get(k, ""))
        for k in result.objective_breakdown]
    result.score = round(sum(weights.get(k, 0.0) * v
                             for k, v in result.objective_breakdown.items()), 4)
    result.risk_summary = _risk_summary(req, active, sessions, day_starts, now)

    # soft (non-hard) issues are surfaced but do not make the plan infeasible
    for item in result.unscheduled_items:
        if not any(v.get("task_id") == item["task_id"] for v in result.violations):
            result.constraints.append(ScheduleConstraint(
                "capacity_soft", False,
                f"{item['title']}: {item['reason']}", satisfied=False))
    return result


def _place_next(req: ScheduleRequest, t: TaskItem, want: int, earliest: int,
                deadline_ts: int, pool: Any, caps: dict[int, int],
                sessions: list[ScheduledSession]) -> tuple[int, int] | None:
    for day in req.day_starts:
        if day + 86400 <= earliest:
            continue
        if deadline_ts and day > deadline_ts:
            break
        ivs = pool(day)
        earliest_min = req.day_start
        if earliest >= day:
            earliest_min = max(req.day_start, _rel_min(day, earliest))
        deadline_min = None
        if deadline_ts and deadline_ts < day + 86400:
            deadline_min = _rel_min(day, deadline_ts)
        got = _find_place(ivs, caps[day], earliest_min, deadline_min, want,
                          req.max_session_minutes, req.min_slot_minutes)
        if got is None:
            continue
        start_min, end_min, idx = got
        used = end_min - start_min
        _consume(ivs, idx, start_min, end_min)
        caps[day] -= used
        sessions.append(ScheduledSession(
            task_id=t.task_id, title=t.title,
            start_ts=day + start_min * 60, end_ts=day + end_min * 60,
            duration=used, start_min=start_min, end_min=end_min,
            project_id=t.project_id, milestone_id=t.milestone_id,
            reason="earliest feasible fit"))
        return used, day + end_min * 60
    return None


def _place_pref(req: ScheduleRequest, t: TaskItem, pref: SoftPreference, want: int,
                earliest: int, deadline_ts: int, pool: Any, caps: dict[int, int],
                sessions: list[ScheduledSession]) -> tuple[int, int] | None:
    for day in req.day_starts:
        if day + 86400 <= earliest:
            continue
        if deadline_ts and day > deadline_ts:
            break
        if deadline_ts and deadline_ts < day + 86400 \
                and _rel_min(day, deadline_ts) <= pref.start_min:
            continue
        ivs = pool(day)
        earliest_min = req.day_start
        if earliest >= day:
            earliest_min = max(req.day_start, _rel_min(day, earliest))
        deadline_min = None
        if deadline_ts and deadline_ts < day + 86400:
            deadline_min = _rel_min(day, deadline_ts)
        got = _find_window(ivs, caps[day], pref.start_min, pref.end_min,
                           earliest_min, deadline_min, want,
                           req.max_session_minutes, req.min_slot_minutes)
        if got is None:
            continue
        start_min, end_min, idx = got
        used = end_min - start_min
        _consume(ivs, idx, start_min, end_min)
        caps[day] -= used
        sessions.append(ScheduledSession(
            task_id=t.task_id, title=t.title,
            start_ts=day + start_min * 60, end_ts=day + end_min * 60,
            duration=used, start_min=start_min, end_min=end_min,
            project_id=t.project_id, milestone_id=t.milestone_id,
            reason="placed in the time you explicitly asked for"))
        return used, day + end_min * 60
    return None


def _reason_for(s: ScheduledSession, t: TaskItem, now: int) -> str:
    if s.pinned and "churn" in s.reason:
        return (f"{t.title} kept at {_hhmm(s.start_min)}-{_hhmm(s.end_min)} "
                f"to avoid unnecessary churn.")
    if s.pinned:
        return (f"{t.title} placed at {_hhmm(s.start_min)}-{_hhmm(s.end_min)} "
                f"because you asked for that window.")
    if "explicitly asked" in s.reason:
        return (f"{t.title} scheduled at {_hhmm(s.start_min)}-{_hhmm(s.end_min)} "
                f"because you explicitly asked to work on it then.")
    bits = []
    if t.deadline:
        bits.append(f"it is due {_iso(t.deadline)}")
    if t.project_id and t.project_risk_level in ("high", "critical"):
        bits.append(f"its project risk is {t.project_risk_level}")
    if t.deps:
        bits.append("its prerequisites are finished")
    if t.priority >= 4:
        bits.append(f"it is priority {t.priority}")
    if not bits:
        bits.append("it is the next feasible work in the plan")
    return (f"{t.title} scheduled at {_hhmm(s.start_min)}-{_hhmm(s.end_min)} "
            f"because " + ", ".join(bits) + ".")


def _churn(existing: list[ScheduledSession],
           sessions: list[ScheduledSession]) -> dict[str, Any]:
    if not existing:
        return {"moved": [], "added": [], "removed": [], "unchanged": [],
                "counts": {"moved": 0, "added": 0, "removed": 0, "unchanged": 0}}
    old: dict[int, ScheduledSession] = {}
    for s in existing:
        old.setdefault(s.task_id, s)
    new: dict[int, ScheduledSession] = {}
    for s in sessions:
        new.setdefault(s.task_id, s)
    moved, added, removed, unchanged = [], [], [], []
    for tid, ns in new.items():
        os_ = old.get(tid)
        if os_ is None:
            added.append({"task_id": tid, "title": ns.title,
                          "new": [ns.start_ts, ns.end_ts]})
        elif (os_.start_ts, os_.end_ts) != (ns.start_ts, ns.end_ts):
            moved.append({"task_id": tid, "title": ns.title,
                          "old": [os_.start_ts, os_.end_ts],
                          "new": [ns.start_ts, ns.end_ts]})
        else:
            unchanged.append({"task_id": tid, "title": ns.title,
                              "at": [ns.start_ts, ns.end_ts]})
    for tid, os_ in old.items():
        if tid not in new:
            removed.append({"task_id": tid, "title": os_.title,
                            "old": [os_.start_ts, os_.end_ts]})
    return {"moved": moved, "added": added, "removed": removed,
            "unchanged": unchanged,
            "counts": {"moved": len(moved), "added": len(added),
                       "removed": len(removed), "unchanged": len(unchanged)}}


def _objective_breakdown(req: ScheduleRequest, active: list[TaskItem],
                         sessions: list[ScheduledSession],
                         weights: dict[str, float],
                         result: OptimizationResult) -> dict[str, float]:
    total_min = sum(s.duration for s in sessions) or 1
    by_id = {t.task_id: t for t in active}

    deadline_tasks = [t for t in active if t.deadline]
    if deadline_tasks:
        met = 0
        for t in deadline_tasks:
            placed = sum(s.duration for s in sessions if s.task_id == t.task_id)
            if placed >= t.remaining_minutes and all(
                    s.end_ts <= t.deadline for s in sessions
                    if s.task_id == t.task_id):
                met += 1
        deadline_safety = met / len(deadline_tasks)
    else:
        deadline_safety = 1.0

    risk_reduction = sum(by_id[s.task_id].project_risk * s.duration
                         for s in sessions if s.task_id in by_id) / total_min
    priority = sum(((int(by_id[s.task_id].priority) - 1) / 4.0) * s.duration
                   for s in sessions if s.task_id in by_id) / total_min

    with_deps = [t for t in active if t.deps]
    if with_deps:
        done = sum(1 for t in with_deps
                   if sum(s.duration for s in sessions if s.task_id == t.task_id)
                   >= t.remaining_minutes)
        dependency_progress = done / len(with_deps)
    else:
        dependency_progress = 1.0

    scheduled_tasks = {s.task_id for s in sessions}
    fragmentation = (len(sessions) - len(scheduled_tasks)) / max(1, len(scheduled_tasks))
    continuity = _clamp(1.0 - fragmentation / 3.0)

    explicit = [p for p in req.preferences if p.is_explicit()]
    if explicit:
        honored = 0
        for p in explicit:
            for s in sessions:
                if s.task_id == p.task_id and s.start_min >= p.start_min \
                        and s.end_min <= p.end_min:
                    honored += 1
                    break
        preference = honored / len(explicit)
    else:
        preference = 1.0

    counts = result.churn.get("counts", {})
    if req.existing:
        churn = counts.get("unchanged", 0) / max(1, len(req.existing))
    else:
        churn = 1.0

    return {
        "deadline_safety": round(_clamp(deadline_safety), 4),
        "risk_reduction": round(_clamp(risk_reduction), 4),
        "priority": round(_clamp(priority), 4),
        "dependency_progress": round(_clamp(dependency_progress), 4),
        "continuity": round(_clamp(continuity), 4),
        "preference": round(_clamp(preference), 4),
        "churn": round(_clamp(churn), 4),
    }


def _risk_summary(req: ScheduleRequest, active: list[TaskItem],
                  sessions: list[ScheduledSession], day_starts: list[int],
                  now: int) -> dict[str, Any]:
    # capacity before a horizon-relative deadline, using per-day geometry
    def capacity_until(deadline_ts: int) -> int:
        total = 0
        for day in day_starts:
            if deadline_ts and day > deadline_ts:
                break
            ivs = _usable_intervals(req, day)
            if deadline_ts and deadline_ts < day + 86400:
                dm = _rel_min(day, deadline_ts)
                ivs = [[a, min(b, dm)] for a, b in ivs if min(b, dm) - a > 0]
            total += sum(b - a for a, b in ivs)
        return total

    projects: dict[int, dict[str, Any]] = {}
    for t in active:
        if not t.project_id:
            continue
        p = projects.setdefault(t.project_id, {
            "project_id": t.project_id, "remaining_minutes": 0,
            "deadline": t.project_deadline or 0, "risk": t.project_risk,
            "risk_level": t.project_risk_level, "tasks": 0})
        p["remaining_minutes"] += t.remaining_minutes
        p["tasks"] += 1
        if t.deadline and (not p["deadline"] or t.deadline < p["deadline"]):
            p["deadline"] = t.deadline
    for p in projects.values():
        placed = sum(s.duration for s in sessions
                     if any(t.project_id == p["project_id"]
                            for t in active if t.task_id == s.task_id))
        remaining = max(0, p["remaining_minutes"] - placed)
        avail = capacity_until(p["deadline"]) if p["deadline"] else None
        p["scheduled_minutes"] = placed
        p["unplaced_minutes"] = remaining
        p["available_minutes"] = avail
        p["slack_minutes"] = (avail - p["remaining_minutes"]) \
            if avail is not None else None
        if avail is None:
            p["pressure"] = None
        elif remaining <= 0:
            p["pressure"] = 0.0
        elif avail <= 0:
            p["pressure"] = 1.0
        else:
            p["pressure"] = round(_clamp(remaining / float(avail)), 4)
    items = sorted(projects.values(),
                   key=lambda p: (-(p["pressure"] or 0.0), p["project_id"]))
    return {
        "projects": items,
        "most_at_risk": items[0] if items else None,
        "overall_pressure": round(max((p["pressure"] or 0.0) for p in items), 4)
        if items else 0.0,
    }


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------
def optimize(request: ScheduleRequest) -> OptimizationResult:
    """Module-level entry point mirroring ``ScheduleOptimizer.optimize``."""
    return _optimize(request)


def objective_weights() -> dict[str, float]:
    return dict(DEFAULT_WEIGHTS)
