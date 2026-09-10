# Using Butler on Telegram

Butler is a personal assistant you talk to naturally. You do not need to learn
an internal vocabulary — say what you want and Butler figures out the rest.
Slash commands are just shortcuts.

## The mental model

| Concept | Question it answers |
|---------|---------------------|
| Topics | What am I talking about? |
| Data | What does Butler know? |
| Tracking | What should Butler watch? |
| Actions | What should Butler do? |
| Schedule | When should it happen? |
| Memory | What should Butler remember? |
| Settings | How should Butler behave? |

## Just talk

```
What should I do today?
Track CS188 assignments.
Add this to my project.
Remember I prefer 2-hour work blocks.
What's low in my pantry?
Add milk to groceries.
Replan my week.
```

## Shortcuts

| Command | What it does |
|---------|--------------|
| `/day` | today's plan |
| `/week` | the week ahead |
| `/now` | what to do now |
| `/tasks` | your tasks |
| `/projects` | your projects |
| `/add` | add something |
| `/link` | connect information |
| `/organize` | organize files |
| `/track` | watch something |
| `/trackers` | what Butler is watching |
| `/topics` | your topics |
| `/topic` | this topic's control panel |
| `/memory` | what Butler remembers |
| `/settings` | how Butler behaves |
| `/context` | what Butler knows right now |
| `/health` | subsystem status |
| `/undo` | undo the last change |

`/help` lists these grouped.

## Topics

In a Telegram forum, each topic is a context. Send any message in a new topic
and Butler asks what it is for; describe it in your own words and Butler sets up
a pinned control panel with the right capabilities. See
[topics.md](topics.md).

## Tracking

Tell Butler what to watch and it will notify you through the normal policy
(quiet hours, daily budget, snooze, dismiss):

```
Track CS188 assignments.
Tell me when chicken gets below 2.
Track my Stock Bot GitHub.
What are you tracking?
Why did you notify me?
```

## Adding, linking, organizing

```
Add a CS188 project due Friday around 8 hours.
Add chicken to my pantry.
Link this to CS188.
Organize these files.
```

Butler resolves names against what it already knows and avoids duplicates. If it
is unsure it asks instead of guessing.

## Scheduling

```
I need two hours for CS188 now.
What should I work on tonight?
Find me two hours tomorrow.
Replan my week.
```

Butler protects hard commitments and always asks before changing your calendar.

## Errors

If something fails you get a short, human explanation ("the local database was
temporarily busy; nothing was changed") — never a stack trace. Details stay in
the logs.

## Privacy

Butler never prints tokens, API keys or passwords. Location, when configured,
is zone-level only.
