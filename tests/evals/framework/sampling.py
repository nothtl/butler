"""Deterministic stratified sampling, budget guards and eval caching."""
from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from typing import Any


# --- stratified sampling -----------------------------------------------------

def stratum_of(case: dict[str, Any]) -> str:
    return str(case.get("stratum") or case.get("category")
               or case.get("domain") or "other")


def stratified_sample(cases: list[dict[str, Any]], n: int,
                      seed: int = 0) -> list[dict[str, Any]]:
    """Deterministic round-robin sample across strata (reproducible by seed)."""
    if n >= len(cases):
        return list(cases)
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in cases:
        groups.setdefault(stratum_of(c), []).append(c)
    rng = random.Random(seed)
    for k in sorted(groups):
        rng.shuffle(groups[k])
    order = sorted(groups)
    out: list[dict[str, Any]] = []
    idx = {k: 0 for k in order}
    while len(out) < n:
        progressed = False
        for k in order:
            if idx[k] < len(groups[k]):
                out.append(groups[k][idx[k]])
                idx[k] += 1
                progressed = True
                if len(out) >= n:
                    break
        if not progressed:
            break
    return out


def by_difficulty(cases: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    for c in cases:
        out[str(c.get("difficulty", "medium"))] = \
            out.get(str(c.get("difficulty", "medium")), 0) + 1
    return out


# --- budget ------------------------------------------------------------------

#: approximate DeepSeek list pricing (USD per token)
COST_IN = 0.27 / 1_000_000
COST_OUT = 1.10 / 1_000_000


@dataclass
class Budget:
    max_calls: int = 0
    max_tokens: int = 0
    max_cost: float = 0.0
    max_runtime: float = 0.0


def estimate(calls: int, *, in_tok: int = 760, out_tok: int = 86,
             sec_per_call: float = 1.2) -> dict[str, Any]:
    total = calls * (in_tok + out_tok)
    cost = calls * (in_tok * COST_IN + out_tok * COST_OUT)
    return {"calls": calls, "input_tokens": calls * in_tok,
            "output_tokens": calls * out_tok, "total_tokens": total,
            "cost_usd": round(cost, 4),
            "runtime_s": round(calls * sec_per_call, 1)}


def check_budget(est: dict[str, Any], budget: Budget) -> tuple[bool, str]:
    if budget.max_calls and est["calls"] > budget.max_calls:
        return False, "BUDGET_EXCEEDED: calls"
    if budget.max_tokens and est["total_tokens"] > budget.max_tokens:
        return False, "BUDGET_EXCEEDED: tokens"
    if budget.max_cost and est["cost_usd"] > budget.max_cost:
        return False, "BUDGET_EXCEEDED: cost"
    if budget.max_runtime and est["runtime_s"] > budget.max_runtime:
        return False, "BUDGET_EXCEEDED: runtime"
    return True, ""


# --- cache -------------------------------------------------------------------

class EvalCache:
    """Deterministic response cache. Key includes model/prompt/schema/context."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(case_id: str, model: str, provider: str, prompt_hash: str,
                 schema_hash: str, context_hash: str, actions_hash: str,
                 temperature: float) -> str:
        raw = "|".join(str(x) for x in (
            case_id, model, provider, prompt_hash, schema_hash, context_hash,
            actions_hash, round(float(temperature), 3)))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def _file(self, key: str) -> str:
        return os.path.join(self.path, key + ".json")

    def get(self, key: str) -> Any | None:
        try:
            with open(self._file(key)) as f:
                self.hits += 1
                return json.load(f)
        except FileNotFoundError:
            self.misses += 1
            return None

    def put(self, key: str, value: Any) -> None:
        with open(self._file(key), "w") as f:
            json.dump(value, f, default=str)

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses}
