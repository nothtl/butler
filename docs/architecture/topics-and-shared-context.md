# Universal Topics & Shared Context (N1)

Status: implemented. A Telegram forum topic is a durable **TopicProfile** — a
lightweight *context/view* over Butler's existing domain data, not a routing
destination and not a separate datastore.

Core principle:

```
TOPIC = CONTEXT
DOMAIN DATA = SOURCE OF TRUTH
```

## 1. Why

Before N1 a Telegram topic was a thin settings row (`topic_settings`) plus an
in-memory "seen topics" cache, and free text in a topic could be forced into a
hardcoded default intent (`routing`). N1 makes the topic tell Butler **what the
conversation is about** and **which existing data is relevant**, while the
semantic interpreter still decides the actual intent. The data stays in courses,
projects, tasks, food, etc.

There is deliberately **no graph database** and **no universal ontology**. Links
are a small bridge table over existing records.

## 2. TopicProfile

Stored by evolving the existing `topic_settings` table (idempotent `ALTER TABLE`
migration); no parallel table and no duplicate rows.

| Field | Meaning |
|-------|---------|
| `id`, `chat_id`, `thread_id` | identity (unique chat+thread) |
| `topic` (name) | resolved topic title |
| `purpose` | short human label ("Course management") |
| `description` | the user's own words |
| `status` | `pending_setup` \| `active` \| `paused` \| `archived` |
| `capabilities` | JSON `{capability: state}` |
| `template` | optional preset name |
| `created_at`, `updated_ts`, `last_seen_at` | timestamps |
| `pin_message_id`, `pin_message_version`, `pin_content_hash` | panel projection |
| `routing`, `push_on`, `push_time`, `push_freq` | legacy per-topic settings (kept) |

## 3. Discovery

When a message arrives from a forum topic Butler has never seen:

1. create a `pending_setup` TopicProfile (keyed by chat+thread);
2. resolve the topic name from Telegram;
3. reply: *"New topic detected: <name>. What is this topic for?"*;
4. the user's next message is interpreted into purpose + capabilities + links;
5. the profile becomes `active` and a pinned control panel is created.

Slash commands never consume the setup reply. An unauthorized user is denied
before any profile is created.

## 4. Capabilities

```
knowledge  tracking  memory  planning  scheduling
reminders  proactive  web  file_organization
```

States: `enabled`, `disabled`, `paused`, `degraded`, `not_applicable`.
Capabilities are **suggested** deterministically from the description (keyword
hints) and are only configuration/context in N1 — there is **no tracker engine**
here (that is N2). Defaults: knowledge/memory/planning/scheduling/proactive on;
tracking/reminders/web/file_organization opt-in from the description. Negative
phrasing ("don't track…") disables a capability.

## 5. Linking (shared data, no duplication)

`topic_links(topic_profile_id, target_type, target_id, relation, confidence,
provenance, …)` references existing records by id. Deterministic matching:

* **courses** by code token or name;
* **projects** by exact/normalized name — several matches are returned as
  **ambiguous** and Butler asks rather than guessing;
* **food / meal plan** by keyword (contextual references, id 0).

Two topics that reference the same course/project point at the **same** record.
Nothing is copied. Explicit links and template-based duplication copy
*configuration*, never data.

## 6. Topic context (relevance, not a boundary)

`AgentRequest.topic` (chat_id/thread_id) is resolved by `ContextBuilder` into
`ContextSnapshot.topic` + `ContextSnapshot.topic_context`. Linked courses,
projects and tasks are marked `topic_relevant` and sorted first — a **boost**.
Global questions ("what deadlines do I have this week?") still see all data.
An inactive topic contributes no linked data.

## 7. Memory scoping

The single M6 memory store is reused. Topic-scoped memory is stored with a
`topic:<name>` tag. Retrieval always includes global memory and *adds* matching
topic-tagged memory on top — never a hard boundary, never a second store.

## 8. Pinned control panel

One Butler-owned message per active topic, pinned. The database is the source of
truth; the Telegram pin is a rendered projection. `render_panel` returns
`(text, content_hash)`. The panel is edited in place when the hash changes; if
the message is missing/uneditable it is replaced and re-pinned. The content hash
prevents unnecessary edits, and `pin_message_version` records the projection
revision. On startup `reconcile_topics` repairs missing panels and refreshes
stale ones without creating duplicates.

Panel contents (concise, never raw rows): purpose, capability icons, connected
data, current items, storage path, last-updated, and action buttons
`[⚙ Settings] [🔗 Connections] [📋 Details]`.

## 9. Views

* **Settings** — purpose, capability toggles, notification behavior, memory scope.
* **Connections** — linked courses/projects/tasks/food with counts.
* **Details** — purpose, status, capabilities and provenance ("why is tracking
  enabled?" → the user's own description).
* **Storage** — the derived course folder and connected file count.

System-wide settings remain separate (the `/settings` system overview).

## 10. Natural language

Preferred over slash commands: *"Create a topic called CS188."*, *"This is my
CS188 course. Track homework and projects."*, *"Connect groceries to my food
inventory."*, *"Stop scheduling in this topic."*, *"Enable tracking."*,
*"Create another topic like CS188."* Core commands stay minimal: `/topics`,
`/topic`, `/settings`, `/help`.

## 11. Duplicate-routing cleanup

The legacy per-topic `routing` (default intent) remains **only as a fallback**
when free text matches nothing. Topic context is now the primary behavior. The
duplicate `/resume` command registration was removed. Existing topic rows and
legacy columns are preserved by the migration.

## 12. Migration

`topic_settings` is extended in place (idempotent `ALTER TABLE`); existing rows
default to `status='active'` and keep working. `topic_links` is added. A
realistic pre-N1 database migrates with no data loss.

## 13. Non-goals (N1)

* No graph database, no universal object ontology.
* No generic tracking/rules engine (N2).
* No rewrite of courses/projects/food/scheduler/memory.
* No change to the NAS/file subsystem.

## 14. Tests

`tests/run_acceptance_n1.py` (134 deterministic checks) covers discovery,
profiles/capabilities, context, linking (incl. ambiguity and no duplication),
the pinned panel (edit/replace/reconcile), UX views, security, memory scoping,
backward-compatible migration and realistic scenarios.
