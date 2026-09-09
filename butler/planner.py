"""Phase 2: the planner — the layer between Butler and the pure solver.

The Planner is the ONLY place that talks to the database (task/event/plan
tables) and the calendar sources. It:

  * syncs external hard commitments (local .ics + Google Calendar) into the
    ``events`` table (DB is the source of truth);
  * borrows the working window from config (sleep_end..sleep_start);
  * builds the day's inputs (active tasks + that day's events) and hands them
    to ``schedule.solve`` (pure);
  * persists each solved day as a plan, keeping an undo history;
  * answers "what should I do now?" and applies status transitions.

DeepSeek (``agent.py``) may reason about *why* something moved or propose a
priority, but the plan's actual positions always come from the pure solver.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

from . import schedule as sch
from .agent import Agent

log = logging.getLogger("butler.planner")


def _hm(minutes: int) -> str:
    minutes = max(0, min(minutes, 1439))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class Planner:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.day_start = int(self.cfg.sleep_end)     # e.g. 07:00
        self.day_end = int(self.cfg.sleep_start)     # e.g. 23:00
        self.agent = Agent(self.container)

    # ------------------------------------------------------------------ events
    def sync_events(self, source: str = "") -> dict[str, Any]:
        """Refresh the ``events`` table from the enabled sources."""
        fetched: list[tuple[Any, Any, Any, str]] = []  # (title, start_ts, end_ts, source)
        if self.cfg.local_calendar_file and os.path.exists(self.cfg.local_calendar_file):
            try:
                fetched += self._parse_ics(self.cfg.local_calendar_file, "local")
            except Exception as exc:  # noqa: BLE001
                log.warning("ics parse failed: %s", exc)
        if self.cfg.google_calendar_enabled and self.cfg.google_calendar_credentials \
                and os.path.exists(self.cfg.google_calendar_credentials):
            try:
                from .gcal import sync_google_events
                fetched += [(e["title"], e["start"], e["end"], "google")
                            for e in sync_google_events(self.cfg)]
            except Exception as exc:  # noqa: BLE001
                log.warning("google calendar sync failed: %s", exc)

        if source and source != "local":
            self.db.clear_events("google")
        elif source == "local":
            self.db.clear_events("local")
        else:
            self.db.clear_events()

        for title, start_ts, end_ts, src in fetched:
            self.db.add_event(str(title), int(start_ts), int(end_ts), source=src)
        return {"ok": True, "count": len(fetched)}

    @staticmethod
    def _parse_ics(path: str, source: str) -> list[tuple[str, int, int, str]]:
        out: list[tuple[str, int, int, str]] = []
        events = []
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            block: list[str] = []
            for line in fh:
                line = line.rstrip("\n")
                if line == "BEGIN:VEVENT":
                    block = []
                elif line == "END:VEVENT":
                    events.append(block)
                elif block is not None:
                    block.append(line)
        for blk in events:
            title, start, end = "", None, None
            for line in blk:
                key, _, raw = line.partition(":")
                key = key.strip().upper()
                if key == "SUMMARY":
                    title = raw.strip()
                elif key == "DTSTART" and start is None:
                    start = Planner._ics_ts(raw.strip())
                elif key == "DTEND" and end is None:
                    end = Planner._ics_ts(raw.strip())
            if title and start is not None:
                end = end or start + 3600
                out.append((title, start, end, source))
        return out

    @staticmethod
    def _ics_ts(raw: str) -> int | None:
        if not raw:
            return None
        if "T" in raw and len(raw) >= 15:
            s = raw.replace("Z", "+0000").replace("-", "").replace(":", "")
            if "+" in s[8:]:
                s = s[:8] + s[9:]
            try:
                return int(datetime.fromisoformat(s[:8] + "T" + s[8:]).timestamp())
            except Exception:  # noqa: BLE001
                pass
        try:
            return int(datetime.strptime(raw, "%Y%m%d").timestamp())
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ tasks
    def _active_tasks(self, day_ts: int) -> list[sch.Task]:
        rows = self.db.tasks("active")  # todo + doing
        day = datetime.fromtimestamp(day_ts)
        tasks = []
        for r in rows:
            deadline_min = None
            if r["deadline"]:
                dl = datetime.fromtimestamp(r["deadline"])
                if dl.date() < day.date():
                    deadline_min = 0              # overdue -> most urgent
                elif dl.date() == day.date():
                    deadline_min = dl.hour * 60 + dl.minute
            tasks.append(sch.Task(
                id=int(r["id"]), title=str(r["title"]),
                remaining_minutes=max(1, int(r["est_minutes"] or 60)),
                deadline=deadline_min, priority=int(r["priority"] or 3),
                status=str(r["status"] or "todo"), tags=str(r["tags"] or ""),
            ))
        return tasks

    def _day_events(self, day_ts: int) -> list[sch.Event]:
        day = datetime.fromtimestamp(day_ts)
        start_ts = int(datetime(day.year, day.month, day.day, 0, 0).timestamp())
        end_ts = start_ts + 86400
        rows = self.db.events_between(start_ts, end_ts)
        out = []
        for r in rows:
            s = datetime.fromtimestamp(int(r["start_ts"]))
            e = datetime.fromtimestamp(int(r["end_ts"]))
            start_min = s.hour * 60 + s.minute if s.date() == day.date() else 0
            end_min = e.hour * 60 + e.minute if e.date() == day.date() else 1440
            if end_min <= start_min:
                continue
            out.append(sch.Event(id=int(r["id"]), title=str(r["title"]),
                                 start_min=start_min, end_min=end_min,
                                 source=str(r["source"] or "local")))
        return out

    # ------------------------------------------------------------------ solve
    def _solve(self, day_ts: int) -> sch.PlanState:
        tasks = self._active_tasks(day_ts)
        events = self._day_events(day_ts)
        state = sch.solve(
            self.day_start, self.day_end, events, tasks,
            buffer_fraction=self.cfg.buffer_fraction,
            buffer_minutes=self.cfg.buffer_minutes,
            min_slot_minutes=self.cfg.min_slot_minutes,
            sleep_start=int(self.cfg.sleep_start),
            sleep_end=int(self.cfg.sleep_end),
        )
        state.snapshot = {"tasks": [t.to_dict() for t in tasks]}
        return state

    # ------------------------------------------------------------------ sync
    def _maybe_sync(self) -> None:
        """Refresh cheap local ics always; Google only if stale (>30 min)."""
        if self.cfg.local_calendar_file and os.path.exists(self.cfg.local_calendar_file):
            self.sync_events("local")
        if self.cfg.google_calendar_enabled and self.cfg.google_calendar_credentials \
                and os.path.exists(self.cfg.google_calendar_credentials):
            stamp = os.path.join(self.cfg.state_dir, "gcal_last_sync")
            fresh = os.path.exists(stamp) and \
                (datetime.now().timestamp() - os.path.getmtime(stamp) < 1800)
            if not fresh:
                self.sync_events()
                try:
                    open(stamp, "w").write(str(datetime.now().timestamp()))
                except OSError:
                    pass

    def plan_day(self, day_ts: int | None = None) -> dict[str, Any]:
        day_ts = day_ts or self._today()
        self._maybe_sync()
        prev = self.db.latest_plan()
        state = self._solve(day_ts)
        self.db.save_plan(self.day_start, self.day_end, "active", state.to_json())
        if prev:
            self.db.update_plan_state(int(prev["id"]), "history")
        return self._summary(state)

    def reschedule(self, day_ts: int | None = None) -> dict[str, Any]:
        return self.plan_day(day_ts)

    def what_now(self, day_ts: int | None = None) -> dict[str, Any]:
        day_ts = day_ts or self._today()
        state = self._solve(day_ts)
        now = datetime.fromtimestamp(day_ts)  # reference
        now_min = datetime.now().hour * 60 + datetime.now().minute
        slots = sorted((s for s in state.slots if s.end_min > now_min),
                       key=lambda s: s.start_min)
        if not slots:
            return {"ok": True, "now": None,
                    "answer": "Nothing left to do right now. Enjoy the buffer."}
        s = slots[0]
        await_ = max(0, s.start_min - now_min)
        return {"ok": True, "now": {"task_id": s.task_id, "title": s.title,
                                    "start": _hm(s.start_min), "end": _hm(s.end_min)},
                "answer": f"Now: {s.title} ({_hm(s.start_min)}-{_hm(s.end_min)})."}

    # ------------------------------------------------------------------ state
    def add_task(self, title: str, detail: str = "", est_minutes: int = 60,
                 deadline: int = 0, priority: int = 3, tags: str = "") -> int:
        return self.db.add_task(title, detail=detail, deadline=deadline,
                                priority=priority, est_minutes=est_minutes, tags=tags)

    def start(self, task_id: int) -> dict[str, Any]:
        t = self.db.task_by_id(task_id)
        if not t:
            return {"ok": False, "error": "unknown task"}
        self.db.update_task(task_id, status="doing")
        return {"ok": True, "task_id": task_id, "title": t["title"]}

    def done(self, task_id: int) -> dict[str, Any]:
        t = self.db.task_by_id(task_id)
        if not t:
            return {"ok": False, "error": "unknown task"}
        self.db.set_task_status(task_id, "done")
        self._replan_if_active(task_id)
        return {"ok": True, "task_id": task_id, "title": t["title"]}

    def skip(self, task_id: int) -> dict[str, Any]:
        t = self.db.task_by_id(task_id)
        if not t:
            return {"ok": False, "error": "unknown task"}
        self.db.set_task_status(task_id, "skipped")
        self._replan_if_active(task_id)
        return {"ok": True, "task_id": task_id, "title": t["title"]}

    def came_up(self, title: str, est_minutes: int = 60, deadline: int = 0,
                priority: int = 4) -> dict[str, Any]:
        """An unplanned urgent thing appeared -> add it and adapt the schedule."""
        if self.agent.ready():
            prop = self.agent.interpret_urgent(title,
                                               default_priority=priority,
                                               default_est=est_minutes)
            title = prop["title"]
            est_minutes = prop["est_minutes"]
            priority = prop["priority"]
        tid = self.add_task(title, est_minutes=est_minutes, deadline=deadline,
                            priority=priority)
        prev = self.db.latest_plan()
        moved = self._diff_moves(prev) if prev else []
        self.reschedule()
        return {"ok": True, "task_id": tid, "title": title, "moved": moved}

    # ------------------------------------------------------------------ explain
    def why(self, day_ts: int | None = None) -> dict[str, Any]:
        history = self.db.history_plans(1)
        if not history:
            return {"ok": True, "reason": "Schedule hasn't changed recently.",
                    "moved": [], "cause": None}
        prev = sch.PlanState.from_json(history[0]["json"])
        current = self.db.latest_plan()
        cur = sch.PlanState.from_json(current["json"]) if current else prev
        moved = sch.diff_old(prev.slots, cur.slots)
        cause = self._cause_of_move(prev, cur, moved)
        reason = self._explain(moved, cause)
        if self.agent.ready():
            reason = self.agent.why_sentence(reason, cause, moved)
        return {"ok": True, "moved": moved, "cause": cause, "reason": reason}

    def _cause_of_move(self, prev: sch.PlanState, cur: sch.PlanState,
                       moved: list[dict[str, Any]]) -> str | None:
        """A hard event is the cause iff it newly appeared and covers the old
        slot of one of the moved blocks (the solver itself never overlaps)."""
        prev_events = {(e.title, e.start_min, e.end_min) for e in prev.events}
        new_events = [e for e in cur.events
                      if (e.title, e.start_min, e.end_min) not in prev_events]
        if not new_events:
            return None
        for m in moved:
            if m["old"] is None:
                continue
            o0, o1 = m["old"]
            for e in new_events:
                if o0 < e.end_min and o1 > e.start_min:
                    return e.title
        return None

    def _explain(self, moved: list[dict[str, Any]], cause: str | None) -> str:
        if not moved:
            return "No changes since the last plan."
        if cause:
            return (f"{cause} is a hard commitment, so Butler moved the soft "
                    f"blocks around it rather than overlapping it.")
        names = [m["title"] for m in moved if m["new"]]
        if names:
            return "Tasks adjusted to keep buffer and respect hard events: " + ", ".join(names)
        return "Tasks were re-prioritised to fit the available time."

    # ------------------------------------------------------------------ undo
    def undo(self) -> dict[str, Any]:
        history = self.db.history_plans(1)
        current = self.db.latest_plan()
        if not history:
            return {"ok": False, "error": "Nothing to undo."}
        h = history[0]
        if current:
            self.db.update_plan_state(int(current["id"]), "history")
        self.db.update_plan_state(int(h["id"]), "active")
        restored = sch.PlanState.from_json(h["json"])
        return {"ok": True, "restored": self._summary(restored)}

    # ------------------------------------------------------------------ helpers
    def _diff_moves(self, prev_row: Any) -> list[dict[str, Any]]:
        prev = sch.PlanState.from_json(prev_row["json"])
        current = self.db.latest_plan()
        cur = sch.PlanState.from_json(current["json"]) if current else prev
        return sch.diff_old(prev.slots, cur.slots)

    def _replan_if_active(self, task_id: int) -> None:
        # completed/skipped tasks are excluded by _active_tasks; refresh plan
        try:
            self.reschedule()
        except Exception as exc:  # noqa: BLE001
            log.warning("replan after task %s failed: %s", task_id, exc)

    def _summary(self, state: sch.PlanState) -> dict[str, Any]:
        return {
            "ok": True,
            "day_start": state.day_start, "day_end": state.day_end,
            "events": [e.to_dict() for e in state.events],
            "slots": [s.to_dict() for s in state.slots],
            "notes": state.notes,
            "text": self._format(state),
        }

    def _format(self, state: sch.PlanState) -> str:
        lines = [f"Day {_hm(state.day_start)}–{_hm(state.day_end)}"]
        for e in state.events:
            lines.append(f"  ⛔ {_hm(e.start_min)}–{_hm(e.end_min)}  {e.title}  "
                         f"[{e.source}]")
        for s in state.slots:
            mark = " ⇢" if s.partial else ""
            lines.append(f"  ✅ {_hm(s.start_min)}–{_hm(s.end_min)}  {s.title}{mark}")
        free = sum(max(0, (g[1] - g[0])) for g in sch.free_intervals(
            state.day_start, state.day_end, int(self.cfg.sleep_start),
            int(self.cfg.sleep_end), state.events))
        used = sum(s.end_min - s.start_min for s in state.slots)
        lines.append(f"  ▦ used {used}m of ~{free}m (buffer kept)")
        for n in state.notes:
            lines.append(f"  ! {n}")
        return "\n".join(lines)

    @staticmethod
    def _today() -> int:
        return int(datetime.now().timestamp())
