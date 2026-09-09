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
from datetime import datetime, timedelta, timezone
from typing import Any

from . import schedule as sch
from . import affinity
from .agent import Agent
from .gcal import GCalError, GoogleCalendar

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
        self.timeline = getattr(container, "timeline", None)

    def _tl(self) -> Any:
        return self.timeline

    def _record_task_event(self, task_id: int, title: str, kind: str) -> None:
        tl = self._tl()
        if tl is None or not hasattr(tl, kind):
            return
        try:
            getattr(tl, kind)(task_id, title or "")
        except Exception:  # pragma: no cover — timeline is an audit trail
            log.debug("timeline %s failed for task %s", kind, task_id, exc_info=True)

    # ------------------------------------------------------------------ events
    def sync_events(self, source: str = "") -> dict[str, Any]:
        """Refresh the ``events`` table from the enabled sources.

        Each source is independent and failure-safe: a Google Calendar outage or
        an auth problem leaves every existing event (local *and* google) intact
        -- only a *successful* fetch is merged into the DB (never a blind
        clear-and-reload). See ``sync_google`` for the merge semantics.
        """
        if source == "local":
            return self._sync_local()
        if source == "google":
            return self.sync_google()
        local = self._sync_local()
        out: dict[str, Any] = {"ok": True, "count": local.get("count", 0)}
        try:
            g = self.sync_google()
            out["count"] = local.get("count", 0) + g.get("count", 0)
            out["google"] = g
        except GCalError as exc:  # noqa: BLE001
            log.warning("google calendar sync failed (kept existing events): %s", exc)
            out["google_error"] = str(exc)
            out["google"] = {"ok": False, "count": 0}
        return out

    def _sync_local(self) -> dict[str, Any]:
        """Parse the local .ics and reload *only local* events.

        Fetch/parse first: if the file is missing or unreadable the existing
        local events are left alone (never cleared into a half-imported state).
        """
        path = self.cfg.local_calendar_file
        if not path or not os.path.exists(path):
            return {"ok": True, "count": 0}
        try:
            fetched = self._parse_ics(path, "local")
        except Exception as exc:  # noqa: BLE001  (keep existing local events)
            log.warning("ics parse failed; keeping existing local events: %s", exc)
            return {"ok": True, "count": 0, "error": str(exc)}
        self.db.clear_events("local")
        for title, start_ts, end_ts, src in fetched:
            self.db.add_event(str(title), int(start_ts), int(end_ts), source=src)
        return {"ok": True, "count": len(fetched)}

    def sync_google(self, gcal: GoogleCalendar | None = None,
                    events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Fetch Google Calendar events and merge them into the ``events`` table.

        ``events`` (already-normalised records) may be injected directly so the
        pure DB merge can be tested without network. Otherwise a ``GoogleCalendar``
        is built (optionally injected) and ``list_events`` is called -- any
        ``GCalError`` propagates and the DB is left untouched.
        """
        if events is None:
            gc = gcal or GoogleCalendar(self.cfg)
            events = gc.list_events()
        return self._merge_google(events)

    def _merge_google(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Deterministic merge of google events keyed by ``external_id``.

        * new instances   -> insert
        * existing, moved -> update in place (never a duplicate)
        * existing, same  -> no-op
        * vanished within the sync window -> delete (cancellation/removal)
        * status=cancelled -> delete (a single cancelled occurrence of a series)

        This is idempotent: a repeated sync with the same feed yields identical
        DB state (``added=updated=removed=0``).
        """
        now = datetime.now(timezone.utc)
        ws = int((now - timedelta(days=7)).timestamp())
        we = int((now + timedelta(days=30)).timestamp())
        seen: set[str] = set()
        added = updated = removed = unchanged = skipped = 0
        for e in events:
            ext = e.get("external_id", "")
            if not ext:
                skipped += 1
                continue
            if e.get("status") == "cancelled":
                row = self.db.event_by_external(ext, "google")
                if row:
                    self.db.delete_event(int(row["id"]))
                    removed += 1
                else:
                    skipped += 1
                continue
            seen.add(ext)
            row = self.db.event_by_external(ext, "google")
            if row is None:
                self.db.add_event(str(e["title"]), int(e["start_ts"]),
                                  int(e["end_ts"]), source="google",
                                  external_id=ext, all_day=int(e.get("all_day", 0)),
                                  location=e.get("location", ""))
                added += 1
            else:
                changed = (str(row["title"]) != str(e["title"])
                           or int(row["start_ts"]) != int(e["start_ts"])
                           or int(row["end_ts"]) != int(e["end_ts"])
                           or int(row["all_day"] or 0) != int(e.get("all_day", 0)))
                if changed:
                    self.db.update_event(int(row["id"]), str(e["title"]),
                                         int(e["start_ts"]), int(e["end_ts"]),
                                         all_day=int(e.get("all_day", 0)),
                                         location=e.get("location", ""))
                    updated += 1
                else:
                    unchanged += 1
        # prune instances that disappeared within the authoritative window
        for row in self.db.google_events_in_window(ws, we):
            if row["external_id"] not in seen:
                self.db.delete_event(int(row["id"]))
                removed += 1
        self._stamp_gcal()
        return {"ok": True, "count": added + updated + unchanged,
                "added": added, "updated": updated, "removed": removed,
                "unchanged": unchanged}

    def _stamp_gcal(self) -> None:
        try:
            with open(os.path.join(self.cfg.state_dir, "gcal_last_sync"), "w") as fh:
                fh.write(str(datetime.now().timestamp()))
        except OSError:
            pass

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
    def _solve(self, day_ts: int, affinities: dict[int, int] | None = None) -> sch.PlanState:
        tasks = self._active_tasks(day_ts)
        if affinities:
            for t in tasks:
                t.affinity = int(affinities.get(t.id, 0))
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
        """Refresh cheap local ics always; Google only if stale (>30 min).

        A Google Calendar outage/auth problem is swallowed so a plan can always
        be produced from whatever events are already in the DB (Graceful
        Degradation). The recency stamp is only written on a *successful* sync,
        so the next attempt retries when Google comes back.
        """
        if self.cfg.local_calendar_file and os.path.exists(self.cfg.local_calendar_file):
            self._sync_local()
        if self.cfg.google_calendar_enabled and self.cfg.google_calendar_credentials \
                and os.path.exists(self.cfg.google_calendar_credentials):
            stamp = os.path.join(self.cfg.state_dir, "gcal_last_sync")
            fresh = os.path.exists(stamp) and \
                (datetime.now().timestamp() - os.path.getmtime(stamp) < 1800)
            if not fresh:
                try:
                    self.sync_google()
                except GCalError as exc:  # noqa: BLE001  (keep existing events)
                    log.warning("google calendar sync failed (kept existing events): %s", exc)

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
        out = self.plan_day(day_ts)
        tl = self._tl()
        if tl is not None and hasattr(tl, "record_schedule_change"):
            try:
                tl.record_schedule_change(note="reschedule")
            except Exception:  # pragma: no cover — timeline is an audit trail
                pass
        return out

    def what_now(self, message: str = "", day_ts: int | None = None,
                 now_min: int | None = None) -> dict[str, Any]:
        """Recommend the next thing to do, using soft context when available.

        Context (presence zone, the user's explicit words, energy) is applied
        only as a *soft* affinity tie-breaker inside the pure solver; it never
        overrides a hard constraint (a lecture, a deadline, sleep, the buffer),
        and never silently changes the day's committed plan (``plan_day`` and
        ``reschedule`` remain context-neutral). When HA is unknown/unavailable,
        or when no preference is expressed, the recommendation is exactly the
        deterministic baseline.

        ``now_min`` lets a caller pin the current minute-of-day (default: real
        clock), which is what makes the acceptance tests reproducible.
        """
        day_ts = day_ts or self._today()
        r = self._recommend(message, day_ts, now_min)
        return {"ok": True, "now": r["now"], "answer": r["answer"],
                "reason": r["reason"], "factors": r["factors"],
                "context_influenced": r["context_influenced"],
                "user_override": r["user_override"],
                "ha_influence": r["ha_influence"],
                "candidate": r["candidate"], "decision_log": r["decision_log"]}

    def explain_now(self, message: str = "", day_ts: int | None = None,
                    now_min: int | None = None) -> dict[str, Any]:
        """Why is this task the current recommendation? (``why this?``)."""
        day_ts = day_ts or self._today()
        r = self._recommend(message, day_ts, now_min)
        return {"ok": True, "candidate": r["candidate"], "reason": r["reason"],
                "factors": r["factors"], "context_influenced": r["context_influenced"],
                "user_override": r["user_override"], "ha_influence": r["ha_influence"],
                "decision_log": r["decision_log"]}

    # ------------------------------------------------------- recommendation
    def _recommend(self, message: str, day_ts: int,
                   now_min: int | None = None) -> dict[str, Any]:
        pres = self._presence_safe()
        zone_raw = (pres.get("zone") or "").strip()
        zone = zone_raw.lower()
        preferences = affinity.preference_weights(message)
        tired = affinity.is_tired(message)
        tasks = self._active_tasks(day_ts)
        aff_map = {t.id: affinity.task_affinity(t.title, t.tags, zone,
                                                preferences, tired)
                   for t in tasks}
        state = self._solve(day_ts, aff_map)
        tasks_by_id = {t["id"]: t for t in state.snapshot.get("tasks", [])}
        if now_min is None:
            now_min = datetime.now().hour * 60 + datetime.now().minute
        slots = sorted((s for s in state.slots if s.end_min > now_min),
                       key=lambda s: s.start_min)
        chosen: sch.Slot | None = None
        for s in slots:
            t = tasks_by_id.get(s.task_id)
            if t and t.get("deadline") is not None and s.end_min > t["deadline"]:
                continue  # never recommend a task after its deadline
            chosen = s
            break
        base_log = {"presence_known": bool(pres.get("known")), "zone": zone_raw,
                    "tired": tired, "preference_weights": dict(preferences),
                    "affinity": {str(k): v for k, v in aff_map.items()}}
        if chosen is None:
            return {"ok": True, "now": None,
                    "answer": "Nothing left to do right now. Enjoy the buffer.",
                    "reason": "", "factors": {}, "context_influenced": False,
                    "user_override": False, "ha_influence": False,
                    "candidate": None, "decision_log": base_log}
        task = tasks_by_id[chosen.task_id]
        cats = list(affinity.classify(task.get("title", ""), task.get("tags", "")))
        zw = affinity.zone_weights_for(zone)
        loc_fit = next((c for c in cats if zw.get(c, 0) > 0), None)
        pref_fit = next((c for c in cats if preferences.get(c, 0) != 0), None)
        factors = self._factors(task, chosen, pres, zw, cats, loc_fit, pref_fit,
                                tired, preferences, tasks_by_id, now_min, day_ts)
        reason = self._explain_reason(task, chosen, zone_raw, loc_fit, pref_fit,
                                      tired, factors)
        candidate = {"task_id": chosen.task_id, "title": chosen.title,
                     "start": _hm(chosen.start_min), "end": _hm(chosen.end_min)}
        answer = f"Now: {chosen.title} ({_hm(chosen.start_min)}-{_hm(chosen.end_min)})."
        if loc_fit or pref_fit or tired:
            answer += " " + reason
        return {"ok": True, "now": candidate, "answer": answer, "reason": reason,
                "factors": factors,
                "context_influenced": bool(loc_fit or pref_fit),
                "user_override": bool(pref_fit),
                "ha_influence": bool(pres.get("known") and loc_fit),
                "candidate": candidate,
                "decision_log": {**base_log,
                                 "candidate": candidate, "factors": factors,
                                 "context_influenced": bool(loc_fit or pref_fit),
                                 "user_override": bool(pref_fit),
                                 "ha_influence": bool(pres.get("known") and loc_fit)}}

    def _factors(self, task: dict[str, Any], sample: sch.Slot, pres: dict[str, Any],
                 zw: dict[str, int], cats: list[str], loc_fit: str | None,
                 pref_fit: str | None, tired: bool,
                 preferences: dict[str, int], tasks_by_id: dict[int, dict[str, Any]],
                 now_min: int, day_ts: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        deadline = task.get("deadline")
        out["deadline"] = None
        if deadline is not None:
            if deadline == 0:
                out["deadline"] = "overdue (due earlier today)"
            else:
                out["deadline"] = f"due by {_hm(deadline)}"
        out["next_commitment"] = self._next_commitment(now_min, day_ts)
        if loc_fit:
            zone_disp = (pres.get("zone") or "").strip()
            out["location"] = f"{zone_disp} favours {loc_fit}"
        else:
            out["location"] = None
        if pref_fit:
            w = preferences.get(pref_fit, 0)
            out["preference"] = f"you want to {pref_fit}" if w > 0 else \
                                f"you'd rather not {pref_fit}"
        else:
            out["preference"] = None
        if tired:
            out["energy"] = "low — heavier tasks deprioritised"
        else:
            out["energy"] = "ok"
        out["category"] = cats
        out["affinity"] = int(task.get("affinity", 0))
        return out

    def _next_commitment(self, now_min: int, day_ts: int) -> str | None:
        ends = [e.end_min for e in self._day_events(day_ts) if e.end_min > now_min]
        nxt = min(ends) if ends else None
        return _hm(nxt) if nxt is not None else None

    def _explain_reason(self, task: dict[str, Any], sample: sch.Slot,
                        zone_raw: str, loc_fit: str | None, pref_fit: str | None,
                        tired: bool, factors: dict[str, Any]) -> str:
        bits: list[str] = []
        if loc_fit:
            bits.append(f"you're at {zone_raw}, which suits {loc_fit}")
        if pref_fit:
            bits.append(f"you wanted to do that")
        if tired:
            bits.append("you're a bit tired, so lighter tasks come first")
        dl = factors.get("deadline")
        if dl:
            bits.append(f"it's {dl}")
        nc = factors.get("next_commitment")
        if nc and not loc_fit and not pref_fit:
            bits.append(f"it fits before the next commitment at {nc}")
        if not bits:
            return ("This is the next clearly-available task in the plan.")
        return " — " + ", ".join(bits) + "."

    def _presence_safe(self) -> dict[str, Any]:
        ha = getattr(self.container, "ha", None)
        if ha is None or not hasattr(ha, "presence"):
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant"}
        try:
            return ha.presence()
        except Exception:  # noqa: BLE001 — presence must never break what_now
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant"}

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
        self._record_task_event(task_id, t["title"], "record_task_started")
        return {"ok": True, "task_id": task_id, "title": t["title"]}

    def done(self, task_id: int) -> dict[str, Any]:
        t = self.db.task_by_id(task_id)
        if not t:
            return {"ok": False, "error": "unknown task"}
        self.db.set_task_status(task_id, "done")
        self._record_task_event(task_id, t["title"], "record_task_completed")
        self._replan_if_active(task_id)
        return {"ok": True, "task_id": task_id, "title": t["title"]}

    def skip(self, task_id: int) -> dict[str, Any]:
        t = self.db.task_by_id(task_id)
        if not t:
            return {"ok": False, "error": "unknown task"}
        self.db.set_task_status(task_id, "skipped")
        self._record_task_event(task_id, t["title"], "record_task_completed")
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

    # ------------------------------------------------- assignment placement
    def capacity_before(self, est_minutes: int, deadline: int,
                        day_ts: int | None = None) -> dict[str, Any]:
        """Deterministic deadline-aware capacity envelope.

        Sums the usable free minutes across every waking day from ``day_ts`` up
        to the assignment deadline (clamping each day at the deadline). Purely
        additive and deterministic; the solver/LLM never enters. Returns whether
        the job fits and the shortfall if it does not (used to *report* a
        conflict instead of silently overbooking).
        """
        day_ts = day_ts or self._today()
        now = datetime.now().timestamp()
        deadline = int(deadline or 0)
        est_minutes = max(0, int(est_minutes or 0))
        if deadline and deadline <= now:
            return {"needed": est_minutes, "available": 0, "days": 0,
                    "deficit": est_minutes, "conflict": est_minutes > 0,
                    "headroom": est_minutes <= 0}
        horizon = min(deadline, int(now) + 30 * 86400) if deadline else int(now) + 30 * 86400
        start = self._day_start_ts(day_ts)
        total = 0
        days = 0
        day = start
        while day < horizon:
            events = self._day_events(day)
            deadline_min = None
            if deadline:
                dl = datetime.fromtimestamp(deadline)
                d = datetime.fromtimestamp(day)
                if dl.date() == d.date():
                    deadline_min = dl.hour * 60 + dl.minute
            total += sch.day_capacity(
                self.day_start, self.day_end, events, self.cfg.buffer_fraction,
                self.cfg.buffer_minutes, self.cfg.min_slot_minutes,
                int(self.cfg.sleep_start), int(self.cfg.sleep_end), deadline_min)
            days += 1
            day += 86400
        if total < 0:
            total = 0
        deficit = max(0, est_minutes - total)
        return {"needed": est_minutes, "available": total, "days": days,
                "deficit": deficit, "conflict": deficit > 0,
                "headroom": total >= est_minutes}

    def schedule_assignment_blocks(self, items: list[dict[str, Any]],
                                   day_ts: int | None = None) -> dict[str, Any]:
        """Deterministically place a set of assignment tasks and report them.

        ``items`` is a list of ``{task_id, est_minutes, deadline}``. For each we
        run a pure deadline-aware capacity check, then re-solve today's plan
        exactly once (the solver chooses every start/end — the LLM has no say),
        and return, per task, its placed slots together with the capacity result.
        Idempotent: re-running re-solves from scratch, so blocks never accumulate.
        """
        out: dict[str, Any] = {}
        if not items:
            return out
        day_ts = day_ts or self._today()
        caps = {it["task_id"]: self.capacity_before(it.get("est_minutes", 0),
                                                    it.get("deadline", 0), day_ts)
                for it in items}
        self.reschedule(day_ts)
        plan = self.db.latest_plan()
        slots: list[sch.Slot] = []
        if plan:
            try:
                state = sch.PlanState.from_json(plan["json"])
                slots = state.slots
            except Exception as exc:  # noqa: BLE001
                log.warning("parse active plan failed: %s", exc)
        for it in items:
            tid = int(it["task_id"])
            mine = [s.to_dict() for s in slots if s.task_id == tid]
            cap = caps.get(tid, {})
            out[tid] = {"task_id": tid, "slots": mine,
                        "placed_minutes": sum(s["end_min"] - s["start_min"] for s in mine),
                        **cap}
        return out

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
    def _day_start_ts(ts: int) -> int:
        d = datetime.fromtimestamp(ts)
        return int(datetime(d.year, d.month, d.day, 0, 0).timestamp())

    @staticmethod
    def _today() -> int:
        return int(datetime.now().timestamp())
