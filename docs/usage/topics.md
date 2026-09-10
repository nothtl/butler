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

Butler turns that into:

- a purpose (e.g. "Course management")
- capabilities (Know, Track, Schedule, Proactive, …)
- links to existing data (the CS188 course)
- a pinned control panel

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

## Capability states

| Icon | State |
|------|-------|
| ✅ | enabled |
| ❌ | disabled |
| ⏸ | paused |
| ⚠️ | degraded |
| ➖ | not applicable |

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
