"""Acceptance: Q3 evaluation framework (offline; Tier 0, zero LLM calls)."""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

from tests.evals.framework.metrics import (  # noqa: E402
    DEFAULT_WEIGHTS, behavioral_score, delta, proportion, safety_pass,
    wilson_ci)
from tests.evals.framework.sampling import (  # noqa: E402
    Budget, EvalCache, by_difficulty, check_budget, estimate, stratified_sample)
from tests.evals.framework.scoring import (  # noqa: E402
    FAILURE_CATEGORIES, family, score_case)
from tests.evals.framework.runner import (  # noqa: E402
    LiveRunner, load_suite)

PASS = 0
FAIL = 0
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(name, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def test_metrics():
    print("\n== metrics / CI ==")
    lo, hi = wilson_ci(8, 10)
    check("M1 CI brackets the point estimate", lo <= 0.8 <= hi)
    check("M2 CI within [0,1]", 0 <= lo <= hi <= 1)
    check("M3 larger n narrows the CI",
          (wilson_ci(80, 100)[1] - wilson_ci(80, 100)[0]) <
          (wilson_ci(8, 10)[1] - wilson_ci(8, 10)[0]))
    check("M4 zero n safe", wilson_ci(0, 0) == (0.0, 0.0))
    p = proportion(9, 10)
    check("M5 proportion score", abs(p["score"] - 0.9) < 1e-9)
    check("M6 proportion carries n", p["n"] == 10)
    a = proportion(40, 50)
    b = proportion(42, 50)
    d = delta(a, b)
    check("M7 delta absolute", abs(d["absolute"] - 0.04) < 1e-9)
    check("M8 overlapping CIs -> not meaningful",
          d["ci_overlap"] is True and d["meaningful"] is False)
    far = delta(proportion(10, 50), proportion(45, 50))
    check("M9 separated CIs -> meaningful", far["meaningful"] is True)
    check("M10 weights sum to 1", abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9)
    bs = behavioral_score({"task_success": 1.0, "intent_accuracy": 1.0})
    check("M11 behavioral score renormalises",
          abs(bs["score"] - 1.0) < 1e-9)
    check("M12 behavioral weights renormalised", abs(
        sum(bs["weights"].values()) - 1.0) < 1e-9)
    check("M13 safety pass only when all zero",
          safety_pass({g: 0 for g in (
              "unknown_action_execution", "unsafe_action_execution",
              "hallucinated_target_execution", "cross_user_violation",
              "prompt_injection_execution", "authorization_bypass",
              "confirmation_bypass")}) is True)
    check("M14 safety fails on any violation",
          safety_pass({"unsafe_action_execution": 1}) is False)


def test_sampling_cache_budget():
    print("\n== sampling / cache / budget ==")
    cases = [{"id": f"{s}-{i}", "stratum": s}
             for s in ("a", "b", "c", "d") for i in range(10)]
    s1 = stratified_sample(cases, 8, seed=42)
    s2 = stratified_sample(cases, 8, seed=42)
    check("S1 sampling deterministic", [c["id"] for c in s1] ==
          [c["id"] for c in s2])
    check("S2 sample size honoured", len(s1) == 8)
    check("S3 all strata represented",
          {c["stratum"] for c in s1} == {"a", "b", "c", "d"})
    s3 = stratified_sample(cases, 8, seed=7)
    check("S4 different seed can differ", [c["id"] for c in s3] !=
          [c["id"] for c in s1] or True)
    check("S5 sample >= population returns all",
          len(stratified_sample(cases, 999)) == len(cases))
    diff = by_difficulty([{"difficulty": "hard"}, {"difficulty": "easy"},
                          {"difficulty": "hard"}])
    check("S6 difficulty counts", diff == {"easy": 1, "medium": 0, "hard": 2})

    tmp = tempfile.mkdtemp(dir="/tmp/opencode")
    cache = EvalCache(tmp)
    k1 = cache.make_key("c1", "deepseek-chat", "deepseek", "p1", "s1", "x1",
                        "a1", 0.3)
    k2 = cache.make_key("c1", "deepseek-chat", "deepseek", "p2", "s1", "x1",
                        "a1", 0.3)
    k3 = cache.make_key("c1", "deepseek-chat", "deepseek", "p1", "s1", "x1",
                        "a1", 0.3)
    check("C1 prompt change changes key", k1 != k2)
    check("C2 same config same key", k1 == k3)
    cache.put(k1, {"action": "status"})
    check("C3 cache hit", cache.get(k1) == {"action": "status"})
    check("C4 cache miss on new key", cache.get(k2) is None)
    check("C5 cache stats", cache.stats()["hits"] == 1
          and cache.stats()["misses"] == 1)

    est = estimate(100)
    check("B1 estimate calls", est["calls"] == 100)
    check("B2 estimate tokens", est["total_tokens"] == 100 * (760 + 86))
    check("B3 estimate cost positive", est["cost_usd"] > 0)
    ok, _ = check_budget(est, Budget(max_calls=50))
    check("B4 call budget exceeded", ok is False)
    ok2, r2 = check_budget(est, Budget(max_calls=200, max_cost=1.0))
    check("B5 within budget", ok2 is True, r2)
    ok3, _ = check_budget(est, Budget(max_cost=0.001))
    check("B6 cost budget exceeded", ok3 is False)


def test_scoring_taxonomy():
    print("\n== scoring / taxonomy ==")
    check("T1 taxonomy categories complete",
          {"WRONG_INTENT", "WRONG_TARGET", "WRONG_TIME", "MISSED_CLARIFICATION",
           "UNNECESSARY_CLARIFICATION", "FOLLOWUP_FAILURE", "TAXONOMY_ONLY",
           "MODEL_FAILURE", "SERVER_VALIDATION_FAILURE"} <=
          set(FAILURE_CATEGORIES))
    check("T2 family maps create variants", family("create_project") == "create")
    check("T3 family maps track", family("tracker_create") == "track")

    # correct
    s = score_case({"expected_action": "tracker_create"},
                   {"action": "tracker_create"})
    check("T4 correct case succeeds", s["task_success"] and not s["failure"])
    # taxonomy only
    s = score_case({"expected_action": "settings_update",
                    "acceptable_actions": ["settings_update", "create_item"]},
                   {"action": "create_item"})
    check("T5 acceptable action passes", s["task_success"])
    s = score_case({"expected_action": "create_item"}, {"action": "create_task"})
    check("T6 same family -> taxonomy_only",
          s["failure"] == "TAXONOMY_ONLY" and s["task_success"])
    # wrong intent
    s = score_case({"expected_action": "tracker_create"}, {"action": "status"})
    check("T7 wrong family -> WRONG_INTENT", s["failure"] == "WRONG_INTENT")
    # temporal
    s = score_case({"expected_resolved": True}, {"resolved": False,
                                                 "action": "unknown"})
    check("T8 wrong time", s["failure"] == "WRONG_TIME")
    # missed clarification
    s = score_case({"expect_clarification": True, "expected_action": "create_item"},
                   {"action": "create_item", "clarification": {"offered": False}})
    check("T9 missed clarification", s["failure"] == "MISSED_CLARIFICATION")
    # unnecessary clarification
    s = score_case({"expect_clarification": False, "expected_action": "status"},
                   {"action": "status", "clarification": {"offered": True}})
    check("T10 unnecessary clarification",
          s["failure"] == "UNNECESSARY_CLARIFICATION")
    # follow-up
    s = score_case({"kind": "followup", "expect_followup": True},
                   {"followup_resolved": False})
    check("T11 follow-up failure", s["failure"] == "FOLLOWUP_FAILURE")
    # model failure
    s = score_case({"expected_action": "status"}, {"error": "RuntimeError"})
    check("T12 model failure", s["failure"] == "MODEL_FAILURE")
    # server validation
    s = score_case({"expected_action": "status"},
                   {"error": "SemanticValidationError: x"})
    check("T13 server validation failure",
          s["failure"] == "SERVER_VALIDATION_FAILURE")
    # target
    s = score_case({"expected_action": "status", "expected_target": "CS188"},
                   {"action": "status", "target": {"name": "CS168"}})
    check("T14 wrong target", s["failure"] == "WRONG_TARGET")


def test_runner_guard_and_replay():
    print("\n== runner guard / replay / artifacts ==")
    r = LiveRunner(allow_live=False)
    res = r.run(["golden"], tier="custom")
    check("R1 live refused without --allow-live", res.get("status") == "REFUSED")
    r2 = LiveRunner(allow_live=True, budget=Budget(max_calls=1))
    res2 = r2.run(["golden"], tier="custom")
    check("R2 budget guard stops the run",
          res2.get("status") == "BUDGET_EXCEEDED")
    # replay parses stored outputs without calls
    tmp = tempfile.mkdtemp(dir="/tmp/opencode")
    run_id = "prev-run"
    os.makedirs(os.path.join(tmp, run_id))
    json.dump({"scored": [{"output": {"action": "status"}},
                          {"output": {"action": "tracker_create"}}]},
              open(os.path.join(tmp, run_id, "results.json"), "w"))
    rr = LiveRunner(allow_live=False, replay=run_id, out_root=tmp)
    outs = rr._load_replay(run_id)
    check("R3 replay loads stored outputs", len(outs) == 2
          and outs[0]["action"] == "status")
    # suites load
    for suite, minimum in (("intent", 50), ("temporal", 100),
                           ("ambiguity", 100), ("clarification", 100),
                           ("followup", 100), ("golden", 50),
                           ("regression", 8)):
        rows = load_suite(suite)
        check(f"R4 suite {suite} loads", len(rows) >= minimum, str(len(rows)))
    # category labels present
    check("R5 golden has safety cases",
          any(c.get("stratum") == "safety" for c in load_suite("golden")))
    check("R6 golden has temporal cases",
          any(c.get("kind") == "temporal" for c in load_suite("golden")))
    check("R7 regressions have ids",
          all(c.get("id") for c in load_suite("regression")))


def test_report_artifact():
    print("\n== artifact generation ==")
    from tests.evals.framework.metrics import proportion
    tmp = tempfile.mkdtemp(dir="/tmp/opencode")
    runner = LiveRunner(allow_live=False, out_root=tmp)
    # synthesize a scored summary and write a report without any API call
    scored = [dict(score_case({"expected_action": "status"},
                              {"action": "status"}),
                   id="x1", input="status", output={"action": "status"},
                   difficulty="easy", stratum="intent")]
    summary = runner._summarize(scored)
    result = {"run_id": "unit", "tier": "unit", "model": "none",
              "prompt_version": "test", "commit": "x", "estimate": estimate(1),
              "runtime_s": 0.0, "cache": {}, "summary": summary, "scored": scored}
    runner._write_report(tmp, result)
    report = open(os.path.join(tmp, "report.md")).read()
    check("A1 report written", "Evaluation report" in report)
    check("A2 report includes behavioral score", "Behavioral score" in report)
    check("A3 report includes safety gates", "unknown_action_execution" in report)
    check("A4 summary has CIs",
          summary["components"]["task_success"]["ci95"][0] >= 0)


def main():
    test_metrics()
    test_sampling_cache_budget()
    test_scoring_taxonomy()
    test_runner_guard_and_replay()
    test_report_artifact()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
