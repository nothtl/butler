# Universal Tracking & Trigger Engine (N2)

Status: implemented. **One** generic engine turns any "when X changes, do Y"
request into a deterministic tracker. It is not a workflow framework, not a
graph database and not a second notification system.

```
tracker -> source provider -> normalized state -> diff -> events
        -> condition evaluation -> action proposal
        -> M7 ProactiveCandidate -> existing notification policy -> Telegram
```

## 1. Adopted design ideas (research)

| Reference | Idea adopted | Why |
|-----------|--------------|-----|
| Home Assistant automations | split *trigger/condition/action*; small explicit condition vocabulary | familiar, deterministic, no scripting language |
| n8n / Temporal (durable workflow) | persist state (snapshot, hash, next check, failures) so restarts are safe | no re-firing unchanged observations |
| Airflow / Dagster | cadence + `next_check_at` instead of a loop | bounded, no uncontrolled polling |
| Butler M7 | reuse ranking/budget/quiet-hours/snooze/dedup | never a second notifier |
| Butler M4 | reuse the web stack | never a second HTTP/crawler |
| Butler Phase 6 | reuse retry, idempotency, audit | consistent failure handling |

No external dependency is introduced.

## 2. Models

**Tracker** (one compact row; the conceptual Tracker+Trigger split is folded
into JSON `condition`/`action`):

`id, name, target_type, target_id, target_ref, source, condition, action,
cadence_seconds, scope (global|topic|object), priority, destination_chat_id,
destination_thread_id, destination_topic_id, enabled, state, last_checked_at,
next_check_at, last_state_hash, last_snapshot, last_event, last_event_at,
failure_count, cooldown_until, one_shot, completed, expires_at, provenance,
created_at, updated_at`.

**Event** (meaningful changes only): `event_type, target, before, after,
observed_at, evidence, deterministic event_key`.

**ActionProposal**: `NOTIFY_TOPIC`, `CREATE_SUGGESTION`,
`UPDATE_INTERNAL_STATE`, `CREATE_REMINDER`, `PROPOSE_CALENDAR_ACTION`.

**EvaluationResult**: `fired, events, proposals, reason, state, snapshot`.

## 3. Event model

`NEW_ITEM`, `ITEM_REMOVED`, `DATA_CHANGED`, `THRESHOLD_CROSSED`,
`DEADLINE_CHANGED`, `RISK_CHANGED`, `STATUS_CHANGED`, `SCHEDULE_CONFLICT`,
`TIME_REACHED`, `EXTERNAL_SOURCE_UPDATED`. Low-level poll results are **not**
stored; only meaningful events get a `tracker_events` row (UNIQUE
`event_key`).

## 4. State comparison

Snapshots are normalized (`items` keyed by id/name with `fields`, plus
top-level `fields`). Diffing is field-level: HTML/formatting/order changes do
not produce events; only meaningful field changes, additions and removals do.
Thresholds fire on **crossing**, not on steady state.

## 5. Source providers

A generic `SourceProvider.snapshot(tracker, now) -> dict` interface. Built-ins:
`food`, `project_risk`, `task`, `calendar` (local); `course`, `web`, `github`,
`file` (external/local). Providers are injectable (tests/sandbox use
`SnapshotProvider`). The engine never knows how a source works. Local state
(e.g. course documents the course subsystem already maintains) is preferred
over re-scraping.

## 6. Rule language

Constrained, explicit, deterministic conditions:

`new_item`, `item_removed`, `field_changed`, `deadline_changed`,
`status_changed`, `risk_crossed_above`, `threshold_below`, `threshold_above`,
`no_activity`, `time_reached`, `state_changed`, plus limited `and`/`or`
(capped at 4 leaves, depth 2). No arbitrary scripting and no LLM in evaluation.

## 7. Actions & safety

Allowed actions are the five above. `PROPOSE_CALENDAR_ACTION` (and any
consequential action) is only ever **proposed** with
`requires_confirmation=True`; it never executes automatically. There is no
purchasing, booking, messaging or arbitrary posting. Actual writes still go
through safety → permission → idempotency → audit.

## 8. Idempotency, cooldown, retries

* `tracker_events.event_key = sha1(tracker_id:event_type:identity:version)` is
  UNIQUE, so repeated polls/restarts/scheduler runs never duplicate events.
* Tracker-level `cooldown_until` suppresses repeated notifications for the same
  fired state; a materially changed event notifies again.
* Provider failures increment `failure_count`, set `degraded`, back off
  `next_check_at`, and reach `error` after `tracker_max_failures`; a later
  success resets to `active`. `error`/`degraded` are retried with backoff.
* `run_due` only evaluates `active|degraded|error` trackers whose
  `next_check_at` is due, capped by `tracker_max_per_cycle`.

## 9. Cross-topic flow (no direct calls)

A tracker names a `destination` (chat/thread). It emits a candidate; M7 delivers
to that destination. Example **Food → Groceries**: a Food tracker watches the
pantry quantity and produces a `CREATE_SUGGESTION` candidate whose destination is
the Groceries topic. Food and Groceries never call each other; they share the
same food records and communicate through events/candidates.

## 10. Relationship to M7 and M5

Tracking **never** sends Telegram directly. `ProactiveEngine.ingest()` accepts
tracker candidates and runs the same ranking/policy/notification path (budget,
quiet hours, snooze, suppression, dedup). Tracking does not schedule: a
meaningful event can surface a recommendation, and scheduling still goes through
the optimizer and confirmation.

## 11. Natural language

`TrackerEngine.parse_request(text, context)` deterministically maps requests to
a tracker proposal (target, source, condition, cadence, destination, priority,
scope, one-shot, expiry) and returns questions when ambiguous. Examples:
"Track CS188 assignments.", "Tell me when chicken gets below 2 portions.",
"Track my Stock Bot GitHub.", "Track basketball club announcements.",
"Track this until Friday." Explicit low-risk rules are created directly;
over-broad or ambiguous requests ask for specifics.

## 12. Database

`trackers` and `tracker_events` with indexes on state, enabled, next_check_at,
target and destination. Migrations are idempotent (`CREATE TABLE IF NOT
EXISTS`); no existing data is touched.

## 13. Security

Authorization and the safety gate are unchanged; the read-only MCP surface only
exposes `get_trackers`/`get_tracker`/`evaluate_tracker` and never a write tool.
Web sources keep M4's URL validation and prompt-injection defences; webpage text
stays data. Tracker rows contain no secrets.

## 14. Non-goals

No second scheduler/notifier/memory/web/calendar; no generic scripting
language; no direct subsystem-to-subsystem calls; no external workflow engine.

## 15. Tests

`tests/run_acceptance_n2.py` (167 deterministic checks) covers lifecycle, local
state, external sources, conditions, cross-topic, dedup/cooldown, degradation,
safety, memory, scheduler, natural language, one-shot, explanations, MCP, N1
panel integration, sandbox scenarios and regression.
