# Clarification engine

One generic engine (`butler/agent/clarification.py`) fills missing or ambiguous
slots. There are no per-domain clarifiers.

## Model

```
ClarificationRequest
  interaction_id, slot_name, question, kind, options,
  free_text_allowed, default, topic_scope, created_at, expires_at, reason
```

Kinds: `CHOICE`, `TARGET`, `VALUE`, `TIME`, `SCOPE`, `CONFIRMATION`.
Clarification (missing information) and confirmation (approval) are separate
states and are never mixed.

## Slot schemas

The action registry (`butler/agent/actions.py`) declares `SlotSpec`s per action
(required / optional / defaultable / clarifiable), e.g.:

- `tracker_create`: required `target`, `event_types`; optional `cadence`,
  `destination`, `notification_policy`.
- `settings_update`: required `capability`, `state`, `scope`.
- `find_best_slot`: required `duration`; optional `target`, `time_window`.
- `create_item`: required `title`; optional `type`, `deadline`, `project`.

## Flow

```
DeepSeek request -> schema validation -> entity resolution
  -> required-slot check -> (safe default? fill) -> (missing? ask)
  -> dispatch / propose
```

Only genuinely missing required slots are asked; defaultable slots are filled
silently (no question spam).

## Buttons

Bounded answer spaces render inline buttons. Callback data is
`clar:<interaction_id>:<slot>:<option_id>`. A tap:

1. is authenticated and bound to user/chat/topic + interaction + slot;
2. looks the option up **server-side** (never trusts a client value);
3. rejects expired/stale/wrong-slot taps;
4. applies the slot and resumes the stored request — no re-interpretation.

## Natural-language answers

`parse_answer` resolves `first`/`second`, ordinals, numbers, exact labels,
option ids, qualifiers ("the CS188 one"), `none`/`cancel`, and free text where
allowed. A target answer must match a real candidate (never free text). An
unrecognised answer keeps the interaction and re-asks. A new, clearly
recognised request supersedes a stale clarification.

## State

Interactions live on the per-user session, expire (default 15 min), are
bounded per session, and are purged on access. Buttons never bypass the normal
safety path: slot -> revalidation -> safety -> confirmation -> execution.
