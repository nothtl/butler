# Safety policy (fail-closed)

Butler's safety gate is the deterministic boundary between a proposed action and
execution. It is **fail-closed**: anything not explicitly registered is denied.

## Action registry

`butler/agent/actions.py` is the single source of truth for every executable
action. Each `ActionDefinition` carries:

| Field | Meaning |
|---|---|
| `name` | canonical action name |
| `risk_class` | one of the classes below |
| `side_effect` | whether it mutates state |
| `requires_confirmation` | whether consent is required |
| `allowed_scopes`, `required_permissions` | future policy hooks |
| `description`, `when_to_use`, `when_not_to_use`, `examples` | model-facing docs |

Risk classes: `READ_ONLY`, `LOCAL_MUTATION`, `SCHEDULE`, `EXTERNAL_WRITE`,
`DESTRUCTIVE`, `PRIVILEGED`.

Every tool action, every `ActionKind`, and every decider intent kind is
registered; a regression test asserts this so the registry cannot drift.

## Decision table

```
unknown action / unregistered      -> DENY
unknown risk class                 -> DENY
READ_ONLY (registered)             -> ALLOW
LOCAL_MUTATION (registered)        -> ALLOW (Butler-owned, reversible)
EXTERNAL_WRITE unconfirmed         -> DENY (confirmation required)
DESTRUCTIVE unconfirmed            -> DENY (confirmation required)
PRIVILEGED                         -> DENY always (never executable)
degraded mode: external            -> DENY
degraded mode: non-allowlisted write -> DENY
```

## Why this is not a deny-list

The old policy allowed any `CONSEQUENT_EXTERNAL` action that was not in a
hand-maintained `_CONFIRM_REQUIRED` set — so `telegram_send`, `ha_call`, `rm`,
`shell`, and any unknown name were allowed unconfirmed. The new policy derives
the decision from the allow-list registry: an action is allowed only if it is
registered, and external/destructive actions require confirmation by
construction.

## Confirmation flow

A confirmation-required verdict returns `allow=False` with
`needed="user confirmation to run '<action>'"`. `AgentRuntime` parks a
`PendingAction`; the next confirmed turn executes it. The decider bridge
defers confirmation to the plan/confirm layer.

## Invariants

- The LLM cannot execute anything; it only proposes a typed `AgentRequest`.
- Model-supplied entity ids are discarded and re-resolved server-side.
- Inferred constraints can never be hard.
- High model confidence cannot override ambiguity or safety.
- Unknown external actions allowed = **0** (hard gate).
