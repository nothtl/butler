# Benchmarking

Butler's evaluation layer lives entirely under `tests/evals/` and
`tests/evals/benchmarks/`. **The production code knows nothing about any
benchmark**, and no external benchmark is a runtime dependency.

## Result categories (never conflated)

| Category | Meaning |
|---|---|
| 1. REAL DEEPSEEK CALL | a real network call to the configured endpoint |
| 2. DETERMINISTIC TEST | offline, part of the acceptance gate |
| 3. SYNTHETIC/FAKE MODEL TEST | a fake client used only for negative tests |
| 4. OFFICIAL EXTERNAL BENCHMARK | the official runner actually executed |
| 5. BUTLER-ADAPTED BENCHMARK | a Butler-specific mapping, not official |
| 6. BLOCKED / NOT APPLICABLE | cannot run (env/resource/domain mismatch) |

## Adapter interface

`tests/evals/benchmarks/adapter.py`:

```
BenchmarkAdapter
  load_cases() -> list[dict]
  build_context(case) -> dict
  invoke_agent(case, context) -> Any
  evaluate(case, output) -> dict
  summarize(results) -> BenchmarkResult
```

`BenchmarkResult` records name, status, scope, score, what it measures, what
does not map, environment, version, runner, deviations and limitations.

## Adapters

| Adapter | Status |
|---|---|
| AgentBench FC | **ADAPTED** — FC-style selection against the action registry |
| ToolBench | **ADAPTED** — intent -> tool selection |
| tau-bench / tau2 | **BLOCKED** — official runner/domain not installed |
| GAIA | **BLOCKED** — official runner/resource mismatch |

Run: `.venv/bin/python tests/evals/benchmarks/run_benchmarks.py`

## Corpora

`tests/evals/` holds the benchmark corpora (`intent`, `followup`, `temporal`,
`ambiguity`, `safety`). `tests/evals/butler/` holds the unified Butler corpus
(single_turn, multi_turn, clarification, ambiguity, temporal, safety, plus
topics/tracking/creation/settings/courses/projects/food/groceries/calendar/
scheduling/memory/files/cross_topic/proactive).

## Model independence

Adapters call the semantic path through the same typed contract, so the same
corpus can be run against a different OpenAI-compatible model without changing
Butler.

## Adopted principles (research)

Synthesised from current public guidance (Rasa forms/slot filling; Home
Assistant Conversation API; Microsoft Semantic Kernel function calling;
DeepSeek strict tool calling; OWASP LLM Excessive Agency; NIST AI RMF):

- **Typed slots** with explicit required/optional/defaultable/clarifiable sets.
- **Persisted, expiring conversation state** (InteractionStore) as the
  authoritative source for clarifications/confirmations.
- **Finite-choice buttons** for bounded answer spaces; server resolves
  candidate ids (never the client/model).
- **Structured tool arguments** validated server-side; DeepSeek strict tool
  calling is supported but not the measured default (see A/B above).
- **Downstream authorization / least privilege / complete mediation**: every
  action is on an allow-list; external/destructive actions require
  confirmation; fail-closed on the unknown (OWASP Excessive Agency).
- **Quantitative evaluation** with separated categories and documented
  limitations/uncertainty (NIST AI RMF: validity, reliability, safety,
  security, resilience, transparency, regular testing).
