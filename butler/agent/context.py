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
        topic, topic_context = self._topic(request, now)
        memories, mem_summary, mem_warnings = self._memory(request, now, topic)
        if memories:
            sources.append("memory")
        if topic:
            sources.append("topic")
            tasks = _topic_boost(tasks, topic_context, "task")
            courses = _topic_boost(courses, topic_context, "course")
            projects = _topic_boost(projects, topic_context, "project")

        return ContextSnapshot(
            now=now, timezone=tz_name, day_start=day_start, day_end=day_end,
            sleep_start=sleep_start, sleep_end=sleep_end,
            commitments=commitments, available_windows=windows,
            tasks=tasks, courses=courses, projects=projects,
            deadlines=deadlines,
            current_plan=current_plan, presence=presence, routines=routines,
            preferences=list(request.preferences), sources=sources,
            truncated=truncated, focus=request.target.name if request.target else "",
            relevant_memories=memories, memory_summary=mem_summary,
            memory_warnings=mem_warnings, memory_timestamp=now,
            topic=topic, topic_context=topic_context,
        )

    # ------------------------------------------------------------- topic
    def _topic(self, request: AgentRequest, now: int
               ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Load the current topic profile + linked data (relevance context).

        A topic is a *boost*, never a hard boundary: the returned data is added
        to the snapshot so relevant items sort first, but global queries still
        see everything.
        """
        ref = dict(request.topic or {})
        if not ref:
            return {}, {}
        topics = getattr(self.container, "topics", None)
        if topics is None:
            return ref, {}
        try:
            prof = None
            if ref.get("chat_id") is not None:
                prof = topics.get(int(ref.get("chat_id")),
                                  int(ref.get("thread_id") or 0))
            if prof is None:
                return ref, {}
            if prof.status != "active":
                return prof.to_dict(), {}
            return prof.to_dict(), topics.linked_data(prof)
        except Exception:  # noqa: BLE001 — topic context must never break a reply
            log.debug("topic context failed", exc_info=True)
            return ref, {}


    # ------------------------------------------------------------- memory
    def _memory(self, request: AgentRequest, now: int,
                topic: dict[str, Any] | None = None
                ) -> tuple[list[dict[str, Any]], str, list[str]]:
        """Bounded retrieval of relevant long-term memories for this request."""
        mem = getattr(self.container, "memory", None)
        if mem is None or not hasattr(mem, "get_relevant"):
            return [], "", []
        try:
            subject = ""
            if request.target is not None and request.target.name:
                subject = request.target.name
            entities = [e.name for e in request.entities if e.name][:6]
            context = {"query": request.raw_text or "", "subject": subject,
                       "entities": entities,
                       "types": [e.type.value for e in request.entities][:4]}
            limit = int(getattr(self.container.cfg, "memory_context_limit", 5))
            rows = mem.get_relevant(context, limit=limit, now=now)
            # Topic-scoped memory is a *relevance boost*: global memory is always
            # retrievable, and topic-tagged memory is added on top (never a
            # hard boundary). Uses the same single M6 store.
            if topic and topic.get("name"):
                tag = f"topic:{str(topic['name']).lower()}"
                scoped = mem.get_relevant({"query": tag, "tags": [tag]},
                                          limit=limit, now=now)
                seen = {r.get("id") for r in rows}
                for r in scoped:
                    if r.get("id") not in seen:
                        rows.append(r)
                        seen.add(r.get("id"))
                rows = rows[:limit]
            warnings: list[str] = []
            if any(r.get("provenance") in ("routine_inferred", "llm_inferred")
                   for r in rows):
                warnings.append("some memories are inferred, not confirmed")
            if any(r.get("scope") == "external" for r in rows):
                warnings.append("some memories are external evidence, not "
                                "personal facts")
            summary = ""
            if rows:
                summary = "; ".join(
                    f"{r.get('subject') or r.get('type')}: {r.get('value')}"
                    for r in rows[:limit])
            return rows, summary, warnings
        except Exception:  # noqa: BLE001 — memory must never break context
            log.debug("memory retrieval failed", exc_info=True)
            return [], "", []

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


def _topic_boost(items: list[dict[str, Any]], topic_context: dict[str, Any],
                 kind: str) -> list[dict[str, Any]]:
    """Mark topic-linked items and sort them first (relevance, not a filter)."""
    if not items or not topic_context:
        return items
    ids: set[int] = set()
    names: set[str] = set()
    if kind == "task":
        for t in topic_context.get("tasks", []) or []:
            ids.add(int(t.get("id") or 0))
            names.add(str(t.get("title") or "").lower())
    elif kind == "course":
        for c in topic_context.get("courses", []) or []:
            ids.add(int(c.get("id") or 0))
            names.add(str(c.get("code") or "").lower())
    elif kind == "project":
        for p in topic_context.get("projects", []) or []:
            ids.add(int(p.get("id") or 0))
            names.add(str(p.get("name") or "").lower())
    if not ids and not names:
        return items
    for it in items:
        try:
            iid = int(it.get("id") or 0)
        except (TypeError, ValueError):
            iid = 0
        iname = str(it.get("name") or it.get("code") or it.get("title") or "").lower()
        if (iid and iid in ids) or (iname and iname in names):
            it["topic_relevant"] = True
    return sorted(items, key=lambda it: 0 if it.get("topic_relevant") else 1)
