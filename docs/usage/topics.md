# Topics

A Telegram forum topic is a **context/view** over Butler's existing data — not a
separate system and not a hardcoded routing destination.

## Creating a topic

Make a new forum topic and send any message. Butler replies:

> New topic detected: CS188.
> What is this topic for?
> You can just describe it naturally.

Describe it however you like:

> This is my CS188 course. Track homework and projects, and help me schedule
> the work.

Butler replies with a **setup proposal** (not an immediate configuration):

```
Topic:
  CS188

Purpose:
  Course management

Description:
  This is my CS188 course. Track homework and projects...

Suggested capabilities:
  ✅ Know
  ✅ Track
  ...

Confirm to save and pin the control panel.
[✅ Set up]  [⚙ Customize]  [✕ Cancel]
```

Nothing is persisted as active and **no panel is pinned** until you confirm.
Confirming applies exactly the proposal that was shown — Butler does not
re-interpret the description on confirmation. `⚙ Customize` lets you toggle
capabilities before saving.

Butler turns that into:

- a name taken from the **Telegram topic title** (never from the description)
- a purpose (e.g. "Course management")
- a description (your sentence)
- capabilities (Know, Track, Schedule, Proactive, …)
- links to existing data (the CS188 course)
- a pinned control panel

Name, purpose and description are separate. If Telegram does not supply a
title, the topic stays unnamed ("New topic") rather than borrowing the
description.

## The pinned control panel

One Butler-managed message per active topic, pinned, edited in place:

```
📚 CS188
━━━━━━━━━━━━━━

Purpose
Course management

Butler
✅ Know
✅ Track
✅ Remember
✅ Plan
✅ Schedule
❌ Remind
✅ Proactive
❌ Web
❌ Files

Connected
• CS188 (course)

Tracking
🟢 CS188 assignments

Current
• CS188 HW4 (due Mon 23:59)

Storage
📁 ~/University/CS188

Last updated
10 Sep · 14:20
```

Buttons: **⚙ Settings**, **🔗 Connections**, **📋 Details**, **📁 Storage**,
**🔄 Refresh**. The database is the source of truth; the pin is a projection.

The bot **owns its panel**. When a capability or linked state changes it
regenerates the panel, compares the content hash and edits the existing pinned
message in place — you never have to say "update the pinned message". If the
message was deleted or can no longer be edited, Butler sends a replacement,
pins it and records the new id (exactly one panel per topic, monotonic
version). A per-topic lock serialises concurrent updates so capability,
tracker and connection changes cannot race into duplicate panels.

If a linked record is removed or archived, the panel shows
`• N connection(s) no longer available` instead of failing, and the link can be
repaired or removed.

## Capability states

| Icon | State |
|------|-------|
| ✅ | enabled |
| ❌ | disabled |
| ⏸ | paused |
| ⚠️ | degraded |
| ➖ | not applicable |

## Capability vs provider

A topic capability being *enabled* is not the same as the underlying provider
being *healthy*. If Web is on for a topic but the provider is offline, the
panel and settings say so honestly:

> ⚠️ Web is enabled for this topic, but the web provider is currently unavailable.

Butler never claims a capability is working merely because the toggle is on.

## Shared data, not copies

Topics reference existing records by id. Two topics can point at the same
course, project or food item; the record is never duplicated. This is how Food
and Groceries cooperate:

- Food topic: meal planning, pantry, recipes
- Groceries topic: shopping and replenishment

Both use the same pantry/meal records, so a low-stock item shows up in Groceries
without a copy.

## Connections

`🔗 Connections` shows, in plain language, what the topic is linked to: the
course, projects, tasks, documents, trackers and connected topics.

## Custom topics

Any topic works without a new module — a club, a research idea, a hobby:

> Create a topic for UAV Research.
> This is my research on degraded visual navigation.

Butler captures the purpose, suggests capabilities and stores information as
notes/links. There is no per-topic code.
