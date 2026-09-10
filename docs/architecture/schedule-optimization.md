# Schedule Optimization (M5)

Status: implemented. This document describes the deterministic optimization
layer that turns Butler's scheduler from *"fit tasks into free gaps"* into
*"find the best feasible schedule for competing work, deadlines, priorities,
projects, dependencies and soft preferences."*

The module is `butler/optimizer.py` (`ScheduleOptimizer`), wired into
`Container` as `self.optimizer` and exposed through the executive service and
the read-only MCP surface.

## 1. Existing baseline solver

`butler/schedule.py` remains the **authority for hard geometry** and is not
modified. It is pure (no LLM, no I/O) and already guarantees:

* a task never overlaps a hard event (lecture / appointment);
* nothing is scheduled during sleep;
* the buffer is never fully consumed;
* completed tasks are excluded;
* partial availability (a task may use the part of a gap that fits);
* deterministic ordering and the existing `PlanState` / `Slot` / `Event` model.

`butler/planner.py` remains the only layer that talks to the DB and Google
Calendar; committing a plan, calendar projection and undo are unchanged.

M5 **wraps** that solver rather than replacing it: it reuses
`schedule.free_intervals` and `schedule.Event` verbatim for every day of the
horizon.

## 2. Optimization layer

```
user request
  → semantic interpretation (ActionKind.OPTIMIZE_* / RESCHEDULE_OPTIMIZED)
  → ExecutiveService
  → ScheduleOptimizer.optimize(ScheduleRequest)
  → OptimizationResult (feasible, sessions, score, violations, explanations,
                        risk_summary, churn)
  → confirmation if consequential
  → existing planner / calendar execution
```

`ScheduleOptimizer.optimize(request)` is pure over the request. The builder
`build_request(...)` reads live state (tasks, projects, dependencies, events)
into a self-contained `ScheduleRequest`. This split keeps the interface stable
so a future solver (OR-Tools CP-SAT, Timefold) can implement the same
`optimize(request) -> OptimizationResult` without touching the executive
service, semantic models, MCP surface, safety layer or calendar integration.

### Models

| Model | Purpose |
|-------|---------|
| `ScheduleRequest` | horizon (`day_starts`), tasks, events, sleep/window, buffer, strategy, weights, soft preferences, existing blocks, pinned windows, bounds |
| `TaskItem` | task id/title, remaining effort, priority, deadline, project/milestone, project risk, explicit deps, affinity |
| `SoftPreference` | a soft preferred window with a `source` (explicit vs inferred) |
| `ScheduledSession` | task id, project/milestone, start/end (absolute + minute), duration, reason, pinned, objective contributions |
| `ScheduleConstraint` | a named constraint and whether it holds |
| `ScheduleExplanation` | a human-readable reason per session |
| `OptimizationResult` | feasible, sessions, score, objective_breakdown, unscheduled_items, violations, explanations, risk_summary, churn |

## 3. Hard vs soft constraints

**Hard constraints are absolute and are never traded for a soft objective:**

* never overlap a hard calendar event;
* never schedule during sleep or outside the waking window;
* never exceed the real free capacity (buffer preserved);
* never place completed / cancelled / blocked tasks;
* respect explicit task dependencies and a valid project DAG;
* never place more than the task's remaining effort (never negative);
* no two sessions for the same task overlap, and no session overlaps a hard
  event;
* respect deadlines where feasible, otherwise report an explicit violation.

If any hard constraint cannot be satisfied the result is **explicitly
infeasible** (`feasible=False`) with a `violations` entry; a hard constraint is
never silently broken.

**Soft constraints influence optimization only.** Inferred preferences
(routines, location, learned habits) are soft and can never become hard — the
`Constraint` model already refuses to exist when `hardness == hard` and the
source is inferred. Context affinity is the *smallest* term in the ordering, so
a nearer deadline always wins:

> CS168 due tomorrow + the user usually studies CS168 at 2 PM. If only 10 AM is
> feasible, the optimizer schedules 10 AM. Preference loses to
> feasibility/deadline safety.

## 4. Objective scoring

A configurable weighted sum (`DEFAULT_WEIGHTS`, overridable per request):

| Objective | Default | Meaning |
|-----------|---------|---------|
| `deadline_safety` | 0.40 | deadline tasks fully scheduled before their deadline |
| `risk_reduction` | 0.20 | scheduled minutes weighted by project risk |
| `priority` | 0.15 | scheduled minutes weighted by priority |
| `dependency_progress` | 0.10 | dependency-bearing tasks completed |
| `continuity` | 0.05 | fewer, larger sessions (anti-fragmentation) |
| `preference` | 0.05 | explicit preferred windows honoured |
| `churn` | 0.05 | existing blocks preserved |

Strategies change only the weighting/order, never the hard constraints:

* `baseline` — the historical `schedule.Task.urgency_key` order;
* `deadline_first` — nearest deadline first;
* `risk_first` — highest project risk first;
* `priority_first` — highest priority first;
* `balanced` (default) — weighted blend with deadline safety dominant.

## 5. Project-aware planning

The optimizer consumes the M3 model (Project → Milestone → Task → Dependency →
Remaining effort → Deadline). Explicit dependency edges are topologically
ordered (Kahn) so a dependent task is never placed before its prerequisites;
inferred edges stay advisory and never block. Project risk comes from the
existing `ProjectIntelligence.risk` API — risk logic is not duplicated.

## 6. Multi-day and deadline capacity

The horizon is a bounded list of local midnights (`optimizer_max_horizon_days`,
default 14) covering today, the next 2/7 days or an arbitrary range. For each
project the risk summary computes:

```
remaining_minutes, available_minutes (before the deadline), slack = available − remaining
```

A negative slack becomes high/critical pressure; a workload that cannot fit is
reported as infeasible rather than pretended to fit. Sufficient capacity is not
compressed unnecessarily (earliest-feasible placement, buffer preserved).

## 7. Rescheduling, churn and undo

* Existing blocks that are still valid (inside the horizon, no conflict, before
  the deadline) are **pinned** and preserved, so re-optimizing moves as few
  blocks as possible.
* A forced window (`pinned`) is a hard directive; if it conflicts the result is
  infeasible and explained.
* A complete diff is returned: `moved`, `added`, `removed`, `unchanged`.
* `RESCHEDULE_OPTIMIZED` returns `NEEDS_CONFIRMATION` with the diff, affected
  tasks, deadline/risk impact and `undo_available`. It never commits. Committing
  reuses the existing planner snapshot/undo path (`plan_day` writes a new plan
  and moves the previous one to history; `undo` restores it).

## 8. Explainability

Every non-obvious decision is explained in human terms, e.g.:

* *"CS168 Project 1 scheduled at 07:00-08:30 because it is due 2026-09-08 23:59
  and it is priority 4."*
* *"CS168 Project kept at 09:00-10:00 to avoid unnecessary churn."*
* *"CS168 scheduled at 21:00-22:00 because you explicitly asked to work on it
  then."*

Raw objective values remain available in `objective_breakdown` but are not the
primary output.

## 9. Safety boundary

Optimization is **read-only**. It never writes a plan, never touches Google
Calendar and never executes a side effect. Changing a schedule or a calendar
still goes through the existing safety → permission → idempotency → audit path.
`optimize_day`, `optimize_week`, `evaluate_schedule` and `find_best_slot`
classify as `read`; `reschedule_optimized` is a reversible `low_risk_write` and
is only applied after confirmation.

## 10. Performance and future solvers

The engine is intentionally bounded: a deterministic greedy pass in dependency
order plus a small local-improvement budget
(`optimizer_max_iterations`), with explicit horizon and session caps. A fast
deterministic "good" schedule is preferred over an expensive perfect one on the
resource-constrained Pi.

A future backend only has to implement:

```python
optimizer.optimize(request: ScheduleRequest) -> OptimizationResult
```

No OR-Tools/Timefold dependency is installed for M5.

## 11. Tests

`tests/run_acceptance_m5.py` (75 deterministic checks, frozen clocks) covers
hard constraints, soft objectives, multi-day feasibility and slack,
rescheduling/churn/undo, realistic CS168/CS188 scenarios, the safety boundary
and regression of the M3/M4 surfaces and MCP profiles.
