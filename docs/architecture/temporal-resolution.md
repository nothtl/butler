# Temporal resolution

DeepSeek identifies the **time expression**; the deterministic
`TemporalResolver` (`butler/agent/temporal.py`) owns the **clock**. The model
never decides hard schedule geometry.

```
User: "after lunch"
DeepSeek -> temporal.phrase = "after lunch"
TemporalResolver -> start = max(now, 13:00), end = sleep_start
```

## Supported expressions

| Phrase | Resolution |
|---|---|
| `in N hours/minutes/days/weeks` | now .. now + delta |
| `now`, `today`, `tonight`, `this evening` | bounded window |
| `this morning/afternoon` | daypart window |
| `tomorrow`, `tomorrow morning/afternoon/evening/night` | next day / daypart |
| `next week`, `this week`, `rest of the week` | week window |
| `next <weekday>`, `this coming <weekday>` | next occurrence (strictly after today for "next") |
| `next weekend`, `this weekend` | Saturday .. Monday |
| `after lunch` / `before lunch` | lunch window (12:00–13:00) |
| `after dinner` / `before dinner` | dinner window (18:00–19:00) |
| `later tonight` | 20:00 .. sleep |
| `before my meeting` / `after my meeting` | live calendar window (ask if none) |
| `before/after my lecture/class` | class window (ask if none) |
| `before/after <clock>`, bare `<clock>` | parsed clock time |
| `next hour` / `in an hour` | now .. now + 1h |

## Interpretation rules

- **"next Tuesday"** = the next Tuesday strictly after today (today Tuesday →
  +7 days). Documented and tested.
- **"before my meeting"** uses the live meeting window; with no calendar
  context it is **unresolved** and Butler asks — it never invents a time.
- Unresolvable phrases return `resolution=unresolved` with `start=end=0`.

## Scope

`TemporalResolver.scope` maps the phrase to a `ScopeKind` (NOW, TONIGHT,
TOMORROW, THIS_WEEK, NEXT_WEEK, RANGE) used by the scheduler.
