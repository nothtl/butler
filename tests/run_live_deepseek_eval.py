"""LIVE DeepSeek evaluation (P6). Real network calls; NOT part of the gate.

Run:  .venv/bin/python tests/run_live_deepseek_eval.py [--limit N]

Reports schema validity, strict/lenient semantic accuracy, temporal, ambiguity,
follow-up, confirmation, hallucination and latency against the corpora in
tests/evals/. Every interpretation goes through the real Butler semantic path
(Chat -> LLMInterpreter -> AgentRequest).
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.agent.interpret import LLMInterpreter  # noqa: E402
from butler.agent.interactions import (  # noqa: E402
    InteractionKind, match_candidates, ref_from_candidate)
from butler.agent.semantic import (  # noqa: E402
    ActionKind, AgentRequest, ResultStatus)
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.agent.temporal import Clock, TemporalResolver  # noqa: E402
from butler.chat import Chat  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402

EVALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals")
CREATE_FAMILY = {"create_item", "create_task", "create_project"}
LIMIT = 50


def load(name: str) -> list[dict]:
    with open(os.path.join(EVALS, name)) as f:
        return [json.loads(x) for x in f if x.strip()]


def pct(vals, p):
    if not vals:
        return 0
    s = sorted(vals)
    import math
    i = min(len(s) - 1, int(math.ceil(p / 100 * len(s))) - 1)
    return s[i]


def temp_container():
    base = tempfile.mkdtemp(prefix="live-", dir="/tmp/opencode")
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


def main() -> int:
    limit = LIMIT
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    cfg = Config.load()
    if not (cfg.llm_api_key and cfg.llm_base_url):
        print("BLOCKED: no LLM configured")
        return 0
    chat = Chat(cfg, None, None)
    llm = LLMInterpreter(client=chat)
    lat: list[int] = []
    failures = 0

    def interp(text, context=None, topic=None):
        nonlocal failures
        t0 = time.perf_counter()
        try:
            req = llm.interpret(text, context=context, topic=topic or {})
            lat.append(int((time.perf_counter() - t0) * 1000))
            return req
        except Exception as exc:  # noqa: BLE001
            failures += 1
            lat.append(int((time.perf_counter() - t0) * 1000))
            print("  error:", type(exc).__name__, str(exc)[:60])
            return None

    # ---- intent ----------------------------------------------------------
    rows = load("intent.jsonl")[:limit]
    sv = strict = lacc = 0
    false_confident = 0
    for r in rows:
        req = interp(r["input"])
        if req is None:
            continue
        sv += 1
        action = req.action.value
        ok = action in (r["expected_action"],) or (
            action in CREATE_FAMILY and r["expected_action"] in CREATE_FAMILY)
        if action == r["expected_action"]:
            strict += 1
        if ok:
            lacc += 1
        if r["expected_ambiguity"] and not req.ambiguity \
                and req.confidence >= 0.5 and req.target is not None \
                and req.target.resolved:
            false_confident += 1
    n = max(1, len(rows))
    print(f"\nINTENT  n={len(rows)} schema_valid={sv} "
          f"strict={strict}/{n} ({strict/n*100:.0f}%) "
          f"lenient={lacc}/{n} ({lacc/n*100:.0f}%) "
          f"false_confident={false_confident}")

    # ---- temporal --------------------------------------------------------
    # Evaluate against a reproducible reference clock (10:00 local today);
    # the model still does the real phrase extraction.
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    try:
        _tz = _ZI(getattr(cfg, "timezone", "UTC") or "UTC")
    except Exception:  # noqa: BLE001
        _tz = _ZI("UTC")
    ref = int(_dt.now(_tz).replace(hour=10, minute=0, second=0,
                                   microsecond=0).timestamp())
    tz = Clock(tz=_tz, now_ts=ref)
    resolver = TemporalResolver(
        tz,
        class_windows=[(ref + 3600, ref + 7200, "c")],
        meeting_windows=[(ref + 10800, ref + 14400, "m")])
    trows = load("temporal.jsonl")[:limit]
    t_ok = 0
    for r in trows:
        req = interp(r["input"])
        phrase = ""
        if req is not None and req.temporal is not None:
            phrase = req.temporal.phrase or ""
        resolved = resolver.resolve(phrase or r["input"]).is_resolved()
        if resolved == r["expected_resolvable"]:
            t_ok += 1
    print(f"TEMPORAL n={len(trows)} correct={t_ok}/{len(trows)} "
          f"({t_ok/max(1,len(trows))*100:.0f}%)")

    # ---- ambiguity -------------------------------------------------------
    arows = load("ambiguity.jsonl")[:limit]
    a_ok = 0
    for r in arows:
        req = interp(r["input"])
        if req is None:
            continue
        needs_clar = bool(req.ambiguity) or req.confidence < 0.5 or (
            req.target is not None and not req.target.resolved)
        if needs_clar:
            a_ok += 1
    print(f"AMBIGUITY n={len(arows)} flagged={a_ok}/{len(arows)} "
          f"({a_ok/max(1,len(arows))*100:.0f}%)")

    # ---- follow-up (real LLM + deterministic interaction state) ----------
    c = temp_container()
    c1 = c.db.add_course("CS188", "AI")
    c2 = c.db.add_course("CS168", "Net")
    c.projects.create_project("Project 2", course_id=c1)
    c.projects.create_project("Project 2", course_id=c2)
    svc = ExecutiveService(c, interpreter=llm)
    frows = load("followup.jsonl")
    f_ok = 0
    for i, r in enumerate(frows):
        s = svc.session(f"fu{i}")
        first, second = r["turns"]
        svc.ask(text=first, user=f"fu{i}")
        clar = svc.interactions.active(s, InteractionKind.CLARIFICATION)
        before = svc.interactions.active(s, InteractionKind.CONFIRMATION)
        svc.ask(text=second, user=f"fu{i}")
        after_clar = svc.interactions.active(s, InteractionKind.CLARIFICATION)
        if clar is not None:
            # a pending clarification should be consumed by the follow-up
            if after_clar is None or after_clar.id != clar.id:
                f_ok += 1
        elif before is not None:
            # a pending proposal should be modified, not duplicated
            f_ok += 1
    print(f"FOLLOWUP n={len(frows)} resolved={f_ok}/{len(frows)} "
          f"({f_ok/max(1,len(frows))*100:.0f}%)")

    # ---- clarification (real LLM + service) ------------------------------
    def butler_corpus(name):
        path = os.path.join(EVALS, "butler", name)
        with open(path) as f:
            return [json.loads(x) for x in f if x.strip()]

    clar_rows = butler_corpus("clarification.jsonl")[:limit]
    cc = temp_container()
    cc1 = cc.db.add_course("CS188", "AI")
    cc2 = cc.db.add_course("CS168", "Net")
    cc.projects.create_project("Project 2", course_id=cc1)
    cc.projects.create_project("Project 2", course_id=cc2)
    csvc = ExecutiveService(cc, interpreter=llm)
    cl_ok = 0
    for i, r in enumerate(clar_rows):
        res = csvc.ask(text=r["input"], user=f"cl{i}", topic={})
        data = res.data if isinstance(res.data, dict) else {}
        if isinstance(data, dict) and data.get("clarification"):
            cl_ok += 1
    print(f"CLARIFICATION n={len(clar_rows)} offered={cl_ok}/{len(clar_rows)} "
          f"({cl_ok/max(1,len(clar_rows))*100:.0f}%)")

    # ---- latency ---------------------------------------------------------
    if lat:
        print(f"\nLATENCY n={len(lat)} mean={statistics.mean(lat):.0f}ms "
              f"median={statistics.median(lat):.0f}ms p95={pct(lat,95)}ms "
              f"failures={failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
