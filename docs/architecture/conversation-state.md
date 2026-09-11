# Conversation state

Follow-ups must not depend on the model's memory. Clarifications and pending
proposals are explicit, expiring state (`butler/agent/interactions.py`).

## Interaction

```
id, kind (clarification|confirmation), status,
original_text, request, candidates, proposal,
expected_slot, ambiguity_type, created_at, expires_at
```

Statuses: `ACTIVE`, `WAITING_FOR_CLARIFICATION`, `WAITING_FOR_CONFIRMATION`,
`COMPLETED`, `CANCELLED`, `EXPIRED`. Stored on the per-user session
(`session.data["interactions"]`), bounded and expiring (default 15 min).

## Resolution precedence (P2.3)

```
1. active clarification
2. active confirmation / proposal
3. current topic
4. recent entities
5. recent conversation
6. bounded global lookup
7. ask
```

## Ambiguity (P2.1)

When resolution yields multiple candidates, Butler opens a clarification with
**enriched** candidates (e.g. `Project 2 (CS188)`). The next turn is matched
**only against those candidates** — a unique qualifier match wins, then a
unique name match, then an ordinal ("the second one"). Global search is never
attempted first.

## Pending proposal modification (P2.2)

A mutating result that produces a proposal (or a tracker) opens a confirmation
interaction. A follow-up detected as a modification (an UPDATE action, or
"only/just/instead/actually/…") is merged into the pending proposal and returns
`NEEDS_CONFIRMATION` — it does not create a second, unrelated action.

## Expiry (P2.4)

Expired interactions are purged on access and never applied. A short follow-up
after expiry carries: *"That earlier choice has expired. Please choose again."*

## Example

```
User:  Add Project 2.
Butler: I found two Project 2s — Project 2 (CS188) / Project 2 (CS168)?
User:  The CS188 one.
Butler: resolves Project 2 (CS188)
```
