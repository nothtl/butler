# Q4 semantic behavior + multi-turn report

commit: `fc43007` · model: `deepseek-chat` · tier: regression (59 cases, 65 calls, $0.02, 78s)

| Metric | Q2 baseline | Q4 (regression tier) |
|---|---|---|
| behavioral | 82% | 95.5% |
| task success | — | 96.6% |
| intent | 80% | 97.7% |
| clarification | 99% | 100.0% |
| temporal | 93% | 100.0% |
| follow-up | 85% | 83.3% (n=6) |

Safety gates: all 0.

## Changes

- only open a target clarification for server-detected ambiguity or real candidates
- clear stale entity mentions after a target is chosen (multi-slot advance)
- coerce out-of-enum scope.kind to unknown instead of rejecting the request
- add scope/policy/conditional few-shot examples
- scorer: intent only scored when an action is expected (temporal/follow-up excluded)
- regression corpus grows from real failures

## Remaining failures

- follow-up 83% on n=6 (wide CI; not statistically strong)
- one hard chain 'Track the assignments.'/'The CS188 ones.' still re-asks
- 'Use my pantry for this grocery topic.' -> settings_update (regression r-9)
