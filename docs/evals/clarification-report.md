# Clarification report

## Live DeepSeek (real calls)

| Metric | Baseline (2028a9f) | Current |
|---|---|---|
| schema validity | 100% | **100%** |
| strict semantic accuracy | ~82% | **80%** |
| clarification offered (missing/ambiguous) | n/a | **100% (50/50)** |
| follow-up resolved | ~80% | **90%** |
| temporal accuracy | ~98% | **94%** |
| ambiguity flagged | 100% | **100%** |
| mean latency | ~1.03 s | **1.03 s** |
| p95 latency | ~1.31 s | **1.32 s** |
| failures | 0 | **0** |

Strict intent accuracy is flat within model variance; the milestone improves
the interaction layer (clarification + follow-up), not raw classification.

## Deterministic

- `run_acceptance_clarification.py`: **216 checks, 0 failed** (slot schemas,
  missing-slot detection, parse/apply, 7 required scenarios, isolation,
  concurrency, restart, expiry, bounded state).
- Full aggregate: **38 suites, 3518 checks, 0 failed**.

## Clarification quality

| Metric | Value |
|---|---|
| clarification offered when required | 100% (live) |
| wrong candidate selection | 0 in deterministic suite |
| unrecognised answer → re-ask (state kept) | yes |
| expired interaction execution | 0 |
| cross-user resume | rejected |
| stale button (second press) | rejected |
| open interactions per session | bounded (≤4) |

## Hard safety gates

unknown-action execution 0 · unsafe execution 0 · hallucinated-target
execution 0 · cross-user violation 0 · prompt-injection execution 0.

## Notes

- Confirmation vs clarification are separate states; buttons never bypass the
  safety path.
- The tracker `event_types` slot is required; a condition (e.g. food threshold)
  satisfies it, avoiding question spam.
