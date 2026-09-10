# Long-Term Memory & Learning (M6)

Status: implemented. This document describes the durable, typed,
provenance-aware memory store that lets Butler remember useful information
across conversations and learn soft preferences/patterns over time — while
remaining conservative and trustworthy.

The module is `butler/memory.py` (`Memory`), wired into `Container` as
`self.memory` and exposed through the executive service and the read-only MCP
surface.

## 1. What was reused (audit)

M6 deliberately extends existing infrastructure instead of adding a second
memory system:

| Existing | Reused as |
|----------|-----------|
| `butler/timeline.py` (`timeline_events`, `tl_state`) | append-only behavioural evidence |
| `butler/routines.py` (`routines`) | the *only* routine detector; M6 mirrors it into typed `routine` memories via `sync_routines` |
| `butler/audit.py` | every mutation is audited with the same redaction rules |
| `butler/agent/session.py` | in-process conversation focus (unchanged) |
| `butler/affinity.py` | task category classification for estimate learning |
| `butler/web.py` | external provenance for web facts |

Newly added: the `memories` / `memory_evidence` / `memory_observations` tables,
`MemoryWriteGate`, typed retrieval, promotion/decay/conflict logic, the
`MEMORY_*` agent actions and the four read-only MCP tools.

## 2. Memory model

```
raw observation / user statement / web page
        │
        ▼
 typed candidate (type, subject, key, value, provenance, confidence, scope)
        │
        ▼
 MemoryWriteGate  (provenance, confidence, secrets, duplication,
                   contradiction, expiry, scope, confirmation)
        │
        ▼
 durable memory (memories + memory_evidence)
        │
        ▼
 bounded retrieval (search / get_relevant)
        │
        ▼
 optional promotion (inferred → confirmed, explicit only)
```

Types: `core_fact`, `preference`, `routine`, `project_fact`, `course_fact`,
`episodic_event`, `temporal_note`, `user_instruction`. `user_instruction` is
kept distinct from an inferred preference.

Each memory carries: `id`, `type`, `subject`, `key`, `value`, `source`,
`source_detail`, `confidence`, `provenance`, timestamps (`created_at`,
`updated_at`, `observed_at`, `expires_at`, `last_confirmed_at`),
`confirmation_state`, `scope`, `tags`, `active`, `supersedes_id`,
`superseded_by`, `usage_count`, `last_used_at`.

## 3. Provenance & trust

| Provenance | Trust | Meaning |
|------------|-------|---------|
| `explicit_user` | 1.00 | the user said it |
| `user_confirmed` | 0.95 | the user confirmed it |
| `calendar_observed` / `task_observed` / `course_observed` / `project_observed` | 0.80 | authoritative system observation |
| `web_verified` | 0.70 | external, source-backed evidence |
| `routine_inferred` | 0.40 | inferred from repeated behaviour |
| `llm_inferred` | 0.30 | proposed by a model |

Rules enforced by the gate:

* inferred memories can **never** become hard constraints;
* inferred memories can **never** override an explicit/user-confirmed memory;
* uncertain memories retain their uncertainty (low-confidence inferred writes
  are rejected);
* conflicting memories never both stay authoritative — the newer explicit value
  supersedes the older row;
* temporal facts expire; stale facts are marked stale, never silently current.

## 4. Write gate

Every write passes `MemoryWriteGate.evaluate` then `.apply`. The gate checks
provenance validity, confidence floor, memory type, sensitivity (secret
patterns), duplication (merge), contradiction (supersede or refuse), expiry,
scope, and the confirmation state. `apply` performs the insert/merge/supersede
and writes the audit record.

Examples:

* "Remember that I prefer study sessions longer than 90 minutes." → explicit
  preference → safe to save (confirmed).
* "You seem to prefer studying CS168 in the afternoon." → inferred → stored as
  inferred, never as an explicit preference.
* "You always study on Tuesday." → inferred routine → needs repeated evidence
  before it is even stored.

## 5. Confirmation states & promotion

`unconfirmed`, `confirmed`, `inferred`, `rejected`, `stale`. An explicit user
statement creates a `confirmed` memory. Observations from authoritative systems
are `confirmed`. Inferred memories are `inferred`. **Only explicit confirmation
promotes an inferred memory to confirmed** (`Memory.confirm`); there is no
silent automatic promotion. The user is never interrupted for tiny
observations.

## 6. Conflict resolution

Old inferred "prefers studying at night" + new explicit "I prefer afternoons
now" → the new row supersedes the old (`active=0`, `superseded_by` set,
`supersedes_id` on the new row). The historical row is retained and
`Memory.explain` returns the supersede chain. No useful history is deleted.

## 7. Decay & staleness

Per-type windows (`STALE_DAYS`): core facts and preferences are long-lived,
routines go stale after ~21 days without observation, course facts after ~120
days, project facts after ~30 days, temporal notes expire automatically.
`refresh_staleness` marks rows `stale` (or deactivates expired notes) without
deleting them. `Memory.freshness` is a deterministic 0..1 age factor.

## 8. Retrieval

Deterministic and bounded: `search`, `get_relevant(context)`, `list`,
`history`, `explain`. Candidate rows are filtered in SQL (type/active/scope)
and capped at `memory_max_scan`; scoring blends keyword overlap, recency,
confidence and trust. `get_relevant` takes a context (query/subject/entities/
tags) and never dumps the database. Semantic/vector search is intentionally not
required — SQLite FTS/keyword matching is sufficient for a personal store.

## 9. Memory context

`ContextSnapshot` gained `relevant_memories`, `memory_summary`,
`memory_warnings` and `memory_timestamp`, all strictly capped
(`memory_context_limit`, default 5). A CS168 question retrieves CS168 memories;
unrelated food/travel memories are excluded.

## 10. Learning

* **Behavioural observation** — `Memory.observe` records evidence with a count,
  first/last seen and confidence; one occurrence never creates a routine.
* **Routine learning** — reuses `butler/routines.py`; `sync_routines` mirrors
  confirmed/candidate routines into typed `routine` memories (idempotent). It
  never creates hard calendar commitments.
* **Estimate learning** — `record_estimate_sample` accumulates
  estimate-vs-actual samples per category; after `memory_estimate_min_samples`
  with a consistent deviation it stores an inferred `preference`
  `effort_factor`. `effort_factor(title, tags)` returns the soft multiplier.
  The original estimate is **never rewritten**.
* **Scheduler integration** — M5's optimizer consumes `effort_factor` as
  `TaskItem.effort_factor` (planning geometry only). A hard event or deadline
  always wins; memory can never override them.

## 11. User control & commands

Natural-language actions routed through `ExecutiveService`:

* `MEMORY_LEARN` — "Remember that I prefer …", "Note that I …"
* `MEMORY_QUERY` — "What do you remember about me?", "Show me my learned routines."
* `MEMORY_SEARCH` — "Search my memory for …"
* `MEMORY_EXPLAIN` — "Why do you think I prefer afternoons?"
* `MEMORY_FORGET` — "Forget my preference for studying at night."
* `MEMORY_CONFIRM` — "Confirm that …"
* `MEMORY_CORRECT` — "Actually I prefer mornings now."

A `forget` whose target is ambiguous returns `AMBIGUOUS` with candidates and
deletes nothing. Every mutation is audited.

## 12. Privacy & safety

Memory is sensitive durable state. The gate rejects:

* credentials/tokens/keys (API keys, private keys, bearer headers, JWTs,
  bot tokens, `password=`/`token=` patterns);
* instruction-like untrusted text (prompt injection);
* web content promoted to a personal `core_fact` or `user_instruction`.

Web-derived information stays `scope=external` with `provenance=web_verified`,
retains its source URL and expiry, and is never converted into a permanent
personal fact automatically. Arbitrary webpage text is not persisted.

## 13. MCP surface

Read-only tools (profile `readonly`): `memory_search`, `memory_get_relevant`,
`memory_list`, `memory_history`. `readonly` now exposes **30** tools; `full`
remains exactly **51**. Memory reads classify as `read`; memory mutations
(`memory_learn/forget/confirm/correct`) are reversible `low_risk_write` and are
audited. Write MCP tools were intentionally not added to preserve the frozen
51/27 profile counts.

The strictly read-only `executive_ask` MCP tool runs the service with
`read_only=True`: a memory mutation requested through it returns
`NEEDS_CONFIRMATION` with a candidate and **does not write**. The normal runtime
(Telegram/CLI) executes the same actions directly, because the user's explicit
utterance is itself the confirmation; every such write still passes the write
gate and is audited.

## 14. Database / migration

`memories`, `memory_evidence`, `memory_observations` are created with
`CREATE TABLE IF NOT EXISTS` (idempotent) and indexed. No existing table or row
is modified, so the migration is backward compatible.

## 15. Tests

`tests/run_acceptance_m6.py` (118 deterministic checks, frozen clocks) covers
storage, provenance/trust, safety, retrieval, learning, conflicts, user
commands, scheduler integration, project/course integration, web integration and
regression of the M3/M4/M5 surfaces and MCP profiles.
