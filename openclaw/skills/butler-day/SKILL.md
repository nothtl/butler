---
name: butler-day
description: >-
  Plan, explain, and update the user's day. Use for "plan my day", "what should
  I do now", "why did my plan change", "reschedule", or "help me prioritize".
  Answers scheduling questions grounded in Butler's deterministic planner, not
  guesswork.
metadata:
  openclaw:
    requires:
      bins:
        - python
---

# Butler Day

Butler owns planning. It computes schedules deterministically from real task
and calendar data; you (the agent) are the interface, never the source of truth.

## Workflow

1. **Now / today**: call `what_now` for "what should I do now / next", or `day`
   for the full today plan.
2. **Why it changed**: call `why` to read the last plan diff.
3. **Add something urgent**: call `came_up` (title, est_minutes, priority).
   For a simple unplanned task that is NOT urgent, prefer `task_add`.
4. **Update a task**: `task_start` / `task_done` / `task_skip` / `task_defer` /
   `task_block` / `task_cancel` (all take `task_id`).
5. **Fix a bad schedule**: `reschedule` to re-solve the day, then `undo` to
   revert the last change.

## Rules

- ALWAYS call `day`/`what_now`/`tasks` before answering "what's my day like".
  Never invent slots, free time, or priorities.
- `came_up` re-plans the day; `task_add` only creates the task. Use the one the
  user actually meant.
- A task's `deadline` is a UNIX epoch (seconds). Only set it if the user gives an
  explicit due time.
- If `what_now` returns no clear recommendation, defer to `brief` for context and
  reply honestly ("nothing scheduled; you could pick up X").
- Side-effecting calls (task transitions, came_up, reschedule, undo) require
  operator approval — expect a prompt. Do not try to work around it.
