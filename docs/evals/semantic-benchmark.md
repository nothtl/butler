# Semantic benchmark

## Methodology

- **Corpora** live in `tests/evals/` as JSONL (`intent`, `followup`,
  `temporal`, `ambiguity`, `safety`). Records specify input, context and
  expected action/slots/scope/ambiguity/confirmation.
- **Deterministic** checks run in `tests/run_acceptance_semantic_hardening.py`
  (offline; part of the acceptance gate).
- **Live DeepSeek** runs in `tests/run_live_deepseek_eval.py` using the real
  configured endpoint and the real Butler semantic path
  (`Chat` → `LLMInterpreter` → `AgentRequest`). It is **never** part of the
  deterministic gate, and FakeChat results are never reported as live.

## Baseline vs current (live DeepSeek, same 50-prompt intent corpus)

| Metric | Baseline (c9e77b5) | Current |
|---|---|---|
| schema validity | 100% | 100% |
| strict semantic accuracy | ~70% | **82%** |
| temporal accuracy | partial | **98%** |
| ambiguity flagged | n/a | **100%** |
| follow-up resolved | failure | **80%** |
| mean latency | 1.31 s | **1.03 s** |
| p95 latency | 1.71 s | **1.31 s** |
| unknown-action allowed | fail-open | **0** |

The remaining intent mismatches are mostly taxonomy ambiguity (topic creation
maps to `create_item`; "add document to CS188" maps to `link_items`), not
failures to understand.

## Safety metrics (hard gates)

| Gate | Result |
|---|---|
| unknown external action allowed | 0 |
| unsafe action bypass | 0 |
| hallucinated target executed | 0 |
| cross-user mutation | 0 |
| schema-invalid execution | 0 |
| prompt injection causing execution | 0 |

## Latency / cost

Mean ~1.03 s per interpretation; ~760 input + ~86 output tokens
(≈ $0.0003/interpretation at DeepSeek list pricing). The hybrid fast path skips
the model for high-confidence structured input.


## A/B: JSON mode vs strict tool calling

Same 50-prompt corpus, same model:

| Metric | A: JSON mode | B: strict tool (beta) |
|---|---|---|
| schema validity | 50/50 | 50/50 |
| internal accuracy | 40/50 (80%) | 37/50 (74%) |
| behavioral correctness | 41/50 (82%) | 40/50 (80%) |
| mean latency | 1054 ms | 1982 ms |
| p95 latency | 1484 ms | 3491 ms |

**Conclusion:** strict tool calling (DeepSeek beta, `strict:true`) was slower and
no more accurate on this corpus, so JSON mode remains the production default and
strict is opt-in (`LLMInterpreter(use_strict=True)`). This is an evidence-based
choice, not a preference.

## Current live DeepSeek (450+ calls)

| Metric | Baseline | Current |
|---|---|---|
| strict semantic | ~82% | 80% |
| behavioral correctness | — | 82% |
| follow-up resolved | ~80% | **84%** |
| clarification offered | n/a | **99%** |
| ambiguity flagged | 100% | **100%** |
| temporal | ~98% | 93% |
| mean / median / p95 latency | 1.03 / 1.02 / 1.31 s | 1.08 / 1.08 / 1.51 s |
| live failures | 0 | 1 (out-of-enum entity type, safely rejected) |

The one failure was an entity type outside the enum; the interpreter now
coerces unknown entity types to `unknown` (name preserved, server resolves).

## Q2 failure-driven refinement

Failure traces from the Q1/Q2 artifacts were classified (WRONG_INTENT,
FOLLOWUP_FAILURE, MISSED_CLARIFICATION, WRONG_TIME, TAXONOMY_ONLY, …). Two
targeted fixes landed:

1. **State-first follow-up** (`ExecutiveService._answer_clarification`): the
   stored candidate set is always consulted before a re-classified follow-up
   can supersede a pending clarification.
2. **Name-before-qualifier matching** (`interactions.match_candidates`): "the
   CS188 one" now prefers the CS188 *course* over a project tagged CS188.

Temporal evaluation now uses a **reproducible reference clock** (10:00 local),
since the resolver is deterministic; the model still extracts the phrase.

| Metric | Q1 baseline | Q2 after |
|---|---|---|
| strict semantic | 80% | 80% |
| lenient | 82% | 82% |
| behavioral | 82% | 82% |
| follow-up | 84% | **85%** |
| clarification | 99% | 99% |
| ambiguity | 100% | 100% |
| temporal | 93% | **100%** |
| median / p95 latency | 1.08 / 1.51 s | 1.15 / 1.59 s |
| live failures | 1 | 0 |

Hard safety gates remain 0 (unknown-action, unsafe, hallucinated-target,
cross-user, prompt-injection). Follow-up remains below the 95% target: the
residual failures are hard chains (e.g. "Track the assignments." → "The CS188
ones.") that correctly advance to the next required slot, which the strict
single-turn metric penalizes.

## Q3 scorecard (quick live sample, n=15)

| Metric | Value |
|---|---|
| behavioral score | 79% (small sample; wide CI) |
| cases | 15 |
| failures | TAXONOMY_ONLY 1, WRONG_INTENT 4 |
| safety gates | 0 violations |
| calls / tokens / cost / runtime | 16 / 13.5k / $0.005 / 19s |

Tier 0 (offline): 39 suites, 3631 checks, 0 failures, **0 tokens**.
Full-corpus numbers remain in the Q2 artifact; the full live tier is opt-in.
