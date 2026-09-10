# Universal Creation & Organization (N3)

Status: implemented. Natural language is the interface; the existing domain
services remain the source of truth. N3 decides *what the user wants created,
linked or organized* and calls the right existing API. It is **not** a second
domain model and **not** a giant ontology.

```
user text -> operation -> topic/recent context -> resolution
          -> proposal -> safety/confirmation -> existing domain API
```

Module: `butler/creation.py::CreationService` (wired as `Container.creation`).

## 1. Adopted design ideas (research)

* **Home Assistant entity/service semantics** — resolve an entity then call a
  service; never invent a parallel model.
* **Mem0 / Khoj** — natural-language entity resolution with confidence and
  provenance; aliases without duplicate objects.
* **n8n** — a small create/update/link/organize vocabulary rather than a
  scripting language.
* **OpenClaw heartbeat/automation** — proposal → confirmation before side effects.
* **Butler N1/N2** — reuse `topic_links`, the tracker engine, memory and the
  existing safety/idempotency/audit path.

## 2. Operations

`create`, `resolve`, `link`, `update`, `organize`, `preview` (plus `track`,
`memory`, `schedule` as specialized creates). Each returns a structured
proposal/result: operation, target type, name, fields, links, resolution,
questions, confidence, confirmation, preview.

## 3. Resolution

Deterministic matching against live domain data (courses, projects, tasks,
food, grocery, recipes) plus the small `entity_aliases` bridge:

1. exact name/code; 2. normalized; 3. alias; 4. substring; 5. token overlap.
Context (topic links, focus, recent entities, topic name) is a **tie-breaker
only** — it never overturns an exact match. Result: `resolved` / `ambiguous` /
`unresolved`. Two equally exact matches are ambiguous (never silently merged).

## 4. Create vs update vs link

Before creating, N3 resolves the name. An existing high-confidence match turns a
`create` into an `update` (never a duplicate). Explicit "link/connect/put this
under" is a `link` (topic_links only). Ambiguity or a missing target returns
questions instead of guessing.

## 5. Deduplication

* food: existing name → increment quantity (one row).
* grocery: `db.add_shopping` is idempotent by name.
* project/task: an existing match updates in place.
* links: `topic_links` is unique on (topic, target_type, target_id, relation).
* aliases: unique on (target_type, target_id, alias).
Identity is deterministic and survives restarts.

## 6. Cross-topic sharing

Links are shared references by id (`topic_links`), never copied data. Food and
Groceries point at the same food record; CS188 and a Projects topic point at the
same course. Subsystems never call each other directly.

## 7. Relationships

A small human-readable set: `about`, `belongs_to`, `part_of`, `related_to`,
`requires`, `uses`, `stored_in`, `scheduled_for`, `replenishes`, `created_for`,
`derived_from`. Native domain relationships are used where they already exist.

## 8. Aliases / canonical names

`entity_aliases(target_type, target_id, alias, canonical, source, confidence)`
adds another name that resolves to an existing row (e.g. HW4 / Homework 4 /
Assignment 4 → CS188 HW4). Source terminology is preserved in provenance.

## 9. Organize & storage

`organize` calls the existing `Organizer`/`FileManager`; paths are validated
with `Engine.require_inside` (no traversal, no root changes). A proposal is
returned and requires confirmation; nothing moves without it. Batches are
summarized. Storage display is derived from the topic's linked data — files are
never duplicated because two topics reference them.

## 10. Documents / "save this"

A note is stored as a small M6 `core_fact` tagged `note` (topic-scoped when a
topic is present). Arbitrary content is **not** turned into long-term memory;
only explicit "remember that …" creates a preference.

## 11. Scheduling

"Schedule this" prepares a structured scheduling request and returns a
confirmation proposal. Actual scheduling stays with the executive/optimizer and
the existing calendar write path; N3 never writes Google Calendar.

## 12. Natural language examples

"Add a CS188 project due Friday around 8 hours." · "Add chicken to my pantry." ·
"Add milk to groceries." · "Create a task to finish the report." · "Save this
note about UAV navigation." · "Link this to CS188." · "Put this under
groceries." · "Organize this." · "Remember that I prefer 2-hour coding
sessions." · "Schedule two hours for CS188 tomorrow."

## 13. Relationship to N1 / N2 / memory / scheduling

* **N1** — topics provide context and `topic_links`; creation writes links there.
* **N2** — "track this" delegates to the tracker engine (`parse_request`/`create`).
* **Memory** — explicit preferences use M6; notes are tagged facts; no memory spam.
* **Scheduling** — proposals only; execution stays in the safety path.

## 14. Telegram UX

Canonical commands `/add`, `/link`, `/organize`; natural language is primary.
Inline `[Create] [Cancel]` for consequential proposals and `[choice]` buttons
for ambiguity; all callbacks are shape-validated. Messages are human-readable
and never expose internal ids.

## 15. MCP

Read-only tools `preview_create`, `resolve_reference`, `get_topic_context`,
`get_connections` (readonly 37; `full` stays 51). No mutating creation tool is
exposed read-only; mutations use the existing safety/permission/idempotency path.

## 16. Security

Authorization and the safety gate are unchanged. File paths are validated;
there is no delete API; no arbitrary SQL/shell; no secrets in messages; all
mutations are audited.

## 17. Non-goals

No universal object table, no giant ontology, no new domain engines, no second
scheduler/notifier/memory, no LLM-driven execution.

## 18. Tests

`tests/run_acceptance_n3.py` (165 deterministic checks) covers create,
resolution, update, link, dedup, organize, memory, safety, natural language,
cross-topic, ambiguity, providers, MCP and regression.
