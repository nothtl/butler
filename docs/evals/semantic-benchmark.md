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
