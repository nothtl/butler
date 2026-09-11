# Q2 semantic quality report

commit before: `ed4c30e` · model: `deepseek-chat` · prompt: v2

| Metric | Before | After |
|---|---|---|
| strict | 0.8 | 0.8 |
| lenient | 0.82 | 0.82 |
| behavioral | 0.82 | 0.82 |
| followup | 0.84 | 0.85 |
| clarification | 0.99 | 0.99 |
| ambiguity | 1.0 | 1.0 |
| temporal | 0.93 | 1.0 |
| median_ms | 1080 | 1154 |
| p95_ms | 1510 | 1591 |
| failures | 1 | 0 |

## Failure taxonomy (before)

- WRONG_INTENT: 9
- TAXONOMY_ONLY: 5
- FOLLOWUP_FAILURE: 15
- MISSED_CLARIFICATION: 1
- WRONG_TIME: 8
- MODEL_FAILURE: 0
- SERVER_VALIDATION_FAILURE: 1
- UNSAFE: 0

## Changes

- follow-up: always consult the stored candidate set before superseding
- match_candidates: prefer a unique exact-name match over a qualifier match
- temporal evaluation uses a reproducible reference clock (10:00 local)
- unknown entity types coerced to unknown (name preserved)

## Safety

- unknown_action_execution: 0
- unsafe_execution: 0
- hallucinated_target_execution: 0
- cross_user_violation: 0
- prompt_injection_execution: 0

## Limitations

- follow-up 85% (<95% target); hard chains re-ask the next required slot
- strict semantic 80% (<90%); residual failures are taxonomy disputes
- temporal 100% is measured against a fixed reference clock
- official external benchmarks remain BLOCKED
