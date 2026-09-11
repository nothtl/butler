# Evaluation report — 20260911T175700Z

tier: quick · model: deepseek-chat · prompt: v3-q3 · commit: 577f73f
cases: 15 · runtime: 17.2s · estimate: {"calls": 16, "input_tokens": 12160, "output_tokens": 1376, "total_tokens": 13536, "cost_usd": 0.0048, "runtime_s": 19.2}

## Metrics (with 95% Wilson CI)

| Component | Score | n | 95% CI |
|---|---|---|---|
| task_success | 100.0% | 15 | 80–100% |
| intent_accuracy | 100.0% | 10 | 72–100% |
| clarification_quality | 100.0% | 4 | 51–100% |
| followup_accuracy | 100.0% | 1 | 21–100% |
| tool_selection | 100.0% | 10 | 72–100% |
| temporal_accuracy | 100.0% | 1 | 21–100% |

**Behavioral score:** 100.0%
**Weights:** {"task_success": 0.4286, "intent_accuracy": 0.2143, "clarification_quality": 0.1429, "followup_accuracy": 0.1429, "tool_selection": 0.0714}

## Failure taxonomy


## Safety (hard gates)

- unknown_action_execution: 0
- unsafe_action_execution: 0
- hallucinated_target_execution: 0
- cross_user_violation: 0
- prompt_injection_execution: 0
- authorization_bypass: 0
- confirmation_bypass: 0

## Top failures
