"""Deterministic runner + metrics for the Q13 conversation benchmark."""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402

from .corpus import Case, build_corpus  # noqa: E402


def fresh_container() -> Container:
    base = tempfile.mkdtemp(prefix="q13-bench-", dir="/tmp/opencode")
    cfg = Config()
    cfg.data_dir = os.path.join(base, "s")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "c.toml")
    cfg.timezone = "UTC"
    cfg.roots = [os.path.join(base, "r")]
    os.makedirs(cfg.roots[0], exist_ok=True)
    cfg.ensure_dirs()
    c = Container(cfg)
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    c.safety.retry = None
    return c


@dataclass
class CaseResult:
    id: str
    category: str
    golden: bool
    checks: list[tuple[str, bool]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok in self.checks)


def run_corpus() -> list[CaseResult]:
    results: list[CaseResult] = []
    for case in build_corpus():
        c = fresh_container()
        svc = ExecutiveService(c, interpreter=DeterministicInterpreter(c))
        svc2 = ExecutiveService(c, interpreter=DeterministicInterpreter(c))
        c._svc = svc
        c._svc2 = svc2
        s = svc.session("user")
        try:
            checks = case.run(c, s)
        except Exception as exc:  # noqa: BLE001 — a crash is a failed case
            checks = [(f"exception:{type(exc).__name__}", False)]
        results.append(CaseResult(id=case.id, category=case.category,
                                  golden=case.golden, checks=list(checks)))
    return results


def summarize(results: list[CaseResult]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    per_cat: dict[str, list[int]] = {}
    for r in results:
        stats = per_cat.setdefault(r.category, [0, 0])
        stats[1] += 1
        if r.passed:
            stats[0] += 1
    gold = [r for r in results if r.golden]
    gold_passed = sum(1 for r in gold if r.passed)
    checks_total = sum(len(r.checks) for r in results)
    checks_ok = sum(1 for r in results for _, ok in r.checks if ok)
    return {
        "conversations": total,
        "completed": passed,
        "completion_rate": passed / total if total else 0.0,
        "per_category": {k: (v[0], v[1]) for k, v in sorted(per_cat.items())},
        "golden": (gold_passed, len(gold)),
        "checks": (checks_ok, checks_total),
    }


def main() -> int:
    results = run_corpus()
    summary = summarize(results)
    print("Q13 conversation benchmark\n")
    print(f"{'category':<24} {'completed':>9}/{'-':<4}")
    print("-" * 44)
    for cat, (ok, n) in summary["per_category"].items():
        print(f"{cat:<24} {ok:>9}/{n:<4}")
    print("-" * 44)
    print(f"{'TOTAL':<24} {summary['completed']:>9}/"
          f"{summary['conversations']:<4}")
    print(f"golden: {summary['golden'][0]}/{summary['golden'][1]}")
    print(f"checks: {summary['checks'][0]}/{summary['checks'][1]}")
    print(f"\nCONVERSATION COMPLETION RATE: "
          f"{summary['completion_rate']:.3f}")
    failures = [r for r in results if not r.passed]
    if failures:
        print("\nfailures:")
        for r in failures[:20]:
            bad = [n for n, ok in r.checks if not ok]
            print(f"  {r.id} ({r.category}): {', '.join(bad)}")
    ok = summary["completion_rate"] >= 0.95
    print("\nRESULT: PASS" if ok else "\nRESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
