"""Benchmark adapter framework (Q1 / Part B).

The production Butler knows nothing about these adapters. They live in the
evaluation layer only and never become runtime dependencies.

Status vocabulary (never conflate):
  PASS            - an official-compatible runner was actually executed
  FAIL            - executed and did not meet the benchmark's criterion
  BLOCKED         - cannot run here (missing env/resources/network)
  NOT_APPLICABLE  - the benchmark's domain does not map to Butler
  ADAPTED         - a Butler-adapted suite (NOT the official benchmark)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
NOT_APPLICABLE = "NOT_APPLICABLE"
ADAPTED = "ADAPTED"


@dataclass
class BenchmarkResult:
    name: str
    status: str
    scope: str = ""
    score: str = ""
    measures: str = ""
    does_not_map: str = ""
    environment: str = ""
    version: str = ""
    runner: str = ""
    known_deviations: str = ""
    limitations: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "status": self.status, "scope": self.scope,
            "score": self.score, "measures": self.measures,
            "does_not_map": self.does_not_map, "environment": self.environment,
            "version": self.version, "runner": self.runner,
            "known_deviations": self.known_deviations,
            "limitations": self.limitations,
        }


class BenchmarkAdapter:
    """Base adapter. Subclasses implement the five hooks."""

    name = "benchmark"
    version = "unknown"
    runner = "none"

    def load_cases(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def build_context(self, case: dict[str, Any]) -> dict[str, Any]:
        return {}

    def invoke_agent(self, case: dict[str, Any], context: dict[str, Any]) -> Any:
        raise NotImplementedError

    def evaluate(self, case: dict[str, Any], output: Any) -> dict[str, Any]:
        raise NotImplementedError

    def summarize(self, results: list[dict[str, Any]]) -> BenchmarkResult:
        raise NotImplementedError

    # convenience
    def run(self, container: Any = None) -> BenchmarkResult:
        cases = self.load_cases()
        results = []
        for case in cases:
            ctx = self.build_context(case)
            try:
                out = self.invoke_agent(case, ctx)
            except Exception as exc:  # noqa: BLE001
                out = {"error": f"{type(exc).__name__}: {exc}"}
            results.append(self.evaluate(case, out))
        return self.summarize(results)
