# Project Intelligence (M3)

Status: implemented. This document describes the durable **Goal → Project →
Milestone → Task** model that lets Butler answer *"what work remains, how much,
what depends on what, and can I realistically finish it?"* without inventing
numbers.

The module is `butler/projects.py` (`ProjectIntelligence`), backed by three new
tables in `butler/db.py` and exposed through the executive service and the
read-only MCP surface.

## 1. Why a project layer

Before M3 the durable units were *courses*, *assignments* and *tasks*. That is
enough to schedule a single deadline but not to reason about a multi-week body
of work. M3 adds the smallest model that answers the executive questions while
**reusing the existing task/deadline system** — a project is not a second
scheduler and does not duplicate tasks.

```
Goal  ──▶  Project  ──▶  Milestone  ──▶  Task  ──▶  Schedule
                                (dependencies connect tasks)
```

## 2. Data model

### `projects`
| Column | Meaning |
|--------|---------|
| `id` | integer key |
| `name` | display name |
| `objective` | free-text goal/description |
| `status` | `active` / `paused` / `completed` / `archived` / `cancelled` |
| `priority` | 1 (low) … 5 (critical) |
| `course_id` | optional link to `courses` (0 = none) |
| `deadline` | unix ts, 0 = none |
| `estimated_total_minutes` | project-level estimate (optional) |
| `remaining_minutes` | project-level remaining (optional) |
| `repo_url` | repository / GitHub URL (reference only) |
| `links` / `refs` | JSON arrays of external links / document references |
| `provenance` | JSON map, e.g. `{"deadline": "inferred"}` |
| `created_at` / `updated_at` | unix ts |

### `milestones`
`id`, `project_id` (FK, cascade delete), `name`, `description`,
`order_index`, `status` (`pending` / `active` / `completed` / `skipped`),
`deadline`, `estimated_minutes`, `remaining_minutes`, `progress`, timestamps.

### `task_deps`
`task_id`, `depends_on`, `inferred` (0/1), `created_at`,
primary key `(task_id, depends_on)`. Edge direction is **task depends on
depends_on** (i.e. `depends_on` must finish first).

### Tasks (extended)
`tasks` gained `project_id`, `milestone_id`, `remaining_minutes` — all default
`0`, meaning "not part of a project", so every pre-M3 task is untouched.

## 3. Migration

`DB.__init__` runs `executescript(SCHEMA)` **before** `_migrate()`. Because of
that ordering:

- New columns are added with `ALTER TABLE` inside `_migrate()` (idempotent).
- The index on the migrated column (`idx_tasks_project`) is created at the
  **end of `_migrate()`**, *after* the `ALTER`, not in `SCHEMA` — otherwise a
  pre-M3 database would fail with `no such column: project_id` when `SCHEMA`
  runs.

`tests/run_acceptance_m3.py::test_migration` builds a realistic pre-M3 `tasks`
table, opens it through `Container`, and asserts the columns/tables/index appear,
the legacy row survives, and reopening is idempotent.

## 4. Progress — effort-based, never task counts

`ProjectIntelligence._effort()` sums task minutes:

- `completed` status counts toward **completed** minutes.
- `skipped` / `cancelled` are **excluded entirely** (they leave both the
  numerator and the denominator).
- every other status (`todo` / `doing` / `scheduled` / `deferred` / `blocked`)
  counts toward **remaining** minutes.
- a task's remaining is `remaining_minutes` when set, else `est_minutes`.

`progress = completed_minutes / estimated_minutes`, or **`None` with
`progress_source = "unknown"`** when no estimate exists. A project is never
reported as "100%" merely because all its tasks were ticked off, and an
unestimated project is never reported as "0%".

If a project has no linked tasks but carries an explicit
`estimated_total_minutes` / `remaining_minutes`, that explicit effort is used
(`progress_source = "explicit"`).

## 5. Workload

`workload(ident)` returns remaining/completed/estimated minutes, derived
progress + source, milestone roll-ups, blocked task ids, `next_tasks`
(see below), and — when a deadline is set — the free minutes available until it
(`available_minutes_until_deadline`, computed read-only from planner geometry)
plus a `feasible` boolean (`remaining <= available`). This is the structured
workload an executive planner/scheduler consumes; the project module does not
schedule anything itself.

`candidates(ident)` ranks active project tasks: **unblocked first**, then higher
priority, then earlier deadline. Blocked tasks are surfaced but ranked last.

## 6. Risk — deterministic and explainable

Risk is a weighted sum of five named factors. Weights sum to 1.0:

| Factor | Weight | Value |
|--------|--------|-------|
| `deadline_pressure` | 0.40 | `remaining / available_minutes_until_deadline` (0 if no deadline, 1 if no time left) |
| `overdue_milestones` | 0.20 | overdue / total milestones |
| `dependency_bottleneck` | 0.15 | blocked active tasks / active tasks |
| `stalled_progress` | 0.15 | days since last completion / 30 (after a 7-day grace) |
| `estimate_uncertainty` | 0.10 | 0 when derived, 1 when unknown |

`score = Σ weight × value` (clamped 0–1); level is `low < 0.34 ≤ medium <
0.67 ≤ high`. `most_at_risk()` returns the highest-scoring active project. No
LLM is ever involved in the score.

## 7. Dependencies — validated DAG

- Edges are stored in `task_deps`; **cycles are rejected** at write time by
  `add_dependency()` (DFS white/gray/black), which rolls the edge back.
- Self-edges and unknown tasks are rejected.
- **Inferred** edges (`inferred=1`) are **advisory**: `blocked_task_ids()` only
  considers explicit edges, so a guessed dependency can never silently block
  work. Inferred edges are marked as such in `dependencies()`.
- `blocked_task_ids()` returns active tasks whose explicit prerequisite is not
  yet `completed`; completing the prerequisite unblocks them.

## 8. Proposal — safe project creation

`propose_project(text)` deterministically parses a description into a proposal
**without writing anything**: name, deadline (`in <n> days/weeks`, ISO date,
tomorrow/today, weekday), total effort (`~12 hours`), and comma/slash-separated
milestones. Every field is marked `inferred` in `provenance`, `confidence`
drops to 0.5 and `questions` is populated when a field is missing.

The executive service routes `CREATE_PROJECT` to this proposal and returns
`NEEDS_CONFIRMATION`; it never persists. Actual creation (`create_project`,
`add_milestone`, `link_task`, `add_dependency`) is a separate, explicit,
safety-classified write (`project_create` / `project_update` /
`project_link` / `project_milestone` / `project_dependency` are
`low_risk_write`).

## 9. Course integration

Projects may link to a course via `course_id`. Assignments remain ordinary
tasks, so the existing course → assignment → task flow keeps working; a course
project simply groups related tasks/milestones. Assignments are **not** forced
into projects. GitHub/document support is limited to the `repo_url` / `links` /
`refs` reference fields — there is no crawler and no fabricated GitHub progress.

## 10. Executive service + MCP

The executive service (`butler/agent/service.py`) answers:

| Request | Behaviour |
|---------|-----------|
| `PROJECT_STATUS` | one project (target) or the project list |
| `PROJECT_WORKLOAD` | remaining effort / next tasks (target or aggregate) |
| `PROJECT_RISK` | project risk, or `most_at_risk` when untargeted |
| `PROJECT_DEPENDENCIES` | nodes, edges, blocked tasks, cycles (target required) |
| `PROJECT_NEXT` | project-scoped advisory ranking (falls back to `what_now` without a target) |
| `CREATE_PROJECT` | gated proposal → `NEEDS_CONFIRMATION`, no write |

The deterministic interpreter classifies these from text (project keyword
tables + entity extraction), and the LLM interpreter schema lists the same
action names. `ContextSnapshot.projects` carries a bounded project summary.

Read-only MCP tools (profile `readonly`): `get_projects`, `get_project`,
`get_project_workload`, `get_project_risk`, `get_project_dependencies`. The
`full` profile is unchanged at 51 tools; no mutating project tool is exposed.

## 11. Safety invariants

- An inferred deadline or effort is never authoritative (provenance + gated
  proposal).
- Guessed progress never marks anything complete; unknown progress is `None`.
- Inferred dependencies are advisory and never enforced by the scheduler.
- Project creation never blocks the calendar automatically.
- Project data never overwrites external calendar events.
- Risk is deterministic; no LLM-invented scores.
