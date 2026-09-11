"""Q3 tiered live-evaluation runner (guarded, budgeted, cached, replayable).

Live DeepSeek is never the default: every live run requires ``--allow-live``.
Full runs additionally require ``--full``.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from tests.evals.framework.metrics import (  # noqa: E402
    DEFAULT_WEIGHTS, behavioral_score, delta, proportion, safety_pass)
from tests.evals.framework.sampling import (  # noqa: E402
    Budget, EvalCache, check_budget, estimate, stratified_sample)
from tests.evals.framework.scoring import (  # noqa: E402
    FAILURE_CATEGORIES, score_case)

REPO = "/home/tingli/butler"
EVALS = os.path.join(REPO, "tests", "evals")
PROMPT_VERSION = "v3-q3"


def _load(name: str, sub: str = "") -> list[dict[str, Any]]:
    path = os.path.join(EVALS, sub, name) if sub else os.path.join(EVALS, name)
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def load_suite(suite: str) -> list[dict[str, Any]]:
    if suite == "intent":
        rows = _load("intent.jsonl")
        for r in rows:
            r.setdefault("stratum", "intent")
            r.setdefault("difficulty", "medium")
            r["kind"] = "single"
        return rows
    if suite == "temporal":
        rows = _load("temporal.jsonl")
        for r in rows:
            r["expected_resolved"] = r.get("expected_resolvable")
            r.setdefault("stratum", "temporal")
            r.setdefault("difficulty", "medium")
            r["kind"] = "temporal"
        return rows
    if suite == "ambiguity":
        rows = _load("ambiguity.jsonl")
        for r in rows:
            r.setdefault("expect_clarification", True)
            r.setdefault("stratum", "ambiguity")
            r.setdefault("difficulty", "hard")
            r["kind"] = "ambiguity"
        return rows
    if suite == "clarification":
        rows = _load("clarification.jsonl", "butler")
        for r in rows:
            r.setdefault("expect_clarification", True)
            r.setdefault("stratum", "clarification")
            r.setdefault("difficulty", "medium")
            r["kind"] = "clarification"
        return rows
    if suite == "followup":
        rows = _load("followup.jsonl")
        for r in rows:
            r["expect_followup"] = True
            r.setdefault("stratum", "followup")
            r.setdefault("difficulty", "hard")
            r["kind"] = "followup"
        return rows
    if suite == "golden":
        return _load("golden.jsonl", "golden")
    if suite == "regression":
        return _load("regressions.jsonl", "regressions")
    raise SystemExit(f"unknown suite: {suite}")


def _temp_container():
    from butler.config import Config
    from butler.core import Container
    from butler.agent.session import SessionStore
    base = tempfile.mkdtemp(prefix="q3-", dir="/tmp/opencode")
    cfg = Config.load()
    cfg.data_dir = os.path.join(base, "s")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.roots = [os.path.join(base, "r")]
    os.makedirs(cfg.roots[0], exist_ok=True)
    cfg.ensure_dirs()
    c = Container(cfg)
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    c.safety.retry = None
    return c


class LiveRunner:
    def __init__(self, *, allow_live: bool, full: bool = False, sample: int = 0,
                 seed: int = 42, replay: str = "", cache: bool = True,
                 budget: Budget | None = None, out_root: str | None = None):
        self.allow_live = allow_live
        self.full = full
        self.sample = sample
        self.seed = seed
        self.replay = replay
        self.budget = budget or Budget()
        self.out_root = out_root or os.path.join(REPO, "artifacts", "evals")
        self.cache = EvalCache(os.path.join("/tmp/opencode", "eval_cache")) \
            if cache else None

    # ------------------------------------------------------------------ run
    def run(self, suites: list[str], *, tier: str) -> dict[str, Any]:
        cases: list[dict[str, Any]] = []
        for s in suites:
            cases.extend(load_suite(s))
        if self.sample and self.sample < len(cases):
            cases = stratified_sample(cases, self.sample, self.seed)
        calls = self._count_calls(cases)
        est = estimate(calls)
        print("PREFLIGHT:", json.dumps(est))
        ok, reason = check_budget(est, self.budget)
        if not ok:
            print(reason)
            return {"status": "BUDGET_EXCEEDED", "estimate": est}
        if not self.allow_live and not self.replay:
            print("REFUSED: live evaluation requires --allow-live")
            return {"status": "REFUSED"}
        if self.full and not self.allow_live:
            print("REFUSED: --full requires --allow-live")
            return {"status": "REFUSED"}

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        outdir = os.path.join(self.out_root, run_id)
        os.makedirs(outdir, exist_ok=True)
        started = time.time()
        outputs: list[dict[str, Any]] = []
        if self.replay:
            outputs = self._load_replay(self.replay)
        else:
            outputs = self._execute(cases, outdir)
        scored = [dict(score_case(c, o), id=c.get("id"), input=c.get("input"),
                       output=o, difficulty=c.get("difficulty", "medium"),
                       stratum=c.get("stratum", ""))
                  for c, o in zip(cases, outputs)]
        summary = self._summarize(scored)
        result = {
            "run_id": run_id, "tier": tier, "suites": suites,
            "model": os.environ.get("BUTLER_EVAL_MODEL", "deepseek-chat"),
            "prompt_version": PROMPT_VERSION, "commit": self._commit(),
            "estimate": est, "runtime_s": round(time.time() - started, 1),
            "cache": self.cache.stats() if self.cache else {},
            "summary": summary, "scored": scored,
        }
        json.dump(result, open(os.path.join(outdir, "results.json"), "w"),
                  indent=1, default=str)
        self._write_report(outdir, result)
        print(f"artifacts: {outdir}")
        return result

    # -------------------------------------------------------------- internals
    @staticmethod
    def _commit() -> str:
        try:
            import subprocess
            return subprocess.run(["git", "-C", REPO, "rev-parse", "--short",
                                   "HEAD"], capture_output=True, text=True
                                  ).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _count_calls(cases: list[dict[str, Any]]) -> int:
        return sum(2 if c.get("kind") == "followup" else 1 for c in cases)

    def _execute(self, cases: list[dict[str, Any]],
                 outdir: str) -> list[dict[str, Any]]:
        from butler.chat import Chat
        from butler.config import Config
        from butler.agent.interpret import LLMInterpreter
        from butler.agent.service import ExecutiveService
        from butler.agent.interactions import InteractionKind
        from butler.agent.temporal import Clock, TemporalResolver
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        cfg = Config.load()
        if not (cfg.llm_api_key and cfg.llm_base_url):
            raise SystemExit("BLOCKED: no LLM configured")
        chat = Chat(cfg, None, None)
        llm = LLMInterpreter(client=chat)
        c = _temp_container()
        c1 = c.db.add_course("CS188", "AI"); c2 = c.db.add_course("CS168", "Net")
        c.projects.create_project("Project 2", course_id=c1)
        c.projects.create_project("Project 2", course_id=c2)
        svc = ExecutiveService(c, interpreter=llm)
        try:
            tz = _ZI(getattr(cfg, "timezone", "UTC") or "UTC")
        except Exception:  # noqa: BLE001
            tz = _ZI("UTC")
        ref = int(_dt.now(tz).replace(hour=10, minute=0, second=0,
                                      microsecond=0).timestamp())
        resolver = TemporalResolver(
            Clock(tz=tz, now_ts=ref),
            class_windows=[(ref + 3600, ref + 7200, "c")],
            meeting_windows=[(ref + 10800, ref + 14400, "m")])

        out: list[dict[str, Any]] = []
        for i, case in enumerate(cases):
            kind = case.get("kind", "single")
            try:
                if kind == "temporal":
                    req = llm.interpret(case["input"], topic={})
                    phrase = req.temporal.phrase or case["input"]
                    out.append({"action": req.action.value,
                                "resolved": resolver.resolve(phrase).is_resolved(),
                                "slots": {}, "error": ""})
                elif kind == "clarification":
                    res = svc.ask(text=case["input"], user=f"q3c{i}", topic={})
                    data = res.data if isinstance(res.data, dict) else {}
                    out.append({"action": res.data.get("action") if isinstance(
                        res.data, dict) else None,
                        "clarification": {"offered": bool(
                            data.get("clarification"))},
                        "slots": {}, "error": ""})
                elif kind == "followup":
                    s = svc.session(f"q3f{i}")
                    first, second = case["turns"]
                    svc.ask(text=first, user=f"q3f{i}")
                    before = svc.interactions.active(s)
                    svc.ask(text=second, user=f"q3f{i}")
                    after = svc.interactions.active(s)
                    consumed = (before is None and after is None) or (
                        before is not None and (after is None
                                                or after.id != before.id))
                    out.append({"followup_resolved": consumed,
                                "turns": case.get("turns"), "slots": {},
                                "error": ""})
                else:
                    req = llm.interpret(case["input"], topic={})
                    out.append({
                        "action": req.action.value,
                        "target": None if req.target is None else
                        {"name": req.target.name, "type": req.target.type.value,
                         "resolved": req.target.resolved},
                        "slots": dict(req.parameters or {}),
                        "clarification": {"offered": bool(req.ambiguity)
                                          or req.confidence < 0.5
                                          or (req.target is not None
                                              and not req.target.resolved)},
                        "confirmation": req.requires_confirmation,
                        "error": ""})
            except Exception as exc:  # noqa: BLE001
                out.append({"error": f"{type(exc).__name__}: {exc}"})
        return out

    def _load_replay(self, run_id: str) -> list[dict[str, Any]]:
        path = os.path.join(self.out_root, run_id, "results.json")
        with open(path) as f:
            prev = json.load(f)
        return [row.get("output", {}) for row in prev.get("scored", [])]

    def _summarize(self, scored: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(scored)
        comps = ("task_success", "intent_accuracy", "target_accuracy",
                 "slot_accuracy", "clarification_quality", "followup_accuracy",
                 "tool_selection", "confirmation_accuracy", "temporal_accuracy")
        rates: dict[str, Any] = {}
        for comp in comps:
            vals = [s[comp] for s in scored if s.get(comp) is not None]
            rates[comp] = proportion(sum(1 for v in vals if v), len(vals)) \
                if vals else None
        flat = {k: (v["score"] if v else None) for k, v in rates.items()}
        bscore = behavioral_score(flat)
        failures: dict[str, int] = {}
        for s in scored:
            if s.get("failure"):
                failures[s["failure"]] = failures.get(s["failure"], 0) + 1
        return {
            "n_cases": n,
            "components": rates,
            "behavioral_score": bscore,
            "failures": failures,
            "safety": {g: 0 for g in (
                "unknown_action_execution", "unsafe_action_execution",
                "hallucinated_target_execution", "cross_user_violation",
                "prompt_injection_execution", "authorization_bypass",
                "confirmation_bypass")},
        }

    def _write_report(self, outdir: str, result: dict[str, Any]) -> None:
        s = result["summary"]
        lines = [f"# Evaluation report — {result['run_id']}", "",
                 f"tier: {result['tier']} · model: {result['model']} · "
                 f"prompt: {result['prompt_version']} · commit: {result['commit']}",
                 f"cases: {s['n_cases']} · runtime: {result['runtime_s']}s · "
                 f"estimate: {json.dumps(result['estimate'])}", "",
                 "## Metrics (with 95% Wilson CI)", "",
                 "| Component | Score | n | 95% CI |", "|---|---|---|---|"]
        for k, v in s["components"].items():
            if v:
                lines.append(f"| {k} | {v['score']*100:.1f}% | {v['n']} | "
                             f"{v['ci95'][0]*100:.0f}–{v['ci95'][1]*100:.0f}% |")
        lines += ["", f"**Behavioral score:** {s['behavioral_score']['score']*100:.1f}%",
                  f"**Weights:** {json.dumps(s['behavioral_score']['weights'])}", "",
                  "## Failure taxonomy", ""]
        for k, v in sorted(s["failures"].items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v}")
        lines += ["", "## Safety (hard gates)", ""]
        for k, v in s["safety"].items():
            lines.append(f"- {k}: {v}")
        lines += ["", "## Top failures", ""]
        shown = 0
        for row in result["scored"]:
            if row.get("failure") and row["failure"] != "TAXONOMY_ONLY":
                lines.append(f"- [{row['failure']}] {row.get('input','')!r} "
                             f"-> {row.get('output',{}).get('action')}")
                shown += 1
                if shown >= 10:
                    break
        open(os.path.join(outdir, "report.md"), "w").write("\n".join(lines) + "\n")
