"""Q3 live DeepSeek evaluation CLI (guarded + tiered).

Live DeepSeek is never the default. Examples:

  python tests/run_live_deepseek_eval.py --allow-live --quick
  python tests/run_live_deepseek_eval.py --allow-live --regression
  python tests/run_live_deepseek_eval.py --allow-live --full
  python tests/run_live_deepseek_eval.py --replay 20260101T000000Z
  python tests/run_live_deepseek_eval.py --allow-live --suite intent --sample 20 --seed 42
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.evals.framework.sampling import Budget  # noqa: E402
from tests.evals.framework.runner import LiveRunner  # noqa: E402

TIERS = {
    "quick": {"suites": ["intent", "temporal", "ambiguity", "clarification",
                         "followup", "golden"], "sample": 15, "seed": 42},
    "regression": {"suites": ["golden", "regression"], "sample": 0, "seed": 42},
    "full": {"suites": ["intent", "temporal", "ambiguity", "clarification",
                        "followup", "golden", "regression"],
             "sample": 0, "seed": 42},
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Butler live DeepSeek evaluation")
    p.add_argument("--allow-live", action="store_true",
                   help="explicit consent to make real API calls")
    p.add_argument("--full", action="store_true",
                   help="run the full corpus (requires --allow-live)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--quick", action="store_true")
    g.add_argument("--regression", action="store_true")
    p.add_argument("--suite", action="append", default=[],
                   help="restrict to one or more suites")
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--replay", default="", help="re-score a previous run id")
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--variant", default="A")
    p.add_argument("--max-calls", type=int, default=0)
    p.add_argument("--max-tokens", type=int, default=0)
    p.add_argument("--max-cost", type=float, default=0.0)
    p.add_argument("--max-runtime", type=float, default=0.0)
    ns = p.parse_args(argv)

    if ns.quick:
        tier = "quick"
    elif ns.regression:
        tier = "regression"
    elif ns.full:
        tier = "full"
    else:
        tier = "custom"

    if tier == "full" and not ns.allow_live:
        print("REFUSED: --full requires --allow-live")
        return 2
    if not ns.allow_live and not ns.replay:
        print("REFUSED: live evaluation requires --allow-live "
              "(or use --replay RUN_ID to re-score without calls)")
        return 2

    cfg = TIERS.get(tier, {"suites": ns.suite or ["golden"], "sample": 0,
                           "seed": ns.seed})
    suites = ns.suite or cfg["suites"]
    sample = ns.sample or cfg["sample"]
    budget = Budget(max_calls=ns.max_calls, max_tokens=ns.max_tokens,
                    max_cost=ns.max_cost, max_runtime=ns.max_runtime)
    runner = LiveRunner(allow_live=ns.allow_live, full=ns.full,
                        sample=sample, seed=ns.seed, replay=ns.replay,
                        cache=not ns.no_cache, budget=budget)
    result = runner.run(suites, tier=tier)
    if result.get("status") in ("REFUSED", "BUDGET_EXCEEDED"):
        return 2
    s = result["summary"]
    print(f"behavioral_score={s['behavioral_score']['score']*100:.1f}% "
          f"cases={s['n_cases']} failures={s['failures']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
