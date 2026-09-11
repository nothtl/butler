"""Run the benchmark adapters and print a status table.

Run:  .venv/bin/python tests/evals/benchmarks/run_benchmarks.py

This executes only the Butler-adapted adapters. Official runners are reported
BLOCKED, never as passed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from tests.evals.benchmarks.adapters import ADAPTERS  # noqa: E402


def main() -> int:
    rows = []
    for cls in ADAPTERS:
        try:
            result = cls().run()
        except Exception as exc:  # noqa: BLE001
            from tests.evals.benchmarks.adapter import BLOCKED, BenchmarkResult
            result = BenchmarkResult(name=cls.name, status=BLOCKED,
                                     limitations=str(exc))
        rows.append(result)
    print(f"{'benchmark':34} {'status':16} score")
    print("-" * 78)
    for r in rows:
        print(f"{r.name:34} {r.status:16} {r.score}")
    print()
    for r in rows:
        if r.status not in ("PASS", "ADAPTED"):
            print(f"[{r.status}] {r.name}: {r.limitations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
