# Proactive Executive Behavior (M7)

Status: implemented. This document describes the deterministic proactive loop
that lets Butler *notice* important situations and *propose* useful
recommendations, while remaining conservative and safe.

The engine is `butler/proactive_engine.py` (`ProactiveEngine`), wired into
`Container` as `self.proactive_engine`. It builds on M1–M6 and introduces **no**
second scheduler, memory system, notification system or autonomous agent.

## 1. The loop

```
OBSERVE  (context snapshot + planner + projects + memory + web)
  -> DETECT   (deterministic candidate detectors)
  -> SCORE    (weighted, explainable ranking)
  -> DECIDE   (notification policy: thresholds, cooldown, budget, quiet hours)
  -> EXPLAIN/PROPOSE (evidence-backed message + proposed action)
  -> NOTIFY   (Telegram or log)
  -> WAIT     (the user responds: accept / snooze / dismiss / ignore)
  -> optionally EXECUTE through the existing safety/permission/idempotency/audit path
```

The LLM may phrase a message; it never decides a side effect. The deterministic
executive layer is the authority.

## 2. Candidate types

| Category | Trigger | Evidence |
|----------|---------|----------|
| `deadline_risk` | remaining work > usable capacity before a deadline | remaining minutes, usable minutes, deadline, ratio |
| `free_time` | a large free window + unfinished high-value work | free minutes, task, reason |
| `missed_task` | a committed block passed without completion | planned start/end, task status, deadline |
| `schedule_conflict` | a hard event overlaps a committed block | event + planned window |
| `project_risk` | project risk rose past a threshold | old/new score, level, remaining |
| `estimate` | learned estimate factor deviates | factor, sample count |
| `routine` | a confirmed routine window + free time | routine, confidence, free minutes |
| `travel` | an event within the configured prep lead | event, start, location, lead |
| `food` | a planned meal is missing ingredients | recipe, missing items |
| `course` | a course deadline within 3 days | course, deadline |
| `web_change` | verified external value differs from memory | old/new value, source URL, provenance |

Every candidate is typed (`ProactiveCandidate`) with `key`, `category`, `title`,
`summary`, `priority`, `score`, `confidence`, `detected_at`,
`relevant_entities`, `evidence`, `proposed_action`, `requires_confirmation`,
`expires_at`, `state` and `explanation`.

## 3. Ranking

A deterministic weighted score over 0..1:

```
0.30*urgency + 0.20*risk + 0.15*capacity_deficit + 0.10*impact
+ 0.10*novelty + 0.10*confidence + 0.05*preference_boost - dismissal_penalty
```

mapped to `critical` (>=0.72), `high` (>=0.52), `medium` (>=0.34) and `low`.
Deadline risk outranks routine affinity; confidence and novelty matter; an
explicit memory preference (M6) shifts the soft rank; dismissal history applies
a penalty to that category — but **never** to critical warnings.

## 4. Notification policy

`policy_filter` applies, in order: expiry, confidence floor, minimum priority,
quiet hours (critical may bypass when `proactive_quiet_critical`), user
suppression (candidate or category), snooze, cooldown, dedup and the daily
budget (with a separate critical budget). Only high-value candidates reach the
user.

## 5. Deduplication, cooldown, budget

Each candidate has a deterministic key and a `state_hash`. A candidate is
notified once; an unchanged candidate is suppressed by the cooldown and then by
dedup. A material change (new hash) allows a fresh notification. The daily
budget defaults to 5 normal messages/day plus 2 critical. State is persisted in
`proactive_candidates` / `proactive_notifications`.

## 6. User response states

`pending`, `accepted`, `dismissed`, `snoozed`, `ignored`, `expired`. An
unanswered notification becomes `ignored` after expiry — **ignored is not
accepted and not completed**. Responses are recorded in `proactive_responses`.

## 7. Snooze

"Remind me later" / "Remind me in 3 hours" writes a deterministic snooze
(`proactive_snoozes`); the candidate is suppressed until the snooze expires and
may then resurface if still relevant. No continuous resurfacing.

## 8. Quiet hours & budget

Quiet hours reuse the existing `notify_quiet_*` configuration. Normal
notifications wait; critical warnings may still be delivered according to
`proactive_quiet_critical`. The daily budget bounds normal pushes; critical has
its own allowance. Tests use a frozen/test-local clock.

## 9. User preferences (M6)

Relevant memories shape ranking: "Don't bother me about low-priority tasks"
lowers low-priority candidates; "I want to know when a project becomes at risk"
boosts risk candidates. Explicit preferences override inferred ones; inferred
preferences stay soft and can never become hard constraints.

## 10. Learning from reactions

Accepted / dismissed / snoozed / ignored responses are aggregated per category
into soft rates that adjust ranking only. Dismissal history can never suppress a
critical safety or deadline warning.

## 11. Evidence & explanations

Every recommendation is evidence-backed with real state (no vague claims).
`explain_proactive_candidate` returns the evidence and the triggering condition
("why are you telling me this?"). Web-derived warnings retain source provenance
and expiry.

## 12. Action proposals & safety boundary

Proactive Butler proposes rather than executes. Candidate actions are limited to
read-only or Butler-owned operations (schedule a block, reschedule, plan,
optimize, add groceries, review a fact). Every mutation still goes through
safety → permission → idempotency → execution → audit. There is **no** automatic
external communication, purchase or booking, and no silent autonomous behavior.
Telegram `pro:` callbacks are validated (`^[a-z_]+:[a-z_:0-9-]+$`); accepting a
recommendation records consent but does not execute it.

## 13. Daily briefing

`ProactiveEngine.briefing()` produces a concise deterministic briefing: the
day's hard commitments, usable free time, the top critical/high candidates and a
recommendation. No giant memory dump.

## 14. Integrations

* **M3 projects** — risk, remaining effort and dependencies feed candidates.
* **M4 web** — verified external changes are surfaced with provenance; web
  checks respect a configured cadence/TTL and are never polled continuously.
* **M5 optimizer** — a recommendation is only made when a feasible block
  actually exists (the optimizer can veto it).
* **M6 memory** — preferences and learned estimates shape ranking and risk.
* **Courses / tasks / routines / food** — feed their respective detectors.
* **Audit** — candidate generated/suppressed/notified/responded/snoozed are all
  recorded with the existing audit layer.

## 15. Event-driven vs periodic

The existing `butler.scheduler` cadence drives `_run_proactive`, which runs the
legacy alert loop and then the M7 engine on the same tick. There is no
uncontrolled background loop. Event-driven invalidation is supported by
re-running `run_cycle` on demand (e.g. after a calendar or task change).

## 16. Idempotency, failure & degradation

`run_cycle` is safe to run repeatedly: candidate keys + state hashes + cooldown
+ dedup mean the same observed state never produces duplicate notifications. If
the calendar, memory, web or optimizer is unavailable the relevant detector
degrades (or is skipped); the deterministic candidates and template-based
explanations still function without an LLM. A read-only preview mode
(`persist=False`) computes candidates without writing anything, used by the
read-only MCP tools.

## 17. MCP surface

Read-only tools (profile `readonly`): `get_proactive_candidates`,
`get_proactive_status`, `explain_proactive_candidate`. `readonly` now exposes
**30** tools; `full` remains exactly **51**. No autonomous write tool is
exposed.

## 18. Database

Idempotent tables: `proactive_candidates`, `proactive_notifications`,
`proactive_responses`, `proactive_suppressions`, `proactive_snoozes`. The schema
is minimal and does not duplicate existing proactive state (`exec_state`,
`scheduler_state`, audit).

## 19. Configuration

`[proactive]`: `engine_enabled`, `daily_budget`, `critical_budget`,
`cooldown_minutes`, `min_priority`, `deadline_risk_ratio`,
`min_free_window_minutes`, `risk_increase`, `min_confidence`,
`candidate_expiry_minutes`, `prep_lead_minutes`, `web_check_enabled`,
`web_ttl_minutes`, `max_candidates`, `quiet_critical`, `briefing_enabled` and
per-category toggles (`cat_*`). Defaults are conservative.

## 20. Tests

`tests/run_acceptance_m7.py` (112 deterministic checks, frozen clocks) covers
generation, ranking, suppression, state, evidence, safety, integrations,
realistic scenarios and regression.
