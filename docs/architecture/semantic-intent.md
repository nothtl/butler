# Semantic intent (DeepSeek structured interpretation)

This document describes the semantic-intent architecture and the inventory of
hardcoded natural-language routing.

## Architecture

```
user text
  -> semantic interpreter  (DeepSeek structured output, when configured)
  -> typed AgentRequest    (butler/agent/semantic.py)
  -> strict schema validation (AgentRequest.from_dict(strict=True))
  -> live entity resolution (ExecutiveService._resolve, server-side only)
  -> deterministic domain logic (ExecutiveService._dispatch)
  -> safety / idempotency / audit
  -> execute
  -> LLM-phrased explanation (butler/chat.py)
```

The model is an *interpreter*, never an executor. It cannot call tools, choose
schedule positions, authorize actions, or supply entity ids. Those stay in the
deterministic layer.

### Components

| Piece | File | Role |
|---|---|---|
| Request contract | `butler/agent/semantic.py` | `AgentRequest`, `ActionKind`, `EntityRef`, `Scope`, `Constraint`, `TemporalRange`, `AgentResult` |
| Schema | `butler/agent/schema.py` | JSON schema + prompt derived from the contract |
| Semantic interpreter | `butler/agent/interpret.py::LLMInterpreter` | structured output -> validated `AgentRequest` |
| Hybrid router | `butler/agent/interpret.py::HybridInterpreter` | semantic-first, narrow deterministic fast path, deterministic fallback |
| Factory | `butler/agent/interpret.py::resolve_interpreter` | picks semantic when `[ai]` is configured |
| Execution | `butler/agent/service.py::ExecutiveService` | validation, server-side resolution, deterministic dispatch |
| Response phrasing | `butler/chat.py::complete` | `json_mode=True` for interpretation; free text for replies |

### Structured output

DeepSeek's OpenAI-compatible endpoint supports JSON mode
(`response_format={"type": "json_object"}`), not full strict JSON schema, so:

1. the full schema is embedded in the system prompt (`schema_prompt()`);
2. the response is parsed with `json.loads` (no prose mining);
3. it is validated with `AgentRequest.from_dict(strict=True)`;
4. a malformed response is retried once with a repair prompt;
5. a still-invalid response is **never** executed — the deterministic fallback
   is used instead.

### Server-side resolution

`LLMInterpreter._sanitize` clears every model-supplied `id`/`resolved` flag.
`ExecutiveService._resolve` then matches names against live state and returns
`RESOLVED` / `AMBIGUOUS` / `NOT_FOUND`. A hallucinated id or name can never
select a record.

### Temporal handling

The model returns a `temporal.phrase` (e.g. `"tomorrow afternoon"`); the
deterministic `TemporalResolver` owns the clock. The model never invents
timestamps.

## Inventory of hardcoded NL routing

Classification legend: **KEEP** technical/safety; **FALLBACK** deterministic
fallback (retained, no longer primary); **LEGACY** old router to retire;
**REPLACE** replaced by the semantic path.

| Location | What | Class |
|---|---|---|
| `butler/agent/interpret.py` | 48 keyword tables (`_ADVISE`, `_TRACKER_*`, `_MEMORY_*`, `_WEB_*`, `_PROJECT_*`, ...) | FALLBACK |
| `butler/agent/interpret.py` | 4 `re.compile` (project effort, URL) | KEEP |
| `butler/decider.py` | ~100 regex operations, first-match-wins intent router | LEGACY |
| `butler/topics.py` | `_CAP_HINTS`, `_PURPOSE_HINTS`, `_COURSE_RE` | FALLBACK (purpose/capability suggestion) |
| `butler/tracking.py` | 7 keyword tables + 3 regex (source/condition names) | FALLBACK / KEEP |
| `butler/creation.py` | 7 keyword tables + 5 regex (link/create verbs, numbers) | FALLBACK / KEEP |
| `butler/memory.py` | 2 tables + 18 regex (extraction, temporal) | FALLBACK / KEEP |
| `butler/affinity.py` | 2 category tables (study/exercise/...) | FALLBACK (fast/cheap, not per-scheduler-call LLM) |
| `butler/course.py` | HTML/spec extraction patterns | KEEP (technical extraction) |
| `butler/telebot.py` | `_capability_change` word map (9 entries) | FALLBACK |
| `butler/telebot.py` | callback `topic:...` parsers | KEEP (protocol) |
| `butler/settings.py` | `SPECS` whitelist | KEEP (validation) |

### Removed / replaced in this pass

- NL routing is now **semantic-first** when `[ai]` is configured. The legacy
  keyword tables are no longer the primary path; they are the fallback.
- `LLMInterpreter` no longer mines JSON out of prose; it uses JSON mode and
  strict parsing.
- Model-supplied entity ids are discarded.
- Added a bounded negation fallback (`don't track`, `never notify me`).

No keyword table was deleted yet: the deterministic fallback is required and
the corpus locks its coverage. Removal is staged (see below).

## Before / after

| Metric | Before | After |
|---|---|---|
| Primary NL router | deterministic first-match-wins chain | DeepSeek structured interpretation |
| Fallback | the same chain | the same chain, explicitly fallback-only |
| Human-intent keyword tables | 48 (interpret) + decider router | unchanged count, no longer primary |
| Model JSON recovery | `find("{")`/`rfind("}")` prose mining | JSON mode + strict parse + one repair |
| Model-supplied ids trusted | n/a (LLM path unused) | never |
| New phrasing needs code | yes | no (live path), fallback unchanged |

## Two-level routing / cost

`HybridInterpreter` calls the model only when the deterministic interpreter is
not already confident (`confidence >= 0.9` and a concrete action) or is
unknown. Exact slash commands never reach the interpreter. This keeps the
common/obvious cases cheap while making the model the decider for free-form
language.

## Safety invariants (unchanged)

- The model cannot execute tools, bypass `AgentRuntime`, `SafetyPolicy`,
  confirmation, authorization, hard schedule positions, or other users'
  targets.
- Inferred constraints can never be hard (enforced structurally).
- Confidence is advisory; validation and safety are independent.
- Web content is untrusted data (prompt-injection safe).
- Memory persistence remains gated deterministically.

## Tests

- `tests/nl_intent_corpus.json` — 320 utterances (222 deterministic-covered,
  98 semantic-only).
- `tests/run_acceptance_semantic_refactor.py` — 534 deterministic checks
  (corpus, schema validation, entity resolution, ambiguity/follow-up, LLM
  failure/hallucination, routing).
- `tests/run_live_deepseek_semantic.py` — optional live DeepSeek smoke test
  (real HTTP, JSON mode, reports status/latency). Not part of the deterministic
  gate.

## Staged removal of legacy routers

The legacy tables are retained as the required fallback. To remove them safely:

1. raise offline corpus coverage of the semantic path (record live outcomes);
2. delete tables the semantic path provably covers, keeping the corpus green;
3. reduce `butler/decider.py` to exact/protocol parsing only;
4. re-point `DeterministicInterpreter` at the trimmed fallback.
