---
name: butler-context
description: >-
  Pull Butler's freshest personal context before answering anything about the
  user's day, home, or location. Use for "what does my day look like", "am I
  home", "what's on my calendar", or any question where being up-to-date matters.
metadata:
  openclaw:
    requires:
      bins:
        - python
---

# Butler Context

Butler maintains a live, DB-backed snapshot of free time, events, tasks, courses,
expiring food, file counts, and (if Home Assistant is configured) presence. Pull
it before any answer that could go stale.

## Workflow

1. **Full snapshot**: `context` returns `now`, `free_minutes_today`,
   `events_today`, `events_week`, `tasks`, `courses`, `food_expiring`,
   `file_count`, `presence`, `timeline`.
2. **Home presence**: `presence` returns Home Assistant presence/location (empty
   if HA is not configured).
3. **Standup / EOD**: `brief` (morning briefing) and `review` (end-of-day) give
   executive summaries. `what_now` for current recommendation.

## Rules

- Always call `context` (or a specific tool) instead of answering from memory.
  Butler is authoritative; its data is fresh because every tool re-reads the DB.
- `presence`/HA data is only available if `BUTLER_HA_TOKEN` + HA url are
  configured. If empty, say so honestly — do not invent a location.
- Combine `context` with `what_now`/`brief` to avoid repeating the same info the
  user already knows; be brief and actionable.
- The snapshot is computed at call time — treat timestamps as real clock values.
