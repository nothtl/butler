"""M3 project intelligence: durable project/milestone/dependency model.

A *project* is the durable unit of real work — a goal broken into milestones and
carried out by ordinary :mod:`butler.db` tasks. This module is deliberately
deterministic and side-effect-free on reads:

* **Effort-based progress.** Progress is completed minutes over estimated
  minutes, never a completed-task count. When no estimate exists the answer is
  ``unknown`` rather than a misleading number.
* **Explainable risk.** Risk is a weighted sum of named, deterministic factors
  (deadline pressure, overdue milestones, dependency bottlenecks, stalled
  progress, estimate uncertainty). No LLM ever invents a score.
* **Validated dependency DAG.** Edges are rejected if they would create a
  cycle. Inferred edges are advisory and never enforced silently.
* **Provenance.** Every inferred field stays marked inferred so a guessed
  deadline or effort can never silently become authoritative.

Writes are explicit methods; the executive service gates them behind
confirmation and the safety policy. Nothing here touches the scheduler's
semantics or the external calendar.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta
from typing import Any

from . import schedule as sch
from .db import normalize_status

PROJECT_STATUSES = ("active", "paused", "completed", "archived", "cancelled")
MILESTONE_STATUSES = ("pending", "active", "completed", "skipped")

_DONE = ("completed",)
_EXCLUDED = ("skipped", "cancelled")

# Deterministic risk weights (sum to 1.0).
_W_DEADLINE = 0.40
_W_MILESTONE = 0.20
_W_DEPENDENCY = 0.15
_W_STALLED = 0.15
_W_ESTIMATE = 0.10

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")

_HOURS_RE = re.compile(r"~?\s*(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", re.I)
_COURSE_RE = re.compile(r"\b([A-Z]{2,6}\s?\d{2,4}[A-Z]?)\b")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _task_effort(row: Any) -> tuple[int, int]:
    """Return ``(estimated_minutes, remaining_minutes)`` for a task row."""
    est = int((row["est_minutes"] if row["est_minutes"] else 0) or 0)
    try:
        raw_rem = int(row["remaining_minutes"] or 0)
    except (IndexError, KeyError):
        raw_rem = 0
    return est, (raw_rem if raw_rem > 0 else est)


class ProjectIntelligence:
    """Domain service over the ``projects``/``milestones``/``task_deps`` tables."""

    def __init__(self, container: Any):
        self.container = container
        self.db = container.db
        self.cfg = getattr(container, "cfg", None)

    # ------------------------------------------------------------------ time
    def _now(self) -> int:
        cfg = self.cfg
        if cfg is not None and hasattr(cfg, "now_local"):
            try:
                return int(cfg.now_local().timestamp())
            except Exception:  # pragma: no cover — bad tz falls back
                pass
        return int(time.time())

    def _midnight(self, ts: int) -> int:
        cfg = self.cfg
        if cfg is not None and hasattr(cfg, "local_midnight"):
            try:
                return int(cfg.local_midnight(ts))
            except Exception:  # pragma: no cover
                pass
        d = datetime.fromtimestamp(ts)
        return int(datetime(d.year, d.month, d.day).timestamp())

    # -------------------------------------------------------------- resolve
    def _resolve(self, ident: Any) -> Any:
        """Resolve a project by id, exact name, or case-insensitive substring."""
        if ident is None:
            return None
        if hasattr(ident, "keys") and "id" in ident.keys():
            return ident
        if isinstance(ident, int) or (isinstance(ident, str) and ident.isdigit()):
            return self.db.project_by_id(int(ident))
        key = str(ident).strip().lower()
        if not key:
            return None
        rows = self.db.projects()
        for r in rows:
            if str(r["name"]).strip().lower() == key:
                return r
        matches = [r for r in rows if key in str(r["name"]).strip().lower()]
        return matches[0] if len(matches) == 1 else None

    # --------------------------------------------------------------- effort
    @staticmethod
    def _effort(rows: list[Any]) -> dict[str, Any]:
        est_total = 0
        completed = 0
        remaining = 0
        for r in rows:
            status = normalize_status(r["status"] or "todo")
            if status in _EXCLUDED:
                continue
            est, rem = _task_effort(r)
            est_total += est
            if status in _DONE:
                completed += est
            else:
                remaining += rem
        value = (completed / est_total) if est_total > 0 else None
        return {"estimated_minutes": est_total, "completed_minutes": completed,
                "remaining_minutes": remaining, "progress": value,
                "effort_known": est_total > 0}

    # ------------------------------------------------------------- workload
    def workload(self, ident: Any) -> dict[str, Any] | None:
        project = self._resolve(ident)
        if project is None:
            return None
        pid = int(project["id"])
        rows = self.db.project_tasks(pid)
        eff = self._effort(rows)

        # A project may carry an explicit estimate/remaining with no tasks yet.
        explicit_est = int(project["estimated_total_minutes"] or 0)
        explicit_rem = int(project["remaining_minutes"] or 0)
        est_total = eff["estimated_minutes"] or explicit_est
        if not rows and explicit_rem:
            remaining = explicit_rem
            completed = max(0, est_total - explicit_rem)
            progress = (completed / est_total) if est_total > 0 else None
            source = "explicit"
        elif not rows:
            remaining = explicit_rem or 0
            completed = 0
            progress = None
            source = "unknown" if not est_total else "derived"
        else:
            remaining = eff["remaining_minutes"]
            completed = eff["completed_minutes"]
            progress = eff["progress"]
            source = "derived" if eff["effort_known"] else "unknown"

        active = [r for r in rows
                  if normalize_status(r["status"] or "todo")
                  not in _EXCLUDED + _DONE]
        blocked = self.blocked_task_ids(pid)
        milestone_rows = self.db.milestones(pid)
        milestones = []
        for m in milestone_rows:
            mrows = [r for r in rows if int(r["milestone_id"] or 0) == int(m["id"])]
            meff = self._effort(mrows)
            milestones.append({
                "id": int(m["id"]), "name": m["name"],
                "status": m["status"], "deadline": int(m["deadline"] or 0),
                "order_index": int(m["order_index"] or 0),
                "estimated_minutes": meff["estimated_minutes"],
                "completed_minutes": meff["completed_minutes"],
                "remaining_minutes": meff["remaining_minutes"],
                "progress": meff["progress"], "task_count": len(mrows),
            })

        avail = self._available_minutes(project["deadline"]) \
            if int(project["deadline"] or 0) else None
        feasible = None
        if avail is not None:
            feasible = remaining <= avail
        return {
            "project_id": pid, "name": project["name"],
            "status": project["status"], "priority": int(project["priority"] or 3),
            "deadline": int(project["deadline"] or 0),
            "course_id": int(project["course_id"] or 0),
            "estimated_minutes": est_total,
            "completed_minutes": completed,
            "remaining_minutes": remaining,
            "progress": progress, "progress_source": source,
            "task_count": len(rows), "active_count": len(active),
            "completed_count": sum(1 for r in rows
                                   if normalize_status(r["status"] or "todo")
                                   in _DONE),
            "blocked_task_ids": blocked,
            "milestones": milestones,
            "available_minutes_until_deadline": avail,
            "feasible": feasible,
            "next_tasks": self.candidates(ident, limit=5),
        }

    def candidates(self, ident: Any, *, limit: int = 5) -> list[dict[str, Any]]:
        """Read-only ranking of active project tasks (blocked ones last)."""
        project = self._resolve(ident)
        if project is None:
            return []
        pid = int(project["id"])
        rows = [r for r in self.db.project_tasks(pid)
                if normalize_status(r["status"] or "todo")
                not in _EXCLUDED + _DONE]
        blocked = self.blocked_task_ids(pid)
        ordered = sorted(
            rows, key=lambda r: (1 if int(r["id"]) in blocked else 0,
                                 -int(r["priority"] or 3),
                                 int(r["deadline"] or 0) or 1 << 62,
                                 int(r["id"])))
        out = []
        for r in ordered[:max(0, limit)]:
            est, rem = _task_effort(r)
            out.append({"task_id": int(r["id"]), "title": r["title"],
                        "status": normalize_status(r["status"] or "todo"),
                        "priority": int(r["priority"] or 3),
                        "deadline": int(r["deadline"] or 0),
                        "remaining_minutes": rem,
                        "blocked": int(r["id"]) in blocked})
        return out

    # ------------------------------------------------------------ available
    def _available_minutes(self, deadline: int) -> int | None:
        """Free usable minutes from now until ``deadline`` using planner
        geometry. Read-only; never commits or syncs anything."""
        planner = getattr(self.container, "planner", None)
        if planner is None or self.cfg is None:
            return None
        now = self._now()
        if deadline <= now:
            return 0
        try:
            base = self._midnight(now)
            days = min(60, max(1, (deadline - base) // 86400 + 1))
            total = 0
            for i in range(int(days)):
                day_ts = int((datetime.fromtimestamp(base)
                              + timedelta(days=i)).timestamp())
                events = planner._day_events(day_ts)
                total += sch.day_capacity(
                    planner.day_start, planner.day_end, events,
                    self.cfg.buffer_fraction, self.cfg.buffer_minutes,
                    self.cfg.min_slot_minutes,
                    int(self.cfg.sleep_start), int(self.cfg.sleep_end))
            return int(total)
        except Exception:  # pragma: no cover — geometry is best-effort
            return None

    # ---------------------------------------------------------------- risk
    def risk(self, ident: Any, *, now: int | None = None) -> dict[str, Any] | None:
        project = self._resolve(ident)
        if project is None:
            return None
        now = int(now if now is not None else self._now())
        wl = self.workload(int(project["id"]))
        assert wl is not None
        remaining = int(wl["remaining_minutes"])
        deadline = int(project["deadline"] or 0)
        factors: list[dict[str, Any]] = []

        # 1. deadline pressure ------------------------------------------------
        if deadline:
            avail = wl["available_minutes_until_deadline"]
            if avail is None:
                pressure = None
            elif remaining <= 0:
                pressure = 0.0
            elif avail <= 0:
                pressure = 1.0
            else:
                pressure = _clamp(remaining / float(avail))
            factors.append({
                "name": "deadline_pressure", "weight": _W_DEADLINE,
                "value": pressure,
                "detail": (f"{remaining}m remaining vs "
                           f"{'unknown' if avail is None else str(avail)+'m'} "
                           "available before deadline"),
            })
        else:
            factors.append({"name": "deadline_pressure", "weight": 0.0,
                            "value": None, "detail": "no deadline set"})

        # 2. overdue milestones ----------------------------------------------
        ms = self.db.milestones(int(project["id"]))
        overdue = [m for m in ms
                   if int(m["deadline"] or 0) and int(m["deadline"]) < now
                   and normalize_status(m["status"] or "pending")
                   not in _DONE + _EXCLUDED]
        ms_ratio = (len(overdue) / len(ms)) if ms else 0.0
        factors.append({
            "name": "overdue_milestones", "weight": _W_MILESTONE,
            "value": _clamp(ms_ratio),
            "detail": (f"{len(overdue)} of {len(ms)} milestones overdue"
                       if ms else "no milestones"),
        })

        # 3. dependency bottleneck -------------------------------------------
        active = [r for r in self.db.project_tasks(int(project["id"]))
                  if normalize_status(r["status"] or "todo")
                  not in _EXCLUDED + _DONE]
        blocked = self.blocked_task_ids(int(project["id"]))
        dep_ratio = (len(blocked) / len(active)) if active else 0.0
        factors.append({
            "name": "dependency_bottleneck", "weight": _W_DEPENDENCY,
            "value": _clamp(dep_ratio),
            "detail": f"{len(blocked)} of {len(active)} active tasks blocked",
        })

        # 4. stalled progress -------------------------------------------------
        rows = self.db.project_tasks(int(project["id"]))
        last_done = max([int(r["completed"] or 0) for r in rows
                         if normalize_status(r["status"] or "todo") in _DONE]
                        or [0])
        anchor = last_done or int(project["created_at"] or 0)
        stalled = 0.0
        if anchor and (now - anchor) > 7 * 86400:
            stalled = _clamp((now - anchor) / (30.0 * 86400))
        factors.append({
            "name": "stalled_progress", "weight": _W_STALLED,
            "value": stalled,
            "detail": (f"no completion in {(now-anchor)//86400}d"
                       if anchor else "no activity recorded"),
        })

        # 5. estimate uncertainty --------------------------------------------
        uncertain = 0.0 if wl["progress_source"] == "derived" and wl["progress"] is not None \
            else 1.0
        factors.append({
            "name": "estimate_uncertainty", "weight": _W_ESTIMATE,
            "value": uncertain,
            "detail": f"progress basis: {wl['progress_source']}",
        })

        score = 0.0
        for f in factors:
            if f["value"] is not None:
                score += float(f["weight"]) * float(f["value"])
        score = _clamp(score)
        if score < 0.34:
            level = "low"
        elif score < 0.67:
            level = "medium"
        else:
            level = "high"
        return {
            "project_id": int(project["id"]), "name": project["name"],
            "score": round(score, 4), "level": level,
            "factors": factors,
            "methodology": ("deterministic weighted sum of named factors; "
                            "no LLM involvement"),
        }

    def most_at_risk(self, *, now: int | None = None,
                     status: str = "active") -> dict[str, Any] | None:
        best = None
        for p in self.db.projects(status):
            r = self.risk(p, now=now)
            if r is None:
                continue
            if best is None or r["score"] > best["score"]:
                best = r
        return best

    # --------------------------------------------------------- dependencies
    def blocked_task_ids(self, project_id: int) -> list[int]:
        """Active tasks whose *explicit* prerequisites are not yet complete."""
        rows = self.db.project_tasks(project_id)
        active = {int(r["id"]): r for r in rows
                  if normalize_status(r["status"] or "todo")
                  not in _EXCLUDED + _DONE}
        if not active:
            return []
        deps = self.db.dependencies_for_tasks(list(active))
        blocked = set()
        for d in deps:
            if int(d["inferred"] or 0):
                continue  # inferred edges are advisory, never blocking
            dep = self.db.task_by_id(int(d["depends_on"]))
            if dep is None:
                continue
            if normalize_status(dep["status"] or "todo") not in _DONE:
                blocked.add(int(d["task_id"]))
        return sorted(blocked)

    def dependencies(self, ident: Any) -> dict[str, Any] | None:
        project = self._resolve(ident)
        if project is None:
            return None
        pid = int(project["id"])
        rows = self.db.project_tasks(pid)
        ids = {int(r["id"]) for r in rows}
        edges = []
        for d in self.db.dependencies_for_tasks(list(ids)):
            tid, dep = int(d["task_id"]), int(d["depends_on"])
            if tid not in ids:
                continue
            edges.append({
                "task_id": tid, "depends_on": dep,
                "inferred": bool(int(d["inferred"] or 0)),
                "satisfied": self._is_done(dep),
            })
        blocking = []
        for tid in self.blocked_task_ids(pid):
            row = self.db.task_by_id(tid)
            deps = [int(d["depends_on"])
                    for d in self.db.task_dependencies(tid)
                    if not int(d["inferred"] or 0)]
            blocking.append({
                "task_id": tid,
                "title": row["title"] if row else "",
                "waiting_on": deps,
            })
        return {
            "project_id": pid, "name": project["name"],
            "nodes": [{"task_id": int(r["id"]), "title": r["title"],
                       "status": normalize_status(r["status"] or "todo")}
                      for r in rows],
            "edges": edges,
            "blocked": blocking,
            "cycles": self.find_cycles(pid),
        }

    def _is_done(self, task_id: int) -> bool:
        row = self.db.task_by_id(task_id)
        return bool(row and normalize_status(row["status"] or "todo") in _DONE)

    def find_cycles(self, project_id: int | None = None) -> list[list[int]]:
        """Return dependency cycles (there should never be one)."""
        graph: dict[int, list[int]] = {}
        for d in self.db.all_task_dependencies():
            graph.setdefault(int(d["task_id"]), []).append(int(d["depends_on"]))
        if project_id is not None:
            ids = {int(r["id"]) for r in self.db.project_tasks(project_id)}
            graph = {k: [v for v in vs if v in ids]
                     for k, vs in graph.items() if k in ids}
        cycles: list[list[int]] = []
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[int, int] = {}
        stack: list[int] = []

        def visit(node: int) -> None:
            color[node] = GRAY
            stack.append(node)
            for nxt in graph.get(node, []):
                c = color.get(nxt, WHITE)
                if c == GRAY:
                    if nxt in stack:
                        cycles.append(stack[stack.index(nxt):] + [nxt])
                elif c == WHITE:
                    visit(nxt)
            stack.pop()
            color[node] = BLACK

        for node in list(graph):
            if color.get(node, WHITE) == WHITE:
                visit(node)
        return cycles

    def add_dependency(self, task_id: int, depends_on: int,
                       inferred: bool = False) -> dict[str, Any]:
        if int(task_id) == int(depends_on):
            return {"ok": False, "error": "a task cannot depend on itself"}
        if self.db.task_by_id(int(task_id)) is None:
            return {"ok": False, "error": f"task {task_id} not found"}
        if self.db.task_by_id(int(depends_on)) is None:
            return {"ok": False, "error": f"task {depends_on} not found"}
        self.db.add_task_dependency(int(task_id), int(depends_on), inferred=inferred)
        cycles = self.find_cycles()
        if cycles:
            self.db.remove_task_dependency(int(task_id), int(depends_on))
            return {"ok": False, "error": "dependency would create a cycle",
                    "cycles": cycles}
        return {"ok": True, "task_id": int(task_id),
                "depends_on": int(depends_on), "inferred": bool(inferred)}

    def remove_dependency(self, task_id: int, depends_on: int) -> dict[str, Any]:
        self.db.remove_task_dependency(int(task_id), int(depends_on))
        return {"ok": True, "task_id": int(task_id), "depends_on": int(depends_on)}

    # ---------------------------------------------------------------- writes
    def create_project(self, name: str, **fields: Any) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "project name is required"}
        provenance = fields.pop("provenance", None) or {}
        allowed = {"objective", "status", "priority", "course_id", "deadline",
                   "estimated_total_minutes", "remaining_minutes", "repo_url",
                   "links", "refs"}
        kwargs = {k: v for k, v in fields.items() if k in allowed}
        if kwargs.get("status") not in PROJECT_STATUSES and "status" in kwargs:
            return {"ok": False, "error": f"invalid status: {kwargs['status']}"}
        pid = self.db.add_project(name, provenance=provenance, **kwargs)
        milestones = fields.get("milestones") or []
        created_ms = []
        for i, m in enumerate(milestones):
            if isinstance(m, str):
                m = {"name": m}
            res = self.add_milestone(
                pid, m.get("name") or f"Milestone {i+1}",
                description=m.get("description", ""),
                order_index=int(m.get("order_index", i)),
                estimated_minutes=int(m.get("estimated_minutes", 0) or 0),
                deadline=int(m.get("deadline", 0) or 0))
            if res.get("ok"):
                created_ms.append(res["milestone"])
        return {"ok": True, "project": self.get_project(pid),
                "milestones": created_ms}

    def update_project(self, ident: Any, **fields: Any) -> dict[str, Any]:
        project = self._resolve(ident)
        if project is None:
            return {"ok": False, "error": "project not found"}
        allowed = {"name", "objective", "status", "priority", "course_id",
                   "deadline", "estimated_total_minutes", "remaining_minutes",
                   "repo_url", "links", "refs", "provenance"}
        kwargs = {k: v for k, v in fields.items() if k in allowed}
        if "status" in kwargs and kwargs["status"] not in PROJECT_STATUSES:
            return {"ok": False, "error": f"invalid status: {kwargs['status']}"}
        if kwargs:
            self.db.update_project(int(project["id"]), **kwargs)
        return {"ok": True, "project": self.get_project(int(project["id"]))}

    def add_milestone(self, project_id: int, name: str, **fields: Any) -> dict[str, Any]:
        if self.db.project_by_id(int(project_id)) is None:
            return {"ok": False, "error": "project not found"}
        mid = self.db.add_milestone(int(project_id), (name or "").strip(),
                                    **{k: v for k, v in fields.items()
                                       if k in {"description", "order_index",
                                                "status", "deadline",
                                                "estimated_minutes",
                                                "remaining_minutes"}})
        return {"ok": True, "milestone": dict(self.db.milestone_by_id(mid) or {})}

    def link_task(self, task_id: int, project_id: int,
                  milestone_id: int = 0) -> dict[str, Any]:
        if self.db.task_by_id(int(task_id)) is None:
            return {"ok": False, "error": "task not found"}
        if self.db.project_by_id(int(project_id)) is None:
            return {"ok": False, "error": "project not found"}
        if milestone_id and self.db.milestone_by_id(int(milestone_id)) is None:
            return {"ok": False, "error": "milestone not found"}
        self.db.link_task(int(task_id), int(project_id), int(milestone_id))
        return {"ok": True, "task_id": int(task_id),
                "project_id": int(project_id), "milestone_id": int(milestone_id)}

    # ----------------------------------------------------------------- reads
    def get_project(self, ident: Any, *, now: int | None = None) -> dict[str, Any] | None:
        project = self._resolve(ident)
        if project is None:
            return None
        pid = int(project["id"])
        wl = self.workload(pid)
        rk = self.risk(pid, now=now)
        return {
            "id": pid, "name": project["name"],
            "objective": project["objective"],
            "status": project["status"], "priority": int(project["priority"] or 3),
            "course_id": int(project["course_id"] or 0),
            "deadline": int(project["deadline"] or 0),
            "estimated_total_minutes": int(project["estimated_total_minutes"] or 0),
            "remaining_minutes": (wl or {}).get("remaining_minutes", 0),
            "progress": (wl or {}).get("progress"),
            "progress_source": (wl or {}).get("progress_source", "unknown"),
            "risk": (rk or {}).get("score"),
            "risk_level": (rk or {}).get("level"),
            "repo_url": project["repo_url"],
            "links": _loads(project["links"], []),
            "refs": _loads(project["refs"], []),
            "provenance": _loads(project["provenance"], {}),
            "created_at": int(project["created_at"] or 0),
            "updated_at": int(project["updated_at"] or 0),
            "milestones": (wl or {}).get("milestones", []),
        }

    def list_projects(self, status: str = "", *, now: int | None = None) -> list[dict[str, Any]]:
        out = []
        for p in self.db.projects(status):
            got = self.get_project(int(p["id"]), now=now)
            if got is not None:
                out.append(got)
        return out

    # ------------------------------------------------------------- proposal
    def propose_project(self, text: str, *, now: int | None = None) -> dict[str, Any]:
        """Turn a natural-language description into a *proposal* (no DB write).

        Deterministic: name, deadline and effort are parsed from the text and
        every field is marked ``inferred`` so confirmation is required before
        anything is persisted.
        """
        text = (text or "").strip()
        now = int(now if now is not None else self._now())
        lower = text.lower()

        # effort -------------------------------------------------------------
        est = 0
        m = _HOURS_RE.search(lower)
        if m:
            est = int(round(float(m.group(1)) * 60))

        # deadline -----------------------------------------------------------
        deadline, phrase = self._parse_deadline(lower, now)

        # name ---------------------------------------------------------------
        name = self._parse_name(text)

        # milestones: trailing comma/slash separated clause after the effort
        milestones: list[dict[str, Any]] = []
        tail = lower
        if m:
            tail = lower[m.end():]
        tail = re.sub(r"^(?:,|\band\b|\bwith\b|\s)+", "", tail).strip(" .,")
        if tail:
            parts = [p.strip(" .") for p in re.split(r"[,/;]|\band\b", tail)
                     if p.strip(" .")]
            parts = [p for p in parts if len(p.split()) <= 6]
            if 1 <= len(parts) <= 8:
                share = (est // len(parts)) if est and len(parts) else 0
                milestones = [{"name": p, "estimated_minutes": share}
                              for p in parts]

        provenance = {k: "inferred" for k in
                      ("name", "deadline", "estimated_total_minutes", "milestones")}
        questions = []
        if not name:
            questions.append("What is the project called?")
        if not deadline:
            questions.append("When is it due?")
        if not est:
            questions.append("Roughly how many hours will it take?")
        confidence = 0.9
        if questions:
            confidence = 0.5
        return {
            "kind": "project_proposal",
            "name": name or "",
            "objective": text,
            "deadline": deadline,
            "deadline_phrase": phrase,
            "estimated_total_minutes": est,
            "milestones": milestones,
            "provenance": provenance,
            "confidence": confidence,
            "questions": questions,
            "requires_confirmation": True,
        }

    def _parse_name(self, text: str) -> str:
        t = re.sub(r"^(?:i\s+have|i've\s+got|i\s+got|add|create|new|start)\s+"
                   r"(?:a|an|the|my)?\s*", "", text.strip(), flags=re.I)
        # cut at the first due/that/by/deadline marker or comma
        cut = re.split(r"\s+(?:due|that|which|by|deadline)\b|,", t, maxsplit=1,
                       flags=re.I)[0]
        cut = cut.strip(" .,-")
        if len(cut.split()) > 8:
            cut = " ".join(cut.split()[:8])
        return cut

    def _parse_deadline(self, lower: str, now: int) -> tuple[int, str]:
        iso = _ISO_RE.search(lower)
        if iso:
            try:
                dt = datetime(int(iso.group(1)), int(iso.group(2)),
                              int(iso.group(3)), 23, 59)
                return int(dt.timestamp()), iso.group(0)
            except ValueError:
                pass
        m = re.search(r"\bin\s+(\d+)\s+(day|week)s?\b", lower)
        if m:
            n = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
            base = self._midnight(now)
            return int((datetime.fromtimestamp(base)
                        + timedelta(days=n)).timestamp()) + 86340, m.group(0)
        if "tomorrow" in lower:
            base = self._midnight(now)
            return int((datetime.fromtimestamp(base)
                        + timedelta(days=1)).timestamp()) + 86340, "tomorrow"
        if "tonight" in lower or "today" in lower:
            base = self._midnight(now)
            return base + 86340, "today"
        for i, wd in enumerate(_WEEKDAYS):
            if re.search(rf"\b(?:next\s+|this\s+|by\s+|on\s+|due\s+)?{wd}\b",
                         lower):
                base = self._midnight(now)
                today = datetime.fromtimestamp(base).weekday()
                delta = (i - today) % 7
                if delta == 0:
                    delta = 7
                if "next" in lower and delta < 7:
                    pass
                return int((datetime.fromtimestamp(base)
                            + timedelta(days=delta)).timestamp()) + 86340, wd
        return 0, ""


def _dump_project(row: Any) -> dict[str, Any]:
    """Best-effort dict for an arbitrary project row (used by callers/tests)."""
    return {k: row[k] for k in row.keys()} if row is not None else {}
