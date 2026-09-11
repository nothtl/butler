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

## Q3: tiers, guards, budgets, caching, replay

The evaluator is now cost-controlled. Live DeepSeek is never the default.

```
python tests/run_live_deepseek_eval.py --allow-live --quick       # ~15 calls
python tests/run_live_deepseek_eval.py --allow-live --regression  # ~50 calls
python tests/run_live_deepseek_eval.py --allow-live --full        # 450+ (needs --full)
python tests/run_live_deepseek_eval.py --replay <run-id>          # 0 calls, re-score
python tests/run_live_deepseek_eval.py --allow-live --suite intent --sample 20 --seed 42
```

- **Guard:** every live run requires `--allow-live`; the full run also requires
  `--full`. Default test commands never call DeepSeek.
- **Budgets:** `--max-calls/--max-tokens/--max-cost/--max-runtime` with a
  preflight estimate; exceeding a budget stops with `BUDGET_EXCEEDED`.
- **Sampling:** `--sample N --seed S` does deterministic **stratified**
  round-robin sampling (all strata represented), not random.
- **Caching:** responses keyed by case+model+provider+prompt/schema/context/
  actions hashes+temperature; a prompt/model/context change invalidates.
  Production interactions are never cached.
- **Replay:** `--replay <run-id>` re-scores stored outputs with new scoring
  logic at zero token cost.
- **Metrics:** Wilson 95% CIs, absolute/relative deltas with CI-overlap
  detection (no "improvement" claimed for noisy deltas), a weighted behavioral
  score, and a hard safety gate reported separately.
- **Artifacts:** `artifacts/evals/<run-id>/{results.json,report.md}` with
  metrics, CIs, failure taxonomy, top failures and safety gates.

## Behavioral score (documented weights)

```
0.30 task_success + 0.15 intent + 0.15 target + 0.10 slot
+ 0.10 clarification_quality + 0.10 followup + 0.05 tool_selection
+ 0.05 confirmation
```

Safety is a hard gate, never averaged in. Internal enum accuracy is reported
separately from behavioral correctness (taxonomy-only mismatches do not
dominate).
