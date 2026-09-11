# Benchmark report

| Benchmark | Scope | Butler score | Measures | Does not map | Environment | Status |
|---|---|---|---|---|---|---|
| AgentBench FC | 10 FC-style cases | tool_choice 5/10 (50%); valid_action 10/10 | tool choice, registry validity | official AgentBench containers | local, offline | **ADAPTED** |
| ToolBench | 10 tool-selection cases | 9/10 (90%); valid_action 10/10 | intent → tool selection | official ToolBench API corpus | local, offline | **ADAPTED** |
| tau-bench (Butler-adapted multi-turn) | 200 two-turn chains | followup_consumed 170/200 (85%) | multi-turn state, follow-up consumption | official tau2 airline/retail/telecom + user simulator | local, offline | **ADAPTED** |
| tau-bench / tau2 | multi-turn tool+policy | — | task success, tool correctness, policy | official user simulator/domain DB/runner | not installed | **BLOCKED** |
| GAIA | general assistant research | — | planning, research, synthesis | official runner; Pi resource mismatch | not installed | **BLOCKED** |

## Notes

- Official repos cloned for evaluation (not runtime deps): `sierra-research/tau-bench @ 59a200c`, `sierra-research/tau2-bench @ 2174a60`, `THUDM/AgentBench @ d1e4a10`, `OpenBMB/ToolBench @ d56fdd8`. A tau2 isolated-venv install was attempted; the official CLI does not import cleanly here and, more fundamentally, Butler is not a tau2 agent and its domains differ.
- **No official external benchmark was executed.** The two ADAPTED rows are
  Butler-adapted suites, not the official benchmarks, and are labelled as such.
- The adapted scores measure tool selection/validity only; they do not measure
  multi-step task success, argument generation against official tools, or
  policy compliance in the official domains.
- tau-bench mapping (for a future compatible harness): user → Telegram/CLI,
  tools → action registry, policy → `SafetyPolicy`, state → Butler live state,
  conversation → `InteractionStore`, tool execution → `AgentRuntime`/domain
  service.
- GAIA must be run in a sandbox with the official runner before any capability
  claim; arbitrary destructive tools are never executed.
- Scores are never combined into a single "Butler score".
