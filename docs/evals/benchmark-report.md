# Benchmark report

| Benchmark | Scope | Butler score | Measures | Does not map | Environment | Status |
|---|---|---|---|---|---|---|
| AgentBench FC | 10 FC-style cases | tool_choice 5/10 (50%); valid_action 10/10 | tool choice, registry validity | official AgentBench containers | local, offline | **ADAPTED** |
| ToolBench | 10 tool-selection cases | 9/10 (90%); valid_action 10/10 | intent → tool selection | official ToolBench API corpus | local, offline | **ADAPTED** |
| tau-bench / tau2 | multi-turn tool+policy | — | task success, tool correctness, policy | official user simulator/domain DB/runner | not installed | **BLOCKED** |
| GAIA | general assistant research | — | planning, research, synthesis | official runner; Pi resource mismatch | not installed | **BLOCKED** |

## Notes

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
