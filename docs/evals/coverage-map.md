# Evaluation coverage map

Maps Butler code areas to the benchmark cases that exercise them. Tier 0
(offline) runs on every commit at zero cost; live tiers are opt-in.

| Code area | Deterministic coverage | Live corpus | Golden |
|---|---|---|---|
| Semantic interpretation | `run_acceptance_semantic_refactor`, `_hardening` | `intent.jsonl` (50) | `g-*` |
| Clarification / slots | `run_acceptance_clarification` (216) | `butler/clarification.jsonl` (200) | `g-amb-*` |
| Follow-up / conversation state | `run_acceptance_semantic_hardening` | `followup.jsonl` (100) | `g-fu-*` |
| Temporal | `run_acceptance_p72`, `_hardening` | `temporal.jsonl` (120) | `g-temp-*` |
| Safety / allowlist | `run_acceptance_p60`, `_hardening` | `safety.jsonl` | `g-safe-*` |
| Topics | N1, N4 | `butler/topics.jsonl` | `g-topics-*` |
| Tracking | N2 | `butler/tracking.jsonl` | `g-trk-*` |
| Creation / linking | N3 | `butler/creation.jsonl` | `g-*` |
| Scheduling | M5, p-suites | `butler/scheduling.jsonl` | `g-sch-*` |
| Food / groceries | N2/N3, p-suites | `butler/food.jsonl`, `groceries.jsonl` | `g-food-*`, `g-groc-*` |
| Courses / projects | M3, N3 | `butler/courses.jsonl`, `projects.jsonl` | `g-cross-*` |
| Memory | M6 | `butler/memory.jsonl` | `g-custom-2` |
| Files / organize | N3 | `butler/files.jsonl` | `g-custom-3` |
| Settings | N4 | `butler/settings.jsonl` | `g-set-*` |
| Proactive | M7 | `butler/proactive.jsonl` | — |
| Cross-topic | N1/N2/N3 | `butler/cross_topic.jsonl` | `g-cross-*` |
| MCP | `run_acceptance_mcp*` | — | — |
| CLI | p-suite CLI | — | — |

## Live tiers

| Tier | Calls | Purpose |
|---|---|---|
| 0 offline | 0 | every commit; all deterministic suites |
| 1 quick | ~15 | every semantic code change |
| 2 regression | ~50 | important semantic/prompt changes |
| 3 full | 450+ | release candidate / nightly (opt-in) |

External benchmarks (AgentBench FC, ToolBench, tau adapted) are offline
adapters; official tau2/GAIA remain BLOCKED.
