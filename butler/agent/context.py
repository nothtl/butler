"""Phase 7 / M1: deterministic context builder.

Wraps the existing :class:`butler.context.ContextEngine` into a single
:class:`ContextBundle` that reasoning and tools can consume. No LLM is involved
here — the bundle is pure DB + scheduler geometry, so it is reproducible and
instant.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .models import ContextBundle
from .semantic import (AgentRequest, ContextSnapshot, ScopeKind)

log = logging.getLogger("butler.agent.context")


class ContextBuilder:
    def __init__(self, container: Any):
        self.container = container
        self.engine = getattr(container, "context", None)

    def build(self, *, focus: str = "") -> ContextBundle:
        """Return the live context. Never raises; degrades to an empty bundle."""
        data: dict[str, Any] = {}
        text = ""
        sources: list[str] = []
        engine = self.engine
        if engine is not None and hasattr(engine, "snapshot"):
            try:
                data = dict(engine.snapshot())
                sources.append("context_engine")
            except Exception:  # noqa: BLE001 — context must never break a reply
                log.debug("context snapshot failed", exc_info=True)
        if engine is not None and hasattr(engine, "describe"):
            try:
                text = str(engine.describe())
            except Exception:  # noqa: BLE001
                text = ""
        if focus:
            data = dict(data)
            data["focus"] = focus
        return ContextBundle(
            now=int(data.get("now", 0) or 0),
            text=text,
            data=data,
            sources=sources,
        )

    # --------------------------------------------------------- typed snapshot
    def build_snapshot(self, request: AgentRequest, *,
                       task_limit: int = 20, course_limit: int = 20,
                       event_limit: int = 40, day_limit: int = 14
                       ) -> ContextSnapshot:
        """A typed, request-scoped, bounded slice of live state.

        Only the days implied by ``request.scope`` are visited, entity filters
        are applied, and every list is capped (``truncated`` records that).
        Never raises: a missing subsystem simply contributes nothing.
        """
        cfg = getattr(self.container, "cfg", None)
        planner = getattr(self.container, "planner", None)
        snap = self._engine_snapshot()
        now = int(snap.get("now") or _now_ts(cfg))
        tz_name = str(getattr(cfg, "timezone", "") or "")

        day_start = int(snap.get("day_start") or _midnight(planner, cfg, now))
        day_end = int(snap.get("day_end") or (day_start + 86400))
        sleep_start = int(getattr(cfg, "sleep_start", 23 * 60) or 0)
        sleep_end = int(getattr(cfg, "sleep_end", 7 * 60) or 0)

        days = self._scope_days(request, planner, now)
        commitments, windows, truncated = self._geometry(
            days, planner, sleep_start, sleep_end, event_limit)
        tasks, task_trunc = self._tasks(snap, request, days, task_limit)
        courses, deadlines = self._courses(snap, request, now, days, course_limit)
        projects, proj_trunc = self._projects(request, now, limit=20)
        truncated = truncated or task_trunc or proj_trunc

        sources = ["context_engine"] if snap else []
        if planner is not None:
            sources.append("planner")
        if commitments:
            sources.append("calendar")
        if courses:
            sources.append("courses")
        if tasks:
            sources.append("tasks")
        if projects:
            sources.append("projects")
        presence = snap.get("presence") or {}
        routines = self._routines()
        current_plan = self._current_plan(request, planner, days)

        return ContextSnapshot(
            now=now, timezone=tz_name, day_start=day_start, day_end=day_end,
            sleep_start=sleep_start, sleep_end=sleep_end,
            commitments=commitments, available_windows=windows,
            tasks=tasks, courses=courses, projects=projects,
            deadlines=deadlines,
            current_plan=current_plan, presence=presence, routines=routines,
            preferences=list(request.preferences), sources=sources,
            truncated=truncated, focus=request.target.name if request.target else "",
        )

    # ------------------------------------------------------------- internals
    def _engine_snapshot(self) -> dict[str, Any]:
        engine = self.engine
        if engine is None or not hasattr(engine, "snapshot"):
            return {}
        try:
            return dict(engine.snapshot())
        except Exception:  # noqa: BLE001
            log.debug("engine snapshot failed", exc_info=True)
            return {}

    def _scope_days(self, request: AgentRequest, planner: Any,
                    now: int) -> list[int]:
        today = _day_start(planner, now)
        kind = request.scope.kind
        scope = request.scope
        days: list[int] = [today]
        if kind == ScopeKind.TOMORROW:
            days = [_shift(planner, today, 1)]
        elif kind == ScopeKind.THIS_WEEK:
            end = int(scope.end or 0)
            d = today
            while (not end or d < end) and len(days) < 7:
                days.append(d)
                d = _shift(planner, d, 1)
        elif kind == ScopeKind.NEXT_WEEK:
            d = int(scope.start or _shift(planner, today, 7))
            for _ in range(7):
                days.append(d)
                d = _shift(planner, d, 1)
        elif kind == ScopeKind.RANGE:
            start = int(request.temporal.start or now)
            end = int(request.temporal.end or start)
            d = _day_start(planner, start)
            last = _day_start(planner, end)
            while d <= last and len(days) < 14:
                days.append(d)
                d = _shift(planner, d, 1)
        # de-duplicate, preserve order, cap
        seen: list[int] = []
        for d in days:
            if d not in seen:
                seen.append(d)
        return seen[:14]

    def _geometry(self, days: list[int], planner: Any, sleep_start: int,
                  sleep_end: int, event_limit: int
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
        from .. import schedule as sch
        commitments: list[dict[str, Any]] = []
        windows: list[dict[str, Any]] = []
        truncated = False
        if planner is None:
            return commitments, windows, truncated
        for day in days:
            try:
                events = planner._day_events(day)
            except Exception:  # noqa: BLE001
                events = []
            for e in events:
                if len(commitments) >= event_limit:
                    truncated = True
                    break
                commitments.append({
                    "day": day, "id": int(getattr(e, "id", 0) or 0),
                    "title": str(getattr(e, "title", "")),
                    "start_ts": day + int(e.start_min) * 60,
                    "end_ts": day + int(e.end_min) * 60,
                    "start_min": int(e.start_min), "end_min": int(e.end_min),
                    "source": str(getattr(e, "source", "") or ""),
                })
            try:
                gaps = sch.free_intervals(int(planner.day_start),
                                          int(planner.day_end),
                                          sleep_start, sleep_end, events)
            except Exception:  # noqa: BLE001
                gaps = []
            for a, b in gaps:
                windows.append({"day": day, "start_min": int(a),
                                "end_min": int(b), "minutes": int(b - a)})
        return commitments, windows, truncated

    def _tasks(self, snap: dict[str, Any], request: AgentRequest,
               days: list[int], limit: int
               ) -> tuple[list[dict[str, Any]], bool]:
        rows = snap.get("tasks") or []
        codes = {e.name.lower() for e in request.entities
                 if e.type.value == "course" and e.name}
        horizon = days[-1] + 86400 if days else 0
        out: list[dict[str, Any]] = []
        for t in rows:
            title = str(t.get("title", ""))
            if codes and not any(c in title.lower() for c in codes):
                continue
            deadline = int(t.get("deadline") or 0)
            item = dict(t)
            item["title"] = title
            item["deadline"] = deadline
            if deadline:
                item["deadline_iso"] = _iso(deadline)
                item["deadline_min"] = _minute_of_day(deadline)
            out.append(item)
        out.sort(key=lambda t: (0 if t.get("deadline") else 1,
                                t.get("deadline") or 0,
                                -(int(t.get("est_minutes") or 0))))
        truncated = len(out) > limit
        return out[:limit], truncated

    def _courses(self, snap: dict[str, Any], request: AgentRequest,
                 now: int, days: list[int], limit: int
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = snap.get("courses") or []
        codes = {e.name.lower() for e in request.entities
                 if e.type.value == "course" and e.name}
        until = (days[-1] + 86400) if days else (now + 7 * 86400)
        courses: list[dict[str, Any]] = []
        deadlines: list[dict[str, Any]] = []
        for c in rows:
            code = str(c.get("code", ""))
            if codes and code.lower() not in codes:
                continue
            courses.append(dict(c))
            for d in c.get("upcoming_deadlines") or []:
                if now <= int(d) <= until:
                    deadlines.append({"kind": "course", "ref": code,
                                      "title": code, "ts": int(d),
                                      "iso": _iso(int(d))})
        deadlines.sort(key=lambda x: x["ts"])
        return courses[:limit], deadlines[:30]

    def _projects(self, request: AgentRequest, now: int,
                  limit: int) -> tuple[list[dict[str, Any]], bool]:
        pmod = getattr(self.container, "projects", None)
        if pmod is None or not hasattr(pmod, "list_projects"):
            return [], False
        try:
            rows = pmod.list_projects(now=now)
        except Exception:  # noqa: BLE001
            return [], False
        names = {e.name.lower() for e in request.entities
                 if e.type.value == "project" and e.name}
        if request.target is not None and request.target.type.value == "project" \
                and request.target.name:
            names.add(request.target.name.lower())
        out: list[dict[str, Any]] = []
        for p in rows:
            if names and not any(n in str(p.get("name", "")).lower()
                                 for n in names):
                continue
            out.append({
                "id": p.get("id"), "name": p.get("name"),
                "status": p.get("status"), "priority": p.get("priority"),
                "deadline": p.get("deadline"), "course_id": p.get("course_id"),
                "progress": p.get("progress"),
                "progress_source": p.get("progress_source"),
                "remaining_minutes": p.get("remaining_minutes"),
                "risk": p.get("risk"), "risk_level": p.get("risk_level"),
                "milestone_count": len(p.get("milestones") or []),
            })
        out.sort(key=lambda p: (0 if p.get("deadline") else 1,
                                p.get("deadline") or 0,
                                -(int(p.get("priority") or 0))))
        truncated = len(out) > limit
        return out[:limit], truncated

    def _routines(self) -> list[dict[str, Any]]:
        routines = getattr(self.container, "routines", None)
        if routines is None or not hasattr(routines, "active"):
            return []
        try:
            return [dict(r) if not isinstance(r, dict) else r
                    for r in (routines.active() or [])][:10]
        except Exception:  # noqa: BLE001
            return []

    def _current_plan(self, request: AgentRequest, planner: Any,
                      days: list[int]) -> dict[str, Any] | None:
        if request.scope.kind not in (ScopeKind.NOW, ScopeKind.TODAY,
                                      ScopeKind.TONIGHT):
            return None
        db = getattr(self.container, "db", None)
        if db is None or not hasattr(db, "latest_plan"):
            return None
        try:
            row = db.latest_plan()
            if row is None:
                return None
            return {"plan_id": int(row["id"]), "created": int(row["created"]),
                    "slot_count": len(str(row["json"]))}
        except Exception:  # noqa: BLE001
            return None


def _now_ts(cfg: Any) -> int:
    if cfg is not None and hasattr(cfg, "now_local"):
        try:
            return int(cfg.now_local().timestamp())
        except Exception:  # noqa: BLE001
            pass
    return int(time.time())


def _day_start(planner: Any, ts: int) -> int:
    if planner is not None and hasattr(planner, "_day_start_ts"):
        try:
            return int(planner._day_start_ts(ts))
        except Exception:  # noqa: BLE001
            pass
    dt = datetime.fromtimestamp(ts)
    return int(datetime(dt.year, dt.month, dt.day).timestamp())


def _midnight(planner: Any, cfg: Any, ts: int) -> int:
    if cfg is not None and hasattr(cfg, "local_midnight"):
        try:
            return int(cfg.local_midnight(ts))
        except Exception:  # noqa: BLE001
            pass
    return _day_start(planner, ts)


def _shift(planner: Any, day: int, n: int) -> int:
    return _day_start(planner, day + int(n) * 86400)


def _iso(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _minute_of_day(ts: int) -> int:
    dt = datetime.fromtimestamp(int(ts))
    return dt.hour * 60 + dt.minute
