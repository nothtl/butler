"""Phase 2: deterministic constraint solver for daily scheduling.

This module is PURE — no LLM, no I/O, no network. It computes, given a set of
tasks, hard calendar events and capacity rules, an allocation of task *sessions*
to time slots that satisfies every hard constraint:

  * a task never overlaps a hard event (lecture / external commitment);
  * nothing is scheduled during sleep hours;
  * the scheduler never fills 100% of available time (buffer);
  * completed tasks are removed from the plan;
  * a task only extends if the free time exists (and never into an event);
  * partial availability: a task needing more than a gap can use that gap for
    the portion that fits (as long as the gap >= min_slot_minutes);
  * a new hard event only moves the *soft* blocks that would overlap it.

All ordering is deterministic. The outer wrapper (``butler/planner.py``) is
responsible for talking to the database and Google Calendar; this module only
decides *where* things go.

Times are integer "minutes within the day" (0..1440) unless stated otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------
@dataclass
class Task:
    id: int
    title: str
    remaining_minutes: int          # total work still needed
    deadline: int | None = None     # minutes-within-day it must finish by
    priority: int = 3               # 1..5
    status: str = "todo"            # todo | doing  (done/skipped are excluded)
    color: str = ""
    tags: str = ""

    def urgency_key(self) -> tuple:
        """Lower is sooner. Deterministic tiebreak by id."""

        d = self.deadline if self.deadline is not None else 10**9
        return (d, -self.priority, self.remaining_minutes, self.id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Task":
        return cls(
            id=int(d["id"]), title=str(d["title"]),
            remaining_minutes=int(d["remaining_minutes"]),
            deadline=int(d["deadline"]) if d.get("deadline") else None,
            priority=int(d.get("priority", 3)), status=str(d.get("status", "todo")),
            color=str(d.get("color", "")), tags=str(d.get("tags", "")),
        )


@dataclass
class Event:
    id: int
    title: str
    start_min: int                  # minutes-within-day
    end_min: int
    source: str = "local"           # local | google

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            id=int(d["id"]), title=str(d["title"]),
            start_min=int(d["start_min"]), end_min=int(d["end_min"]),
            source=str(d.get("source", "local")),
        )


@dataclass
class Slot:
    task_id: int
    title: str
    start_min: int
    end_min: int
    partial: bool = False           # True if task continues in a later slot

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Slot":
        return cls(
            task_id=int(d["task_id"]), title=str(d["title"]),
            start_min=int(d["start_min"]), end_min=int(d["end_min"]),
            partial=bool(d.get("partial", False)),
        )


@dataclass
class PlanState:
    """A solved day: the slots plus what we need to undo back to."""

    day_start: int
    day_end: int
    events: list[Event] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    # snapshot of task states before this solve (for undo of schedule moves)
    snapshot: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "day_start": self.day_start,
            "day_end": self.day_end,
            "events": [e.to_dict() for e in self.events],
            "slots": [s.to_dict() for s in self.slots],
            "snapshot": self.snapshot,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanState":
        return cls(
            day_start=int(d["day_start"]), day_end=int(d["day_end"]),
            events=[Event.from_dict(x) for x in d.get("events", [])],
            slots=[Slot.from_dict(x) for x in d.get("slots", [])],
            snapshot=dict(d.get("snapshot", {})),
            notes=list(d.get("notes", [])),
        )

    @classmethod
    def from_json(cls, s: str) -> "PlanState":
        return cls.from_dict(json.loads(s))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))


# ---------------------------------------------------------------------------
# geometry helpers (pure)
# ---------------------------------------------------------------------------
def _clip(a: int, b: int, lo: int, hi: int) -> tuple[int, int] | None:
    a = max(a, lo)
    b = min(b, hi)
    if b <= a:
        return None
    return (a, b)


def sleep_blocks(day_start: int, day_end: int,
                 sleep_start: int, sleep_end: int) -> list[tuple[int, int]]:
    """Sleep periods clipped to the day window (handles wrap past midnight)."""
    out: list[tuple[int, int]] = []
    if sleep_start <= sleep_end:
        b = _clip(sleep_start, sleep_end, day_start, day_end)
        if b:
            out.append(b)
    else:
        b1 = _clip(sleep_start, 1440, day_start, day_end)
        b2 = _clip(0, sleep_end, day_start, day_end)
        if b1:
            out.append(b1)
        if b2:
            out.append(b2)
    return out


def _reserved(events: list[Event], sleep: list[tuple[int, int]]) -> list[tuple[int, int]]:
    blocks = [(e.start_min, e.end_min) for e in events if e.end_min > e.start_min]
    blocks += sleep
    blocks.sort()
    merged: list[tuple[int, int]] = []
    for a, b in blocks:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def free_intervals(day_start: int, day_end: int, sleep_start: int,
                   sleep_end: int, events: list[Event]) -> list[tuple[int, int]]:
    """Waking free time = day window minus sleep minus events."""
    res = _reserved(events, sleep_blocks(day_start, day_end, sleep_start, sleep_end))
    gaps: list[tuple[int, int]] = []
    cur = day_start
    for a, b in res:
        if a > cur:
            gaps.append((cur, a))
        cur = max(cur, b)
    if cur < day_end:
        gaps.append((cur, day_end))
    return gaps


def day_capacity(day_start: int, day_end: int, events: list[Event],
                 buffer_fraction: float, buffer_minutes: int,
                 min_slot_minutes: int, sleep_start: int,
                 sleep_end: int, deadline_min: int | None = None) -> int:
    """Deterministic usable minutes for one waking day.

    Free time is the day window minus sleep minus hard events; each gap keeps
    ``buffer_minutes`` of breathing room on top of the ``buffer_fraction``
    applied to the total. ``deadline_min`` (a minute-within-day) clamps every
    gap so nothing is counted after an earlier deadline. Pure: no side effects.
    """
    total = 0
    for a, b in free_intervals(day_start, day_end, sleep_start, sleep_end, events):
        b = b if deadline_min is None else min(b, deadline_min)
        if b - a <= 0:
            continue
        usable = (b - a) - buffer_minutes
        if usable < min_slot_minutes:
            continue
        total += usable
    return max(0, int(total * (1.0 - buffer_fraction)))


# ---------------------------------------------------------------------------
# core solver (pure)
# ---------------------------------------------------------------------------
def solve(day_start: int, day_end: int, events: list[Event], tasks: list[Task],
          buffer_fraction: float = 0.20, buffer_minutes: int = 15,
          min_slot_minutes: int = 25, sleep_start: int = 1380,
          sleep_end: int = 420) -> PlanState:
    """Deterministically allocate tasks to free time. Pure: no side effects."""
    gaps = free_intervals(day_start, day_end, sleep_start, sleep_end, events)
    usable = [(g[0], g[1], max(0, (g[1] - g[0]) - buffer_minutes)) for g in gaps if g[1] - g[0] > 0]
    total_available = sum(g[1] - g[0] for g in usable)
    if total_available <= 0:
        return PlanState(day_start, day_end, events=events,
                         notes=["No free time today (fully blocked)."])

    capacity = int(total_available * (1.0 - buffer_fraction))
    placed = 0
    slots: list[Slot] = []

    for t in sorted(tasks, key=Task.urgency_key):
        if t.remaining_minutes <= 0 or t.status not in ("todo", "doing"):
            continue
        remaining = t.remaining_minutes
        for idx, (gs, ge, gcap) in enumerate(usable):
            if placed >= capacity:
                break
            if gcap < min_slot_minutes:
                continue
            contribution = min(remaining, gcap, capacity - placed)
            if contribution < min_slot_minutes and contribution < remaining:
                continue
            if contribution <= 0:
                continue
            start = gs
            end = start + contribution
            slots.append(Slot(t.id, t.title, start, end,
                              partial=(contribution < remaining)))
            remaining -= contribution
            placed += contribution
            # shrink this gap so the next task starts after this session
            usable[idx] = (end, ge, max(0, (ge - end) - buffer_minutes))
            if remaining <= 0:
                break
        if placed >= capacity:
            break

    notes = []
    if placed >= capacity:
        notes.append("Buffer reached — remaining tasks left for later.")
    unplaced = [t.title for t in tasks if t.status in ("todo", "doing")
                and self_needs(t) and not any(s.task_id == t.id for s in slots)]
    if unplaced:
        notes.append("Could not fit: " + ", ".join(unplaced))

    return PlanState(day_start, day_end, events=events, slots=slots, notes=notes)


def self_needs(t: Task) -> bool:
    return t.remaining_minutes > 0


# ---------------------------------------------------------------------------
# differences & explanations (pure)
# ---------------------------------------------------------------------------
def diff_old(old: list[Slot], new: list[Slot]) -> list[dict[str, Any]]:
    """Which tasks changed position, or were added, after a re-solve."""
    old_by = {s.task_id: s for s in old}
    new_by = {s.task_id: s for s in new}
    moved: list[dict[str, Any]] = []
    for tid, ns in new_by.items():
        o = old_by.get(tid)
        if o is None:
            moved.append({"task_id": tid, "title": ns.title,
                          "old": None, "new": (ns.start_min, ns.end_min),
                          "reason": "newly scheduled"})
        elif (o.start_min, o.end_min) != (ns.start_min, ns.end_min):
            moved.append({"task_id": tid, "title": ns.title,
                          "old": [o.start_min, o.end_min],
                          "new": [ns.start_min, ns.end_min],
                          "reason": _why(o, ns)})
    removed = [{"task_id": tid, "title": s.title, "old": [s.start_min, s.end_min],
                "new": None, "reason": "no longer scheduled"}
               for tid, s in old_by.items() if tid not in new_by]
    return moved + removed


def _why(old: Slot, new: Slot) -> str:
    if new.start_min > old.start_min:
        return "free time was needed earlier (or a hard event blocked the old time)"
    if new.start_min < old.start_min:
        return "moved earlier — more free time became available"
    return "time window changed"


def overlap_with_event(slot: Slot, events: list[Event]) -> Event | None:
    for e in events:
        if slot.start_min < e.end_min and slot.end_min > e.start_min:
            return e
    return None
