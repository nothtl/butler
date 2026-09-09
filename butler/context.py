"""Phase 3: Central Context Engine.

A single deterministic aggregation of the user's situation that every subsystem
(course, food, file, proactive, Telegram, CLI) can query. The scheduler remains
the sole entity that places work — this engine only *reports* free time, it
never mutates it.

``snapshot()`` returns a machine-readable dict; ``describe()`` produces a
human summary (e.g. for a morning briefing). No LLM is used to build context —
it is pure DB + scheduler geometry so it is reproducible and instant.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import schedule

log = logging.getLogger("butler.context")

SLEEP_START_MIN = 23 * 60
SLEEP_END_MIN = 7 * 60


def _midnight_ts(ts: int) -> int:
    dt = datetime.fromtimestamp(ts)
    return int(datetime(dt.year, dt.month, dt.day).timestamp())


def describe_location(pres: dict[str, Any]) -> str:
    """A natural sentence describing where the user is (no trailing period)."""
    status = pres.get("status", "unknown")
    zone = (pres.get("zone") or "").strip()
    if status == "unknown" or not pres.get("known"):
        return "I can't tell where you are right now"
    if status == "home" or zone.lower() in ("home",):
        return "You're at home"
    if zone:
        return f"You're at {zone}"
    return "You're away from home"


def presence_battery(pres: dict[str, Any]) -> str:
    bat = pres.get("battery")
    return f", battery {bat}%" if isinstance(bat, int) else ""


class ContextEngine:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.timeline = getattr(container, "timeline", None)

    def _local_midnight(self, ts: int) -> int:
        """Absolute timestamp of the local midnight containing ``ts`` (tz-aware
        when ``[user] timezone`` is set, otherwise the host's local time)."""
        cfg = getattr(self, "cfg", None)
        if cfg is not None and hasattr(cfg, "local_midnight"):
            try:
                return cfg.local_midnight(ts)
            except Exception:  # pragma: no cover — tz invalid => fall back
                pass
        return _midnight_ts(ts)

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        now = int(datetime.now().timestamp())
        day_start = self._local_midnight(now)
        day_end = day_start + 86400
        week_end = day_start + 7 * 86400
        presence = self._presence()
        self._observe_timeline(presence)
        return {
            "now": now,
            "day_start": day_start,
            "day_end": day_end,
            "free_minutes_today": self.free_minutes(day_start, day_end),
            "events_today": self._events(day_start, day_end),
            "events_week": self._events(day_start, week_end),
            "tasks": self._tasks(),
            "courses": self._courses(now, week_end),
            "courses_document_count": self._course_doc_count(),
            "food_expiring": self._food_expiring(),
            "file_count": self._file_count(),
            "presence": presence,
            "timeline": self._timeline_snapshot(day_start, day_end),
        }

    def _observe_timeline(self, presence: dict[str, Any]) -> None:
        """Record a zone change if the HA presence actually moved (idempotent).

        Recording is passive and never raises: a HA outage just means no event.
        """
        tl = self.timeline
        if tl is None or not hasattr(tl, "observe_presence"):
            return
        try:
            tl.observe_presence(presence)
        except Exception:  # pragma: no cover — timeline must never break context
            log.debug("timeline observe failed", exc_info=True)

    def _timeline_snapshot(self, day_start: int, day_end: int) -> dict[str, Any]:
        """Current vs historical context, distinct: ``current`` is the live
        zone; ``today`` is the durable history for this calendar day."""
        tl = self.timeline
        if tl is None or not hasattr(tl, "current_context"):
            return {"current": {"known": False, "zone": ""}, "today": []}
        try:
            return {"current": tl.current_context(),
                    "today": tl.get_between(day_start, day_end)[:50]}
        except Exception:  # pragma: no cover
            return {"current": {"known": False, "zone": ""}, "today": []}

    def free_minutes(self, day_start: int, day_end: int) -> int:
        """Waking free time (minutes, minute-of-day window) on a day.

        ``day_start``/``day_end`` are absolute timestamps used only to fetch the
        events that fall on that day; the geometric window is minute-of-day.
        """
        events = self._schedule_events(day_start, day_end)
        gaps = schedule.free_intervals(0, 1440, SLEEP_START_MIN, SLEEP_END_MIN, events)
        return sum(g[1] - g[0] for g in gaps)

    def _schedule_events(self, day_start: int, day_end: int) -> list[schedule.Event]:
        out: list[schedule.Event] = []
        for r in self.db.events_between(day_start, day_end):
            start_min = int((int(r["start_ts"]) % 86400) / 60)
            end_min = int((int(r["end_ts"]) % 86400) / 60)
            if end_min <= start_min:
                end_min += 24 * 60
            out.append(schedule.Event(id=int(r["id"]), title=str(r["title"]),
                                      start_min=start_min, end_min=end_min,
                                      source=str(r["source"] or "local")))
        return out

    def _events(self, start: int, end: int) -> list[dict[str, Any]]:
        return [{"id": int(r["id"]), "title": str(r["title"]),
                 "start_ts": int(r["start_ts"]), "end_ts": int(r["end_ts"]),
                 "source": str(r["source"] or "local")}
                for r in self.db.events_between(start, end)]

    def _tasks(self) -> list[dict[str, Any]]:
        return [{"id": int(r["id"]), "title": str(r["title"]),
                 "est_minutes": int(r["est_minutes"] or 0),
                 "deadline": int(r["deadline"] or 0),
                 "priority": str(r["priority"] or ""),
                 "status": str(r["status"] or "todo")}
                for r in self.db.tasks("active")][:20]

    def _courses(self, now: int, until: int) -> list[dict[str, Any]]:
        out = []
        for r in self.db.courses():
            # aggregate deadlines from course documents (authoritative dates)
            docs = [dict(d) for d in self.db.course_documents(int(r["id"]))]
            deadlines = [d["deadline"] for d in docs if d.get("deadline")]
            upcoming = sorted([d for d in deadlines if now <= d <= until])
            out.append({"code": str(r["code"]), "name": str(r["name"]),
                        "monitoring": bool(r["monitoring_enabled"]),
                        "documents": len(docs),
                        "upcoming_deadlines": [int(d) for d in upcoming]})
        return out

    def _course_doc_count(self) -> int:
        try:
            row = self.db.one("SELECT COUNT(*) AS n FROM course_documents")
            return int(row["n"]) if row else 0
        except Exception:  # pragma: no cover
            return 0

    def _food_expiring(self) -> list[dict[str, Any]]:
        if not hasattr(self.container, "food") and not hasattr(self.container, "chef"):
            return []
        food = getattr(self.container, "food", None)
        if food is not None and hasattr(food, "expiring"):
            return food.expiring(3)
        chef = getattr(self.container, "chef", None)
        return chef.inventory.expiring(3) if chef else []

    def _file_count(self) -> int:
        try:
            row = self.db.one("SELECT COUNT(*) AS n FROM files WHERE is_dir=0")
            return int(row["n"]) if row else 0
        except Exception:  # pragma: no cover
            return 0

    def _presence(self) -> dict[str, Any]:
        """Zone-level presence (Phase 4.1). Never raises; degrades to unknown."""
        ha = getattr(self.container, "ha", None)
        if ha is None or not hasattr(ha, "presence"):
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant"}
        try:
            return ha.presence()
        except Exception:  # pragma: no cover — degrade on any unexpected failure
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant"}

    # ------------------------------------------------------------ describe
    def describe(self) -> str:
        s = self.snapshot()
        lines = ["🧠 Context", ""]
        now = datetime.fromtimestamp(s["now"]).strftime("%A %H:%M")
        lines.append(f"Now: {now}")
        lines.append(f"Free time today: ~{s['free_minutes_today']} min")
        ev = s["events_today"]
        lines.append(f"Hard events today: {len(ev)}" +
                     (" " + ", ".join(e["title"] for e in ev[:5]) if ev else ""))
        tasks = s["tasks"]
        if tasks:
            lines.append(f"Active tasks: {len(tasks)}")
            for t in tasks[:6]:
                lines.append(f"  • {t['title']}")
        else:
            lines.append("Active tasks: none")
        courses = s["courses"]
        if courses:
            lines.append(f"Courses: {len(courses)}")
            for c in courses:
                d = c["upcoming_deadlines"]
                if d:
                    lines.append(f"  • {c['code']} — deadline(s) in next 7d")
        exp = s["food_expiring"]
        if exp:
            lines.append(f"Food expiring in 3d: "
                         + ", ".join(f"{i['name']}" for i in exp[:5]))
        lines.append(f"Indexed files: {s['file_count']}")
        pres = s.get("presence", {})
        zone = pres.get("zone") or "an unknown location"
        status = pres.get("status", "unknown")
        lines.append(f"Presence: {status} ({zone})")
        return "\n".join(lines)
