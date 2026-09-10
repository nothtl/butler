"""Phase 5.1: Daily Executive Loop.

The executive layer is a thin, pure, *aggregative* seam on top of the existing
deterministic subsystems (context engine, planner, routine/food/course domains).
It never mutates a schedule, a calendar, a task, or a meal plan. It only:

  * reads one coherent snapshot of the day (context engine),
  * synthesises a **briefing** (what the day looks like),
  * synthesises a **review** (what actually happened vs. what was planned),
  * surfaces the planner's *executive* recommendation ("what should I do now?"),
  * tracks **idempotent delivery markers** so a daily briefing/review is sent
    at most once per day (across restarts and re-runs).

Every check degrades gracefully: if the context engine, the planner, the
course/food domains or the timeline are unavailable the piece simply drops out
and the remaining summary is still produced (never a hard failure).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import db as dbmod
from . import schedule as sch

log = logging.getLogger("butler.executive")

# Delivery-marker kind names (persisted in the ``exec_state`` table).
BRIEFING_KIND = "briefing"
REVIEW_KIND = "review"


def _hm(minutes: int | str) -> str:
    if isinstance(minutes, str):
        return minutes
    minutes = max(0, min(minutes, 1439))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class Executive:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.context = getattr(container, "context", None)
        self.planner = getattr(container, "planner", None)
        self.food = getattr(container, "food", None)
        self.chef = getattr(container, "chef", None)
        self.foodplan = getattr(container, "foodplan", None)

    # --------------------------------------------------------- day helpers
    @staticmethod
    def _local_midnight(ts: int, cfg: Any) -> int:
        if cfg is not None and hasattr(cfg, "local_midnight"):
            try:
                return cfg.local_midnight(ts)
            except Exception:  # pragma: no cover — tz invalid => fall back
                pass
        d = datetime.fromtimestamp(ts)
        return int(datetime(d.year, d.month, d.day).timestamp())

    def _day_key(self, day_ts: int) -> str:
        return datetime.fromtimestamp(self._local_midnight(day_ts, self.cfg)).strftime(
            "%Y-%m-%d")

    @staticmethod
    def _now_local(cfg: Any) -> datetime:
        if cfg is not None and hasattr(cfg, "now_local"):
            try:
                return cfg.now_local()
            except Exception:  # pragma: no cover
                pass
        return datetime.now()

    # ------------------------------------------------- delivery (idempotent)
    def was_delivered(self, kind: str, day_ts: int) -> bool:
        key = f"exec:delivered:{kind}:{self._day_key(day_ts)}"
        return bool(self.db.get_meta(key, ""))

    def mark_delivered(self, kind: str, day_ts: int) -> None:
        key = f"exec:delivered:{kind}:{self._day_key(day_ts)}"
        self.db.set_meta(key, str(day_ts))

    def is_due(self, kind: str, day_ts: int) -> bool:
        """Whether the daily piece should be (re)generated for ``day_ts``.

        Due when the executive is enabled, the cadence is on (non-zero) and the
        marker has not already been written for this local day. This is what
        makes the scheduler job idempotent across restarts.
        """
        if not getattr(self.cfg, "executive_enabled", True):
            return False
        if kind == BRIEFING_KIND:
            spec = getattr(self.cfg, "briefing_schedule", "daily")
        else:
            spec = getattr(self.cfg, "review_schedule", "daily")
        if not spec or spec == "off" or spec == "never":
            return False
        return not self.was_delivered(kind, day_ts)

    # ------------------------------------------------------------- briefing
    def briefing(self, day_ts: int | None = None) -> dict[str, Any]:
        """Synthesise the day's briefing (deterministic, graceful)."""
        day_ts = day_ts or int(self._now_local(self.cfg).timestamp())
        snap = self._snapshot()
        now_min = self._now_local(self.cfg).hour * 60 + self._now_local(self.cfg).minute
        data = self._assemble_briefing(day_ts, snap, now_min)

        lines = [str(self.cfg.briefing_banner or "📋 Daily briefing")]
        day = datetime.fromtimestamp(day_ts).strftime("%A %d %b")
        lines.append(f"Today is {day}. {snap.get('presence_note', '')}".rstrip())
        free = data["free_minutes_today"]
        lines.append(f"Free time: ~{free} min (now {_hm(now_min)})")

        if data["overdue"]:
            lines.append("")
            lines.append(f"⚠ Overdue: {', '.join(data['overdue'])}")
        if data["important"]:
            lines.append("")
            lines.append("🔥 Priority: " + "; ".join(
                f"{i} ({_hm(i['deadline_min'])})" if i.get("deadline_min") else str(i["title"])
                for i in data["important"]))
        if data["hard_events"]:
            lines.append("")
            lines.append("📅 Hard: " + ", ".join(
                e.get("title", "event") for e in data["hard_events"]))
        if data["scheduled_blocks"]:
            lines.append("")
            lines.append("🗓 Scheduled: " + ", ".join(
                f"{b['title']} {_hm(b['start'])}–{_hm(b['end'])}"
                for b in data["scheduled_blocks"]))
        if data["stalled"]:
            lines.append("")
            lines.append(f"🪫 Stalled: {', '.join(data['stalled'])}")
        if data["course_deadlines"]:
            lines.append("")
            lines.append("🎓 " + "; ".join(data["course_deadlines"]))
        if data["food_expiring"]:
            lines.append("")
            lines.append(f"🥫 Expiring: {', '.join(data['food_expiring'])}")
        if data["meal_suggestion"]:
            m = data["meal_suggestion"]
            lines.append("")
            lines.append(f"🍳 Suggestion: {m['recipe']} (~{m['time_minutes']}min)")
        rec = data["recommendation"]
        if rec:
            lines.append("")
            lines.append(f"👉 {_hm(rec['start'])}–{_hm(rec['end'])} {rec['title']}")
            why = rec.get("reason")
            if why:
                lines.append(f"   ({why})")
        else:
            lines.append("")
            lines.append("👉 Nothing urgent right now.")
        for n in data["notes"]:
            lines.append(f"   ! {n}")

        data["day_ts"] = day_ts
        data["banner"] = self.cfg.briefing_banner
        data["text"] = "\n".join(lines)
        return data

    def _assemble_briefing(self, day_ts: int, snap: dict[str, Any],
                           now_min: int) -> dict[str, Any]:
        presence = snap.get("presence", {})
        pres_note = ""
        try:
            from .context import describe_location, presence_battery
            pres_note = describe_location(presence) + presence_battery(presence) + "."
        except Exception:  # pragma: no cover
            pres_note = ""
        tasks = self._tasks_safe()
        overdue = [t["title"] for t in tasks if t["status"] in dbmod.ACTIVE_TASK_STATUSES
                   and t.get("deadline") and t["deadline"] < day_ts]
        important = []
        scheduled = []
        stalled = []
        now_local_min = self._now_local(self.cfg).hour * 60 + self._now_local(self.cfg).minute
        for t in tasks:
            dl = t.get("deadline") or 0
            dl_today = self._deadline_min(dl, day_ts)
            if t["status"] in dbmod.ACTIVE_TASK_STATUSES and (
                    int(t.get("priority") or 3) >= 4 or dl_today is not None):
                important.append({"title": t["title"], "priority": int(t.get("priority") or 3),
                                  "deadline_min": dl_today})
            if t["status"] == "scheduled":
                scheduled.append({"title": t["title"], "start": 0, "end": 0})
            if t["status"] in ("deferred", "blocked"):
                stalled.append(t["title"])
        scheduled = self._scheduled_blocks(day_ts) or scheduled

        rec = None
        rec_reason = ""
        if self.planner is not None and hasattr(self.planner, "what_now"):
            try:
                r = self.planner.what_now("", day_ts=day_ts)
                cand = r.get("candidate")
                if cand:
                    rec = {"title": cand["title"], "start": cand["start"], "end": cand["end"]}
                    rec_reason = r.get("reason", "") or ""
                    rec["reason"] = rec_reason
            except Exception as exc:  # pragma: no cover — never break briefing
                log.debug("briefing recommendation failed: %s", exc)

        notes = self._plan_notes(day_ts)
        return {
            "free_minutes_today": snap.get("free_minutes_today", 0),
            "overdue": overdue,
            "important": important,
            "hard_events": snap.get("events_today", []),
            "scheduled_blocks": scheduled,
            "stalled": stalled,
            "course_deadlines": self._course_deadline_lines(snap),
            "food_expiring": [i.get("name", "") for i in snap.get("food_expiring", [])
                              if i.get("name")],
            "meal_suggestion": self._meal_suggestion(),
            "recommendation": rec,
            "notes": notes,
            "presence": presence,
            "presence_note": pres_note,
        }

    def _snapshot(self) -> dict[str, Any]:
        if self.context is None or not hasattr(self.context, "snapshot"):
            return {"free_minutes_today": 0, "events_today": [], "courses": [],
                    "food_expiring": [], "presence": {"known": False, "zone": ""}}
        try:
            return self.context.snapshot()
        except Exception:  # pragma: no cover — degrade
            log.debug("context snapshot failed; using empty", exc_info=True)
            return {"free_minutes_today": 0, "events_today": [], "courses": [],
                    "food_expiring": [], "presence": {"known": False, "zone": ""}}

    def _tasks_safe(self) -> list[dict[str, Any]]:
        try:
            return [dict(r) for r in self.db.all_tasks()]
        except Exception:  # pragma: no cover
            return []

    def _deadline_min(self, deadline_ts: int, day_ts: int) -> int | None:
        """Minute-of-day the task must finish, or None if not today/overdue-
        tomorrow. Overdue (deadline < now-on-this-day) reports 0."""
        if not deadline_ts:
            return None
        day = datetime.fromtimestamp(day_ts)
        dl = datetime.fromtimestamp(deadline_ts)
        if dl.date() == day.date():
            return dl.hour * 60 + dl.minute
        if dl.date() < day.date():
            return 0
        return None

    def _scheduled_blocks(self, day_ts: int) -> list[dict[str, Any]]:
        row = self.db.latest_plan()
        if not row:
            return []
        try:
            state = sch.PlanState.from_json(row["json"])
        except Exception:  # pragma: no cover
            return []
        return [{"title": s.title, "start": s.start_min, "end": s.end_min}
                for s in state.slots]

    def _plan_notes(self, day_ts: int) -> list[str]:
        row = self.db.latest_plan()
        if not row:
            return []
        try:
            return list(sch.PlanState.from_json(row["json"]).notes or [])
        except Exception:  # pragma: no cover
            return []

    def _course_deadline_lines(self, snap: dict[str, Any]) -> list[str]:
        now = int(self._now_local(self.cfg).timestamp())
        out = []
        for c in snap.get("courses", []):
            for d in c.get("upcoming_deadlines", []):
                days = max(0, (int(d) - now) // 86400)
                label = "today" if days == 0 else f"in {days}d"
                out.append(f"{str(c.get('code', ''))}: deadline {label}")
        return out

    def _meal_suggestion(self) -> dict[str, Any] | None:
        fp = self.foodplan
        if fp is None or not hasattr(fp, "peek"):
            return None
        try:
            b = fp.budget()
            if int(b.get("budget_minutes", 0)) <= 0:
                return None
            res = fp.peek()
            plan = (res or {}).get("plan") or {}
            if not plan.get("recipe"):
                return None
            return {"recipe": plan["recipe"], "time_minutes": plan.get("time_minutes", 0)}
        except Exception as exc:  # pragma: no cover
            log.debug("meal suggestion failed: %s", exc)
            return None

    # --------------------------------------------------------------- review
    def review(self, day_ts: int | None = None) -> dict[str, Any]:
        """Synthesise the end-of-day review (planned vs. actual)."""
        day_ts = day_ts or int(self._now_local(self.cfg).timestamp())
        day_start = self._local_midnight(day_ts, self.cfg)
        day_end = day_start + 86400
        planned = self._planned_today(day_ts)
        actual = self._actual_today(day_start, day_end)
        notes = self._plan_notes(day_ts)

        lines = [str(self.cfg.review_banner or "📊 Daily review")]
        day = datetime.fromtimestamp(day_ts).strftime("%A %d %b")
        lines.append(f"{day} — planning vs. results")
        lines.append("")
        lines.append(f"Planned slots   : {planned['count']} ({planned['minutes']} min)")
        lines.append(f"Achieved (done) : {actual['done']}")
        lines.append(f"Deferred        : {actual['deferred']}")
        lines.append(f"Skipped         : {actual['skipped']}")
        lines.append(f"Blocked         : {actual['blocked']}")
        lines.append(f"Cancelled       : {actual['cancelled']}")
        if actual["done"]:
            lines.append("")
            lines.append("✅ Completed: " + ", ".join(actual["done"]))
        if actual["blocked"]:
            lines.append("")
            lines.append("🪫 Blocked: " + ", ".join(actual["blocked"]))
        if actual["deferred"]:
            lines.append("")
            lines.append("↻ Deferred: " + ", ".join(actual["deferred"]))
        unplanned = actual["others"]
        if unplanned:
            lines.append("")
            lines.append("New items: " + ", ".join(unplanned))
        for n in notes:
            lines.append("")
            lines.append(f"! {n}")

        return {
            "kind": REVIEW_KIND,
            "day_ts": day_ts,
            "planned": planned,
            "actual": actual,
            "notes": notes,
            "text": "\n".join(lines),
        }

    def _planned_today(self, day_ts: int) -> dict[str, Any]:
        planned_slots = self._scheduled_blocks(day_ts) or []
        minutes = sum(max(0, (b["end"] - b["start"])) for b in planned_slots)
        return {"count": len(planned_slots), "minutes": minutes}

    def _actual_today(self, day_start: int, day_end: int) -> dict[str, Any]:
        done = []
        deferred = []
        skipped = []
        blocked = []
        cancelled = []
        others = []
        try:
            rows = self.db.query(
                "SELECT DISTINCT t.id, t.title FROM task_history h "
                "JOIN tasks t ON t.id=h.task_id "
                "WHERE h.ts>=? AND h.ts<? "
                "AND h.to_status IN ('completed','deferred','skipped','blocked','cancelled') "
                "ORDER BY h.ts",
                (day_start, day_end))
        except Exception:  # pragma: no cover
            return {"done": done, "deferred": deferred, "skipped": skipped,
                    "blocked": blocked, "cancelled": cancelled, "others": others}
        seen = set()
        # join yields title; aggregate by transition target within the window
        for r in rows:
            key = r["title"]
            to = self._last_transition_today(r["title"], day_start, day_end)
            if to == "completed":
                done.append(key)
            elif to == "deferred":
                deferred.append(key)
            elif to == "skipped":
                skipped.append(key)
            elif to == "blocked":
                blocked.append(key)
            elif to == "cancelled":
                cancelled.append(key)
            seen.add(key)
        # any task created today (spontaneously) with no transition is "new"
        try:
            created = self.db.query(
                "SELECT id, title FROM tasks WHERE created>=? AND created<?",
                (day_start, day_end))
            for c in created:
                title = c["title"]
                if title not in seen:
                    others.append(title)
        except Exception:  # pragma: no cover
            pass
        return {"done": done, "deferred": deferred, "skipped": skipped,
                "blocked": blocked, "cancelled": cancelled, "others": others}

    def _last_transition_today(self, title: str, day_start: int, day_end: int) -> str:
        try:
            row = self.db.one(
                "SELECT h.to_status FROM task_history h JOIN tasks t ON t.id=h.task_id "
                "WHERE t.title=? AND h.ts>=? AND h.ts<? "
                "AND h.to_status IN ('completed','deferred','skipped','blocked','cancelled') "
                "ORDER BY h.ts DESC LIMIT 1",
                (title, day_start, day_end))
            return str(row["to_status"]) if row else ""
        except Exception:  # pragma: no cover
            return ""

    # ----------------------------------------------------------- recommend
    def recommend(self, message: str = "", day_ts: int | None = None,
                  now_min: int | None = None) -> dict[str, Any]:
        """The daily executive recommendation, augmented for the renderer.

        Delegates the actual decision to the deterministic planner (which is the
        single source of truth) and only *presents* the result with a duration
        and the next scheduled commitment. Never changes a plan.
        """
        if self.planner is None or not hasattr(self.planner, "what_now"):
            return {"ok": False, "candidate": None, "answer": "Planner unavailable.",
                    "reason": "", "factors": {}, "duration": None, "next": None}
        r = self.planner.what_now(message, day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate")
        duration = None
        if cand:
            try:
                duration = self._candidate_duration(cand, day_ts)
            except Exception:  # pragma: no cover
                duration = None
        r["duration"] = duration
        r["next"] = self._next_commitment(day_ts, now_min)
        return r

    def _candidate_duration(self, cand: dict[str, Any], day_ts: int) -> int | None:
        try:
            start = self._hm_to_min(cand["start"])
            end = self._hm_to_min(cand["end"])
            return max(0, end - start)
        except Exception:  # pragma: no cover
            return None

    @staticmethod
    def _hm_to_min(hm: str) -> int:
        h, m = str(hm).split(":")
        return int(h) * 60 + int(m)

    def _next_commitment(self, day_ts: int, now_min: int | None) -> str | None:
        if now_min is None:
            now_min = self._now_local(self.cfg).hour * 60 + self._now_local(self.cfg).minute
        try:
            ends = [e["end_min"] for e in self._day_events_mins(day_ts)
                    if e["end_min"] > now_min]
            nxt = min(ends) if ends else None
            return _hm(nxt) if nxt is not None else None
        except Exception:  # pragma: no cover
            return None

    def _day_events_mins(self, day_ts: int) -> list[dict[str, Any]]:
        day = datetime.fromtimestamp(day_ts)
        start_ts = int(datetime(day.year, day.month, day.day).timestamp())
        end_ts = start_ts + 86400
        out = []
        for r in self.db.events_between(start_ts, end_ts):
            s = datetime.fromtimestamp(int(r["start_ts"]))
            e = datetime.fromtimestamp(int(r["end_ts"]))
            start_min = s.hour * 60 + s.minute if s.date() == day.date() else 0
            end_min = e.hour * 60 + e.minute if e.date() == day.date() else 1440
            if end_min > start_min:
                out.append({"title": r["title"], "start_min": start_min,
                            "end_min": end_min})
        return out
