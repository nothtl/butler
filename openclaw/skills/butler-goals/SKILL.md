---
name: butler-goals
description: >-
  Help the user set, track, and review goals/projects against real progress.
  Use for "how close am I to X", "make this a goal", "what's my streak", or
  reviewing long-term progress. Grounds motivation in Butler's reward/audit data.
metadata:
  openclaw:
    requires:
      bins:
        - python
---

# Butler Goals

Goals are long-lived. Butler tracks progress through tasks, routines, and its
reward/audit history. You translate user goals into trackable state and report
real progress — never invented numbers.

## Workflow

1. **State current progress**: read `tasks`, `brief`, and `review` to see what's
   done / pending. `review` covers end-of-day accomplishments.
2. **Turn "a goal" into trackable work**: use `task_add` for concrete next steps
   with `est_minutes` and optional `deadline`. Keep actions small and verifiable.
3. **Routines/habits**: `routines_list` + `scan_routines` to detect or confirm
   recurring habits that support the goal; `routines_confirm` to lock one in.
4. **Motivation**: Butler's rewards table tracks earned "treats". Report streaks
   only from `audit`/`review` data, not estimates.

## Rules

- Break a fuzzy goal into concrete `task_add` items (each one actionable). Do NOT
  add a single giant ambiguous task.
- Avoid over-promising: report what the tools actually show. If a goal has no
  tracked state yet, help set it up (tasks/routines) rather than guessing a
  percent complete.
- `task_add`, `routines_confirm`, `scan_routines` are guarded writes → operator
  approval.
- Prefer `brief`/`review` for milestone retrospectives; use them as the source of
  "what changed".
