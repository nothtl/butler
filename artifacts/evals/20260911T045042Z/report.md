# Butler evaluation artifact — 20260911T045042Z

commit: `91d4953` · model: `deepseek-chat` · prompt: v2

## External benchmarks

| Benchmark | Status | Score |
|---|---|---|
| AgentBench FC (Butler-adapted) | ADAPTED | tool_choice 5/10 (50%), valid_action 10/10 |
| ToolBench (Butler-adapted) | ADAPTED | tool_selection 9/10 (90%), valid_action 10/10 |
| tau-bench (Butler-adapted multi-turn) | ADAPTED | followup_consumed 170/200 (85%) |
| tau-bench / tau2 (official) | BLOCKED |  |
| GAIA (official) | BLOCKED |  |

No official external benchmark was executed; ADAPTED rows are Butler-adapted suites.

## Live DeepSeek

- intent: {'n': 50, 'schema_valid': 50, 'strict': 40, 'lenient': 41, 'false_confident': 0}
- temporal: {'n': 100, 'correct': 93}
- ambiguity: {'n': 100, 'flagged': 100}
- followup: {'n': 100, 'resolved': 84}
- clarification: {'n': 100, 'offered': 99}
- latency_ms: {'n': 250, 'mean': 1079, 'median': 1083, 'p95': 1508}
- model: deepseek-chat
- endpoint: https://api.deepseek.com/v1

## Categories

1. LIVE DEEPSEEK — run_live_deepseek_eval.py (450+ calls)
2. DETERMINISTIC — 3569 checks
3. SYNTHETIC/FAKE — negative validation only
4. OFFICIAL EXTERNAL — none
5. ADAPTED EXTERNAL — see table
6. BLOCKED — tau2, GAIA
7. NOT APPLICABLE — GAIA domain/resource
