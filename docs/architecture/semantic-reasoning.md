# Semantic reasoning

DeepSeek is the **primary semantic interpreter** when `[ai]` is configured.
It is an interpreter, never an executor.

```
user input
  -> bounded live context + relevant action schemas
  -> DeepSeek structured request   (Chat.complete(json_mode=True))
  -> schema validation             (AgentRequest.from_dict(strict=True))
  -> server-side entity resolution (ExecutiveService._resolve)
  -> deterministic validation + safety policy
  -> confirmation where required
  -> domain service
  -> audit / idempotency
  -> result
  -> DeepSeek explanation
```

## Roles

| Layer | Responsibility |
|---|---|
| DeepSeek | intent, entities, topic purpose, capability/tracker/creation/settings interpretation, ambiguous-reference interpretation, response phrasing |
| Deterministic | scheduling geometry, trigger evaluation, safety, authorization, DB truth, external-effect permission, state transitions, temporal resolution, entity resolution |

The model never: executes tools, bypasses `AgentRuntime`/`SafetyPolicy`,
chooses hard schedule positions, authorizes, selects another user's target,
constructs SQL/paths, or invokes a shell.

## Structured output

DeepSeek's OpenAI-compatible endpoint supports JSON mode, not full strict
schema. `LLMInterpreter` embeds the schema (`butler/agent/schema.py`) in the
system prompt, requests `response_format={"type":"json_object"}`, parses with
`json.loads` (no prose mining), validates strictly, retries once with a repair
prompt, and **never executes malformed output** (the deterministic fallback is
used instead).

The prompt includes disambiguation examples (QUERY vs CREATE vs UPDATE, TRACK
vs QUERY, SETTINGS vs SCHEDULE, LINK vs CREATE) and a bounded, topic-relevant
action hint (`relevant_actions`). The full enum remains authoritative and every
proposal is validated against the complete registry.

## Target resolution

`LLMInterpreter._sanitize` clears every model-supplied `id`/`resolved`. The
server resolves names against live state:

```
exact id/code -> exact scoped name -> aliases -> topic-linked candidates
-> bounded semantic search -> ambiguity / ask
```

## Fallback

`HybridInterpreter` uses a narrow deterministic fast path (confidence ≥ 0.9 and
a concrete action) and otherwise the model; on no-key/error/rejection it falls
back to `DeterministicInterpreter`. Exact slash commands never reach the
interpreter.

## Files

`butler/agent/semantic.py` (contract), `schema.py` (schema/prompt),
`interpret.py` (LLM + hybrid), `service.py` (resolution/dispatch),
`actions.py` (registry), `chat.py` (JSON mode).
