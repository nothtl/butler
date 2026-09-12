"""Acceptance: Q13 conversation benchmark (deterministic, offline).

Run:  .venv/bin/python tests/run_acceptance_q13_benchmark.py

Executes the 300+ conversation corpus and reports the primary metric
(conversation completion rate) plus per-category results.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

from tests.evals.conversation.runner import (  # noqa: E402
    run_corpus, summarize)


def main() -> int:
    results = run_corpus()
    summary = summarize(results)
    print("Q13 conversation benchmark\n")
    print(f"{'category':<24} {'completed':>9}")
    print("-" * 40)
    for cat, (ok, n) in summary["per_category"].items():
        print(f"{cat:<24} {ok:>6}/{n}")
    print("-" * 40)
    print(f"{'TOTAL':<24} {summary['completed']:>6}/"
          f"{summary['conversations']}")
    print(f"golden conversations : {summary['golden'][0]}/"
          f"{summary['golden'][1]}")
    print(f"checks               : {summary['checks'][0]}/"
          f"{summary['checks'][1]}")
    print(f"conversation completion rate: {summary['completion_rate']:.3f}")
    ok = summary["completion_rate"] >= 0.95
    print(f"\n==== RESULT: {summary['completed']} passed, "
          f"{summary['conversations'] - summary['completed']} failed ====")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
