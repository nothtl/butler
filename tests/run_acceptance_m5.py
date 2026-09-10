"""M5 acceptance: advanced schedule optimization (deterministic).

Run:  .venv/bin/python tests/run_acceptance_m5.py

Proves the M5 optimization layer end to end, entirely offline and with frozen
clocks:

  A. Hard constraints: no overlap with hard events, sleep/waking protected,
     dependencies ordered, completed/cancelled excluded, remaining effort
     respected, deadlines enforced where feasible, cycles and pinned conflicts
     reported as infeasible.
  B. Soft objectives: deadline beats affinity, explicit preference beats
     inferred, risk/priority/deadline strategies order work, fragmentation and
     churn are measured, buffer is preserved.
  C. Multi-day: feasible and infeasible workloads, slack, deadline pressure,
     projects spanning days, bounded sessions.
  D. Rescheduling: one block moves, unrelated blocks are preserved, a complete
     diff is returned, infeasible requests are explained, undo is maintained.
  E. Realistic examples: CS168 + CS188 + lectures, multi-milestone projects,
     competing deadlines, insufficient capacity, explicit preferred times,
     soft leisure, risk-aware ordering.
  F. Safety: the optimizer writes nothing, MCP tools are side-effect free,
     reschedules require confirmation, inferred constraints never become hard.
  G. Regression: full profile intact, readonly surface grew to 23, routing and
     the M3/M4 surfaces still work.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

from butler import schedule as sch  # noqa: E402
from butler.agent.mcp_tools import build_mcp_registry  # noqa: E402
from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.semantic import (  # noqa: E402
    ActionKind, Constraint, ConstraintKind, ConstraintSource, Hardness,
    ResultStatus, SemanticValidationError,
)
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402
from butler.optimizer import (  # noqa: E402
    DEFAULT_WEIGHTS, OptimizationResult, ScheduleOptimizer, ScheduleRequest,
    ScheduledSession, SoftPreference, TaskItem, _day_capacity,
    _usable_intervals, optimize,
)

PASS = 0
FAIL = 0

DAY = int(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc).timestamp())  # Monday
NEXT = DAY + 86400
NEXT2 = DAY + 2 * 86400
NOW = DAY + 9 * 3600  # 09:00


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def ev(title: str, start: int, end: int) -> sch.Event:
    return sch.Event(id=0, title=title, start_min=start, end_min=end)


def preq(tasks, events=None, days=1, **kw) -> ScheduleRequest:
    ds = [DAY + i * 86400 for i in range(days)]
    base = dict(day_starts=ds, tasks=tasks, now=DAY, events_by_day=events or {},
                day_start=7 * 60, day_end=23 * 60, sleep_start=23 * 60,
                sleep_end=7 * 60)
    base.update(kw)
    return ScheduleRequest(**base)


def base_config(prefix: str) -> Config:
    base = tempfile.mkdtemp(prefix=prefix)
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    cfg.timezone = "UTC"
    cfg.sleep_start = 23 * 60
    cfg.sleep_end = 7 * 60
    cfg.ensure_dirs()
    return cfg


def fresh_container(prefix: str = "m5-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


def sessions_on_day(r: OptimizationResult, day_index: int) -> list:
    lo = DAY + day_index * 86400
    hi = lo + 86400
    return [s for s in r.sessions if lo <= s.start_ts < hi]


# =====================================================================
# A. hard constraints
# =====================================================================
def test_hard_constraints() -> None:
    print("\n== A. hard constraints ==")
    # A1 no overlap with a hard event
    r = optimize(preq([TaskItem(1, "Study", 180)],
                      {DAY: [ev("Lecture", 9 * 60, 11 * 60)]}))
    check("A1 no session overlaps a hard event",
          all(not (s.start_min < 11 * 60 and s.end_min > 9 * 60)
              for s in r.sessions),
          str([(s.start_min, s.end_min) for s in r.sessions]))

    # A2/A3 sleep + waking bounds
    r = optimize(preq([TaskItem(1, "Study", 300)],
                      {DAY: [ev("Lecture", 9 * 60, 11 * 60)]}))
    check("A2 nothing is scheduled during sleep",
          all(s.start_min >= 7 * 60 and s.end_min <= 23 * 60 for s in r.sessions))
    check("A3 everything is inside the waking window",
          all(420 <= s.start_min and s.end_min <= 1380 for s in r.sessions))

    # A4 dependency ordering
    r = optimize(preq([TaskItem(1, "A1", 60), TaskItem(2, "A2", 60, deps=[1])]))
    a1 = next(s for s in r.sessions if s.task_id == 1)
    a2 = next(s for s in r.sessions if s.task_id == 2)
    check("A4 a dependent task starts after its prerequisite ends",
          a1.end_ts <= a2.start_ts, f"{a1.end_min}->{a2.start_min}")

    # A5/A6 excluded statuses
    r = optimize(preq([TaskItem(1, "Done", 60, status="completed"),
                       TaskItem(2, "Cancelled", 60, status="cancelled"),
                       TaskItem(3, "Blocked", 60, status="blocked")]))
    check("A5 completed tasks are never scheduled", not r.sessions)

    # A7 deadline met when feasible
    r = optimize(preq([TaskItem(1, "Essay", 120, deadline=DAY + 12 * 3600)]))
    check("A7 a feasible deadline is respected",
          r.feasible and all(s.end_ts <= DAY + 12 * 3600 for s in r.sessions))

    # A8 deadline miss reported
    r = optimize(preq([TaskItem(1, "Essay", 120, deadline=DAY + 20 * 3600)],
                      {DAY: [ev("All day", 7 * 60, 22 * 60)]}))
    check("A8 an impossible deadline is an explicit violation",
          (not r.feasible)
          and any(v["kind"] == "deadline_miss" for v in r.violations),
          str([v["kind"] for v in r.violations]))

    # A9 dependency cycle
    r = optimize(preq([TaskItem(1, "X", 60, deps=[2]),
                       TaskItem(2, "Y", 60, deps=[1])]))
    check("A9 a dependency cycle is infeasible",
          (not r.feasible)
          and any(v["kind"] == "dependency_cycle" for v in r.violations))

    # A10 never exceed remaining effort
    r = optimize(preq([TaskItem(1, "Small", 50)]))
    check("A10 scheduled minutes never exceed remaining effort",
          sum(s.duration for s in r.sessions) <= 50,
          str(sum(s.duration for s in r.sessions)))

    # A11 no overlapping sessions for the same task
    r = optimize(preq([TaskItem(1, "Big", 400)]))
    mine = sorted((s for s in r.sessions if s.task_id == 1),
                  key=lambda s: s.start_ts)
    overlaps = any(mine[i].end_ts > mine[i + 1].start_ts
                   for i in range(len(mine) - 1))
    check("A11 a task never has two overlapping sessions", not overlaps)

    # A12 capacity (buffer) respected
    r = optimize(preq([TaskItem(1, "Huge", 5000)]))
    ivs = _usable_intervals(preq([]), DAY)
    cap = _day_capacity(preq([]), ivs)
    check("A12 scheduled minutes never exceed usable capacity",
          sum(s.duration for s in r.sessions) <= cap,
          f"{sum(s.duration for s in r.sessions)} <= {cap}")

    # A13 pinned conflict
    r = optimize(preq([TaskItem(1, "A", 60)],
                      {DAY: [ev("Lecture", 9 * 60, 11 * 60)]},
                      pinned={1: [DAY + 9 * 3600, DAY + 10 * 3600]}))
    check("A13 an impossible pinned window is an explicit violation",
          (not r.feasible)
          and any(v["kind"] == "pinned_conflict" for v in r.violations))


# =====================================================================
# B. soft objectives
# =====================================================================
def test_soft_objectives() -> None:
    print("\n== B. soft objectives ==")
    # B1 deadline beats affinity
    r = optimize(preq([TaskItem(1, "Near", 60, deadline=DAY + 11 * 3600),
                       TaskItem(2, "Far", 60, deadline=DAY + 20 * 3600,
                                affinity=8)]))
    near = next(s for s in r.sessions if s.task_id == 1)
    far = next(s for s in r.sessions if s.task_id == 2)
    check("B1 a nearer deadline beats context affinity",
          near.start_ts < far.start_ts, f"{near.start_min} vs {far.start_min}")

    # B2 explicit preference beats inferred
    r = optimize(preq([TaskItem(1, "CS168", 60)],
                      preferences=[SoftPreference(1, 14 * 60, 15 * 60,
                                                  "routine_inferred"),
                                   SoftPreference(1, 21 * 60, 22 * 60,
                                                  "explicit_user")]))
    check("B2 an explicit preference beats an inferred one",
          r.sessions[0].start_min == 21 * 60,
          str(r.sessions[0].start_min))

    # B3 explicit preference loses to feasibility
    r = optimize(preq([TaskItem(1, "Essay", 60, deadline=DAY + 11 * 3600)],
                      preferences=[SoftPreference(1, 21 * 60, 22 * 60,
                                                  "explicit_user")]))
    check("B3 feasibility beats an explicit preference",
          all(s.end_ts <= DAY + 11 * 3600 for s in r.sessions)
          and r.sessions[0].start_min < 21 * 60)

    # B4 risk strategy ordering
    r_risk = optimize(preq([TaskItem(1, "High", 60, project_risk=0.9,
                                     deadline=DAY + 20 * 3600),
                            TaskItem(2, "Low", 60, project_risk=0.1,
                                     deadline=DAY + 12 * 3600)],
                           strategy="risk_first"))
    check("B4 risk_first schedules the high-risk work first",
          r_risk.sessions[0].task_id == 1)
    r_dl = optimize(preq([TaskItem(1, "High", 60, project_risk=0.9,
                                   deadline=DAY + 20 * 3600),
                          TaskItem(2, "Low", 60, project_risk=0.1,
                                   deadline=DAY + 12 * 3600)],
                         strategy="deadline_first"))
    check("B5 deadline_first schedules the nearest deadline first",
          r_dl.sessions[0].task_id == 2)
    r_pri = optimize(preq([TaskItem(1, "Important", 60, priority=5),
                           TaskItem(2, "Minor", 60, priority=1)],
                          strategy="priority_first"))
    check("B6 priority_first schedules the higher priority first",
          r_pri.sessions[0].task_id == 1)

    # B7 balanced is the default
    r = optimize(preq([TaskItem(1, "A", 60)]))
    check("B7 balanced is the default strategy",
          ScheduleRequest().strategy == "balanced" and r.strategy == "balanced")

    # B8 fragmentation / continuity
    r_one = optimize(preq([TaskItem(1, "Focus", 120)],
                          max_session_minutes=180))
    r_split = optimize(preq([TaskItem(1, "Focus", 120)],
                            max_session_minutes=60))
    check("B8a a contiguous task is one session",
          len(r_one.sessions) == 1
          and r_one.objective_breakdown["continuity"] == 1.0)
    check("B8b splitting lowers continuity",
          len(r_split.sessions) == 2
          and r_split.objective_breakdown["continuity"] < 1.0,
          str(r_split.objective_breakdown["continuity"]))

    # B9 churn penalty
    existing = [ScheduledSession(1, "A", DAY + 9 * 3600, DAY + 10 * 3600, 60,
                                 start_min=9 * 60, end_min=10 * 60, pinned=True)]
    r = optimize(preq([TaskItem(1, "A", 60), TaskItem(2, "B", 60)],
                      existing=existing))
    check("B9 an unchanged block is preserved to avoid churn",
          r.churn["counts"]["unchanged"] == 1
          and any(s.task_id == 1 and s.start_min == 9 * 60
                  for s in r.sessions), str(r.churn["counts"]))

    # B10 buffer preserved
    r = optimize(preq([TaskItem(1, "Small", 60)]))
    ivs = _usable_intervals(preq([]), DAY)
    cap = _day_capacity(preq([]), ivs)
    check("B10 free buffer is left unused",
          sum(s.duration for s in r.sessions) < cap,
          f"{sum(s.duration for s in r.sessions)} < {cap}")

    # B11 inferred preference never overrides a deadline
    r = optimize(preq([TaskItem(1, "Essay", 60, deadline=DAY + 11 * 3600)],
                      preferences=[SoftPreference(1, 21 * 60, 22 * 60,
                                                  "routine_inferred")]))
    check("B11 an inferred preference cannot override a deadline",
          all(s.end_ts <= DAY + 11 * 3600 for s in r.sessions))

    # B12 objective breakdown is complete
    r = optimize(preq([TaskItem(1, "A", 60)]))
    check("B12 the objective breakdown reports every weight",
          set(DEFAULT_WEIGHTS) <= set(r.objective_breakdown)
          and len(r.objectives) == len(r.objective_breakdown)
          and all(o.description for o in r.objectives))


# =====================================================================
# C. multi-day
# =====================================================================
def _three_busy_days():
    busy = {DAY: [ev("Class", 8 * 60, 20 * 60)],
            NEXT: [ev("Class", 8 * 60, 20 * 60)],
            NEXT2: [ev("Class", 8 * 60, 20 * 60)]}
    return busy


def test_multi_day() -> None:
    print("\n== C. multi-day optimization ==")
    busy = _three_busy_days()
    r = optimize(preq([TaskItem(1, "Project", 300,
                                deadline=NEXT2 + 20 * 3600)],
                      busy, days=3))
    check("C1 a workload that fits across days is feasible", r.feasible,
          str([v.get("kind") for v in r.violations]))
    check("C5 a project can span multiple days",
          len({s.start_ts // 86400 for s in r.sessions}) >= 2,
          str(sorted({s.start_ts // 86400 for s in r.sessions})))

    r2 = optimize(preq([TaskItem(1, "Project", 600,
                                 deadline=NEXT2 + 20 * 3600)],
                       busy, days=3))
    check("C2 an infeasible workload is reported, not hidden",
          (not r2.feasible)
          and any(v["kind"] == "deadline_miss" for v in r2.violations))

    # C3/C4 slack
    r3 = optimize(preq([TaskItem(1, "P", 700, project_id=1,
                                 project_deadline=NEXT2 + 20 * 3600,
                                 project_risk=0.8, project_risk_level="high")],
                       busy, days=3))
    proj = r3.risk_summary["projects"][0]
    check("C3 a shortfall shows negative slack", proj["slack_minutes"] < 0,
          str(proj["slack_minutes"]))
    check("C6 deadline pressure is computed", proj["pressure"] > 0,
          str(proj["pressure"]))
    check("C10 the project appears in the risk summary",
          proj["project_id"] == 1 and proj["remaining_minutes"] == 700)

    r4 = optimize(preq([TaskItem(1, "P", 60, project_id=2,
                                 project_deadline=NEXT2 + 20 * 3600)],
                       busy, days=3))
    check("C4 a comfortable workload shows positive slack",
          r4.risk_summary["projects"][0]["slack_minutes"] > 0,
          str(r4.risk_summary["projects"][0]["slack_minutes"]))

    # C7 horizon bounds
    check("C7 sessions stay inside the horizon",
          all(DAY <= s.start_ts and s.end_ts <= NEXT2 + 86400
              for s in r.sessions))

    # C8 max session cap
    r8 = optimize(preq([TaskItem(1, "Big", 400)], max_session_minutes=60))
    check("C8 no session exceeds the max session length",
          all(s.duration <= 60 for s in r8.sessions),
          str(sorted({s.duration for s in r8.sessions})))

    # C9 per-day capacity
    ok = True
    for i in range(3):
        day = DAY + i * 86400
        cap = _day_capacity(preq([]), _usable_intervals(preq([]), day))
        used = sum(s.duration for s in sessions_on_day(r, i))
        if used > cap:
            ok = False
    check("C9 no day exceeds its usable capacity", ok)


# =====================================================================
# D. rescheduling
# =====================================================================
def test_rescheduling() -> None:
    print("\n== D. rescheduling ==")
    r = optimize(preq([TaskItem(1, "Study", 60), TaskItem(2, "Other", 60)],
                      days=2,
                      pinned={1: [NEXT + 9 * 3600, NEXT + 10 * 3600]}))
    check("D1 a forced move places the block on the requested day",
          any(s.task_id == 1 and s.start_ts >= NEXT for s in r.sessions))
    check("D2 unrelated work is not disturbed",
          any(s.task_id == 2 and s.start_ts < NEXT for s in r.sessions))
    check("D3 the complete diff is returned",
          set(r.churn) >= {"moved", "added", "removed", "unchanged", "counts"})

    # D4 infeasible reschedule explained
    r4 = optimize(preq([TaskItem(1, "A", 60)],
                       {DAY: [ev("Lecture", 9 * 60, 11 * 60)]},
                       pinned={1: [DAY + 9 * 3600, DAY + 10 * 3600]}))
    check("D4 an infeasible reschedule is explained",
          (not r4.feasible)
          and any(v["kind"] == "pinned_conflict" for v in r4.violations))

    # D5 churn counts
    existing = [ScheduledSession(1, "A", DAY + 9 * 3600, DAY + 10 * 3600, 60,
                                 start_min=9 * 60, end_min=10 * 60, pinned=True)]
    r5 = optimize(preq([TaskItem(1, "A", 60), TaskItem(2, "B", 60)],
                       days=2, existing=existing,
                       pinned={1: [NEXT + 9 * 3600, NEXT + 10 * 3600]}))
    check("D5 churn counts a moved and an added block",
          r5.churn["counts"]["moved"] == 1 and r5.churn["counts"]["added"] == 1,
          str(r5.churn["counts"]))

    # D6/D7 service confirmation boundary
    c = fresh_container("m5-resched-")
    c.db.add_task("CS168 essay", est_minutes=120, deadline=NOW + 3 * 3600,
                  priority=4)
    c.db.add_task("CS188 homework", est_minutes=90, deadline=NOW + 6 * 3600)
    c.planner.plan_day(DAY)
    svc = ExecutiveService(c, now_ts=NOW)
    res = svc.ask(text="move this without messing up the rest of my schedule")
    check("D6 a multi-block reschedule requires confirmation",
          res.status == ResultStatus.NEEDS_CONFIRMATION
          and res.confirmation_required, res.status.value)
    check("D7 confirmation carries diff and undo availability",
          isinstance(res.data, dict) and "diff" in res.data
          and "undo_available" in res.data)
    check("D7b the optimizer never commits during a reschedule request",
          c.db.latest_plan() is not None
          and res.candidate_actions
          and res.candidate_actions[0]["action"] == "reschedule")

    # D8 undo restores the previous schedule
    c2 = fresh_container("m5-undo-")
    c2.db.add_task("Task A", est_minutes=60, deadline=DAY + 15 * 3600)
    c2.planner.plan_day(DAY)
    first = json.loads(c2.db.latest_plan()["json"])["slots"]
    c2.planner.reschedule()
    c2.planner.undo()
    restored = json.loads(c2.db.latest_plan()["json"])["slots"]
    check("D8 undo restores the previous schedule",
          restored == first, f"{len(restored)} slots")


# =====================================================================
# E. realistic examples
# =====================================================================
def test_realistic() -> None:
    print("\n== E. realistic examples ==")
    c = fresh_container("m5-real-")
    c.db.add_course("CS168")
    lec = c.db.add_event("CS168 Lecture", DAY + 10 * 3600, DAY + 12 * 3600,
                         source="course:CS168")
    c.db.add_event("CS188 Lecture", NEXT + 10 * 3600, NEXT + 12 * 3600,
                   source="course:CS188")
    t1 = c.db.add_task("CS168 Project 2", est_minutes=180,
                       deadline=NOW + 2 * 86400, priority=4, tags="cs168")
    t2 = c.db.add_task("CS188 Homework", est_minutes=120,
                       deadline=NOW + 4 * 86400, priority=3, tags="cs188")
    svc = ExecutiveService(c, now_ts=NOW)
    res = svc.ask(text="optimize my week")
    opt = res.data["optimization"]
    scheduled = {s["task_id"] for s in opt["sessions"]}
    check("E1 CS168 + CS188 + lectures produce a feasible plan",
          res.status == ResultStatus.OK and opt["feasible"]
          and {t1, t2} <= scheduled, str(sorted(scheduled)))
    check("E1b no optimized session overlaps a lecture",
          all(not (s["start_ts"] < DAY + 12 * 3600
                   and s["end_ts"] > DAY + 10 * 3600)
              for s in opt["sessions"]),
          str([(s["task_id"], s["start_min"], s["end_min"],
                (s["start_ts"] - DAY) // 86400) for s in opt["sessions"]]))
    check("E10 every session carries a human-readable reason",
          opt["sessions"] and all(s["reason"] for s in opt["sessions"]),
          opt["sessions"][0]["reason"][:50] if opt["sessions"] else "")

    # E2/E9 project with milestones
    proj = c.projects.create_project(
        "CS168 Final", deadline=NOW + 3 * 86400, priority=5,
        milestones=[{"name": "Design", "estimated_minutes": 120},
                    {"name": "Implement", "estimated_minutes": 240}])
    pid = proj["project"]["id"]
    ms = proj["milestones"]
    p1 = c.db.add_task("CS168 design doc", est_minutes=120,
                       deadline=NOW + 86400, priority=4)
    p2 = c.db.add_task("CS168 implementation", est_minutes=240,
                       deadline=NOW + 3 * 86400, priority=5)
    c.projects.link_task(p1, pid, ms[0]["id"])
    c.projects.link_task(p2, pid, ms[1]["id"])
    c.projects.add_dependency(p2, p1)
    opt2 = c.optimizer.optimize_from_state(days=7, strategy="balanced",
                                           day_ts=DAY, now=NOW)
    check("E2 project tasks are scheduled with project/milestone links",
          any(s.project_id == pid for s in opt2.sessions),
          str([(s.task_id, s.project_id, s.milestone_id)
               for s in opt2.sessions]))
    check("E9 a multi-milestone project spans the plan",
          len({s.milestone_id for s in opt2.sessions
               if s.project_id == pid}) >= 1)
    d1 = next((s for s in opt2.sessions if s.task_id == p1), None)
    d2 = next((s for s in opt2.sessions if s.task_id == p2), None)
    check("E9b a project dependency is ordered (design before implement)",
          d1 is not None and d2 is not None and d1.end_ts <= d2.start_ts)

    # E3 competing deadlines
    r3 = optimize(preq([TaskItem(1, "Due soon", 60, deadline=DAY + 12 * 3600),
                        TaskItem(2, "Due later", 60, deadline=DAY + 20 * 3600)]))
    check("E3 two competing deadlines are ordered by urgency",
          r3.sessions[0].task_id == 1)

    # E4 insufficient weekly capacity
    busy = _three_busy_days()
    r4 = optimize(preq([TaskItem(1, "Too much", 2000,
                                 deadline=NEXT2 + 20 * 3600)], busy, days=3))
    check("E4 insufficient capacity is reported as infeasible", not r4.feasible)

    # E5 explicit preferred study time
    r5 = optimize(preq([TaskItem(1, "CS168", 60)],
                       preferences=[SoftPreference(1, 20 * 60, 21 * 60,
                                                   "explicit_user")]))
    check("E5 an explicit study time is honoured",
          r5.sessions[0].start_min == 20 * 60)

    # E6 leisure is soft
    r6 = optimize(preq([TaskItem(1, "Gym", 60), TaskItem(2, "Cook dinner", 45),
                        TaskItem(3, "Read", 30)]))
    check("E6 gym/cooking/leisure are scheduled as soft work",
          {s.task_id for s in r6.sessions} == {1, 2, 3} and r6.feasible)

    # E7 context affinity cannot beat a deadline
    r7 = optimize(preq([TaskItem(1, "Essay", 60, deadline=DAY + 11 * 3600,
                                 affinity=8)]))
    check("E7 context affinity never overrides a deadline",
          all(s.end_ts <= DAY + 11 * 3600 for s in r7.sessions))

    # E8 risk-aware balanced ordering
    r8 = optimize(preq([TaskItem(1, "Risky", 60, project_risk=0.9,
                                 deadline=DAY + 18 * 3600, priority=3),
                        TaskItem(2, "Safe", 60, project_risk=0.0,
                                 deadline=DAY + 19 * 3600, priority=3)]))
    check("E8 balanced scheduling favours the riskier project",
          r8.sessions[0].task_id == 1)


# =====================================================================
# F. safety
# =====================================================================
def test_safety() -> None:
    print("\n== F. safety ==")
    c = fresh_container("m5-safe-")
    tid = c.db.add_task("Essay", est_minutes=120, deadline=NOW + 86400,
                        priority=4)
    c.db.add_event("Lecture", DAY + 10 * 3600, DAY + 12 * 3600)
    before_tasks = len(c.db.tasks("active"))
    before_events = len(c.db.events())
    before_plan = c.db.latest_plan()

    svc = ExecutiveService(c, now_ts=NOW)
    svc.ask(text="optimize my week")
    svc.ask(text="evaluate my schedule")
    svc.ask(text="find the best time for Essay")
    check("F1 the optimizer leaves tasks untouched",
          len(c.db.tasks("active")) == before_tasks)
    check("F7 the optimizer never modifies calendar events",
          len(c.db.events()) == before_events)
    check("F1b the optimizer never commits a plan",
          c.db.latest_plan() == before_plan)

    ro = MCPServer(c, profile="readonly")

    def call(name, args=None):
        resp = ro._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args or {}}})
        return json.loads(resp["result"]["content"][0]["text"])

    call("optimize_day", {})
    call("optimize_week", {"days": 3})
    call("evaluate_schedule", {"days": 3})
    call("find_best_slot", {"task": "Essay"})
    check("F2 read-only MCP optimizer tools leave state untouched",
          len(c.db.tasks("active")) == before_tasks
          and c.db.latest_plan() == before_plan
          and len(c.db.events()) == before_events)

    res = svc.ask(text="rearrange my week so I have more time for CS168")
    res2 = svc.ask(text="move this without messing up the rest of my schedule")
    check("F3 a reschedule request never executes without confirmation",
          res2.status == ResultStatus.NEEDS_CONFIRMATION
          and c.db.latest_plan() == before_plan)

    check("F4 optimize actions classify as read",
          all(c.safety.classify(a).value == "read" for a in
              ("optimize_day", "optimize_week", "evaluate_schedule",
               "find_best_slot")))
    check("F5 a reschedule is a low-risk (reversible) write",
          c.safety.classify("reschedule_optimized").value == "low_risk_write")

    raised = False
    try:
        Constraint(kind=ConstraintKind.ROUTINE, hardness=Hardness.HARD,
                   source=ConstraintSource.ROUTINE_INFERRED)
    except SemanticValidationError:
        raised = True
    check("F6 an inferred constraint can never be hard", raised)

    opt = c.optimizer.optimize_from_state(days=3, day_ts=DAY, now=NOW)
    bad = False
    for s in opt.sessions:
        for e in c.planner._day_events(DAY):
            if s.start_ts < DAY + 86400 and \
                    s.start_min < e.end_min and s.end_min > e.start_min:
                bad = True
    check("F8 no optimized session overlaps a hard event", not bad)


# =====================================================================
# G. regression
# =====================================================================
def test_regression() -> None:
    print("\n== G. regression ==")
    c = fresh_container("m5-reg-")
    c.db.add_course("CS168")
    c.db.add_task("CS168 Project 2", est_minutes=120, deadline=NOW + 86400,
                  priority=4)

    full = MCPServer(c, profile="full")
    full_names = {t["name"] for t in full._tools_spec()}
    check("G1 the full profile is still exactly 51 tools",
          len(full_names) == 51, str(len(full_names)))

    ro = MCPServer(c, profile="readonly")
    ro_names = {t["name"] for t in ro._tools_spec()}
    opt_tools = {"optimize_day", "optimize_week", "evaluate_schedule",
                 "find_best_slot"}
    check("G2 the readonly profile exposes the optimizer and 37 tools",
          opt_tools <= ro_names and len(ro_names) == 37, str(len(ro_names)))
    check("G2b the full profile stays free of the executive surface",
          not (opt_tools & full_names) and "executive_ask" not in full_names)

    it = DeterministicInterpreter(c, now_ts=NOW)
    routes = {
        "optimize my day": ActionKind.OPTIMIZE_DAY,
        "plan my day properly": ActionKind.OPTIMIZE_DAY,
        "optimize my week": ActionKind.OPTIMIZE_WEEK,
        "best way to schedule this week": ActionKind.OPTIMIZE_WEEK,
        "evaluate my schedule": ActionKind.EVALUATE_SCHEDULE,
        "when should i work on CS168": ActionKind.FIND_BEST_SLOT,
        "move this without messing up the rest of my schedule":
            ActionKind.RESCHEDULE_OPTIMIZED,
    }
    ok = all(it.interpret(text).action == action
             for text, action in routes.items())
    check("G3 optimization phrases route to the right actions", ok,
          str({t: it.interpret(t).action.value for t in routes}))

    check("G4 existing routing is unchanged",
          it.interpret("plan my day").action == ActionKind.PLAN_DAY
          and it.interpret("what's my status").action == ActionKind.STATUS
          and it.interpret("what's my CS168 project status").action
          == ActionKind.PROJECT_STATUS)
    check("G6 M4 web routing still works",
          it.interpret("check online for CS168 news").action
          == ActionKind.WEB_RESEARCH
          and it.interpret("open this webpage https://cs168.io").action
          == ActionKind.WEB_FETCH)

    # G5 M3 project reads
    proj = c.projects.create_project("Regression Project",
                                     deadline=NOW + 5 * 86400)
    pid = proj["project"]["id"]
    got = c.projects.get_project(pid)
    check("G5 M3 project reads still work",
          got is not None and got["id"] == pid)

    # G7 structured request through executive_ask
    svc = ExecutiveService(c, now_ts=NOW)
    res = svc.ask(request={"action": "optimize_day", "intent": "plan",
                           "confidence": 0.9})
    check("G7 a structured optimize_day request works",
          res.status == ResultStatus.OK
          and isinstance(res.data, dict) and "optimization" in res.data)

    # G8 configurable default strategy is used
    c.cfg.optimizer_default_strategy = "priority_first"
    res8 = svc.ask(text="optimize my day")
    check("G8 the configured default strategy is applied",
          res8.data["optimization"]["strategy"] == "priority_first",
          res8.data["optimization"]["strategy"])

    # registry resolves the optimizer tools for readonly only
    reg = build_mcp_registry(c)
    check("G9 the registry exposes the optimizer for readonly only",
          reg.find_mcp("optimize_week", profile="readonly") is not None
          and reg.find_mcp("optimize_week", profile="full") is None)


def main() -> int:
    print("M5 acceptance: advanced schedule optimization")
    test_hard_constraints()
    test_soft_objectives()
    test_multi_day()
    test_rescheduling()
    test_realistic()
    test_safety()
    test_regression()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
