# Settings

Butler has **two** kinds of settings and they never overlap:

- **System settings** — how Butler behaves overall (`/settings`).
- **Topic settings** — what a specific topic is for (open `/topic`, tap
  ⚙ Settings).

Both can also be changed by talking naturally, and all interfaces update the
same underlying state.

## Scope resolution

Inside a topic, a capability request is **topic-scoped** by default:

```
enable web              → proposes enabling Web for this topic
turn web on here        → proposes enabling Web for this topic
disable scheduling here → proposes disabling Schedule for this topic
```

A request is treated as **global** only when the scope is explicit:

```
enable web globally
disable web everywhere
```

Global changes go to the system settings service; topic changes go to the topic
profile. Butler shows the interpreted scope in the proposal before applying a
consequential change, and asks when the scope is ambiguous. It never silently
changes global configuration when you likely meant the current topic. If global
Web is disabled, a topic cannot enable it — Butler explains that global Web must
be enabled first.

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

Natural-language topic changes are **proposal-first**: Butler replies
`Enable Web for Food? [✅ Enable] [✕ Cancel]`, and only applies the change after
you confirm. Button toggles in the ⚙ Settings view are an explicit action and
apply immediately. Either way, the change is persisted, audited, and the pinned
panel is regenerated and edited automatically — no "update the pinned message"
command is needed.

When a capability is on but its provider is offline, the settings view and panel
state that the provider is unavailable rather than implying the capability works.

## What is not a setting

- Secrets and credentials (environment variables / config file only).
- Per-topic purpose and links (stored on the topic, not system-wide).
- Tracking rules (managed by `/track` and `/trackers`).
