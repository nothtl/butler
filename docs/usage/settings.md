# Settings

Butler has **two** kinds of settings and they never overlap:

- **System settings** — how Butler behaves overall (`/settings`).
- **Topic settings** — what a specific topic is for (open `/topic`, tap
  ⚙ Settings).

Both can also be changed by talking naturally, and all interfaces update the
same underlying state.

## System settings (`/settings`)

```
⚙️ Butler Settings

📅 Scheduling
  Day starts: 07:00
  Day ends: 23:00
  Ask before calendar changes: on

🔔 Notifications
  Quiet hours start: 22:00
  Quiet hours end: 08:00
  Proactive alerts: on
  Alert threshold: medium

🧠 Memory
  Memory: on

🔗 Integrations
  Web knowledge: on
  Tracking: on

📍 Location
  Location precision: zone
```

Secrets (tokens, API keys) are never shown.

## Natural language

```
Don't schedule work after 10 PM.
Quiet hours 11 PM to 7 AM.
Stop proactively messaging me about low-priority things.
Disable web.
Enable memory.
Ask before changing my calendar.
Track location only at zone level.
```

Butler interprets the request, applies it, confirms in one line, and the change
survives a restart. On the read-only surface (MCP) the same request is returned
as a confirmation proposal instead of being applied.

## Topic settings

Topic capabilities (Know, Track, Remember, Plan, Schedule, Remind, Proactive,
Web, Files) are toggled from the pinned control panel's **⚙ Settings** view.
Each capability has a clear state (✅ enabled / ❌ disabled / ⏸ paused /
⚠️ degraded / ➖ not applicable).

## What is not a setting

- Secrets and credentials (environment variables / config file only).
- Per-topic purpose and links (stored on the topic, not system-wide).
- Tracking rules (managed by `/track` and `/trackers`).
