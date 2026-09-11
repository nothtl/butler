"""M2 acceptance: typed semantic / domain contract.

Run:  .venv/bin/python tests/run_acceptance_p72.py

Proves the M2 contract end to end:
  1. Temporal resolution is deterministic (tonight/tomorrow/in two hours/
     after dinner/before class/this week/next week/bare clock/DST), preserves
     the phrase, marks inferred boundaries, and never invents exact times.
  2. The typed request/result models reject malformed or incoherent input
     (unknown fields, unknown enums, bad confidence, inferred-hard constraint,
     target-required actions without a target).
  3. The deterministic interpreter classifies the listed cases without a regex
     pile, and the LLM interpreter never trusts model output blindly.
  4. The executive service validates, resolves entities/ambiguity against live
     state, gathers a bounded snapshot, evaluates with existing domain code,
     and never mutates: gated actions return NEEDS_CONFIRMATION only.
  5. The read-only MCP boundary exposes the new ``executive_ask`` entry point.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TZ", "UTC")
try:
    time.tzset()
except Exception:  # noqa: BLE001
    pass

from butler.agent.errors import SemanticValidationError  # noqa: E402
from butler.agent.interpret import (  # noqa: E402
    DeterministicInterpreter, LLMInterpreter,
)
from butler.agent.semantic import (  # noqa: E402
    ActionKind, AgentRequest, AgentResult, Constraint, ConstraintKind,
    ConstraintSource, EntityRef, EntityType, Hardness, RequestIntent,
    ResultStatus, ScopeKind, TemporalResolution,
)
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import Session, SessionStore  # noqa: E402
from butler.agent.temporal import Clock, TemporalResolver  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def raises(fn) -> bool:
    try:
        fn()
    except SemanticValidationError:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def fresh_container() -> Container:
    base = tempfile.mkdtemp(prefix="m2p72-")
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    cfg.timezone = "UTC"
    cfg.sleep_start = 23 * 60
    cfg.sleep_end = 7 * 60
    cfg.ensure_dirs()
    c = Container(cfg)
    c.agent.store = SessionStore()
    return c


# =====================================================================
# 1. Temporal resolution (deterministic, frozen clock)
# =====================================================================
def test_temporal() -> None:
    print("\n== temporal resolution ==")
    tz = ZoneInfo("UTC")
    now = int(datetime(2026, 9, 9, 20, 0, tzinfo=tz).timestamp())  # Wed 20:00
    clock = Clock(tz=tz, now_ts=now)
    res = TemporalResolver(clock, sleep_start=23 * 60, sleep_end=7 * 60,
                           dinner_start=18 * 60, dinner_end=19 * 60,
                           class_windows=[(now + 3600, now + 7200, "CS168 Lecture")])

    t = res.resolve("tonight")
    check("tonight resolves", t.resolution != TemporalResolution.UNRESOLVED)
    check("tonight starts at now", t.start == now, str(t.start - now))
    check("tonight ends at sleep", t.end == clock.at_minute(now, 23 * 60),
          str(t.end))

    t = res.resolve("tomorrow")
    check("tomorrow is all-day", t.all_day and t.resolution != TemporalResolution.UNRESOLVED)
    check("tomorrow spans 24h (UTC)", t.end - t.start == 86400, str(t.end - t.start))

    t = res.resolve("in two hours")
    check("in two hours is now+2h", t.start == now and t.end == now + 2 * 3600,
          f"{t.start - now}/{t.end - now}")

    t = res.resolve("after dinner")
    check("after dinner resolved", t.resolution != TemporalResolution.UNRESOLVED)
    check("after dinner is a resolved window",
          t.resolution == TemporalResolution.RESOLVED, t.resolution.value)
    check("after dinner confidence < 1", 0 < t.confidence < 1.0, str(t.confidence))

    t = res.resolve("before class")
    check("before class resolved with window", t.resolution != TemporalResolution.UNRESOLVED)
    check("before class ends at class", t.end == now + 3600, str(t.end - now))

    t = res.resolve("before class", now_ts=now)
    check("before class keeps phrase", t.phrase == "before class", t.phrase)

    t = res.resolve("this week")
    check("this week resolves", t.resolution != TemporalResolution.UNRESOLVED)

    t = res.resolve("next week")
    check("next week resolves", t.resolution != TemporalResolution.UNRESOLVED)
    check("next week starts Monday 00:00",
          datetime.fromtimestamp(t.start, tz).weekday() == 0 and
          datetime.fromtimestamp(t.start, tz).hour == 0)

    t = res.resolve("at 5pm")
    check("bare clock resolves", t.resolution != TemporalResolution.UNRESOLVED)

    t = res.resolve("banana")
    check("unknown phrase is unresolved",
          t.resolution == TemporalResolution.UNRESOLVED)

    # scope mapping
    sc = res.scope("tonight", res.resolve("tonight"))
    check("tonight maps to TONIGHT scope", sc.kind == ScopeKind.TONIGHT, sc.kind.value)

    # DST: 2026-03-08 spring forward in America/Los_Angeles
    la = ZoneInfo("America/Los_Angeles")
    dst_clock = Clock(tz=la,
                      now_ts=int(datetime(2026, 3, 7, 12, 0, tzinfo=la).timestamp()))
    dst = TemporalResolver(dst_clock)
    t = dst.resolve("tomorrow")
    check("DST tomorrow spans 23h", t.end - t.start == 23 * 3600,
          str((t.end - t.start) / 3600))
    t = dst.resolve("in 24 hours")
    check("DST in 24h is exactly 24h", t.end - t.start == 24 * 3600,
          str((t.end - t.start) / 3600))


# =====================================================================
# 2. Typed model validation
# =====================================================================
def test_models() -> None:
    print("\n== typed model validation ==")
    check("inferred-hard constraint rejected", raises(lambda: Constraint(
        kind=ConstraintKind.ROUTINE, hardness=Hardness.HARD,
        source=ConstraintSource.ROUTINE_INFERRED)))
    check("soft inferred constraint allowed", isinstance(Constraint(
        kind=ConstraintKind.ROUTINE, hardness=Hardness.SOFT,
        source=ConstraintSource.ROUTINE_INFERRED), Constraint))

    check("unknown request field rejected", raises(
        lambda: AgentRequest.from_dict({"intent": "advise", "bogus": 1})))
    check("unknown enum rejected", raises(
        lambda: AgentRequest.from_dict({"intent": "nonsense"})))
    check("bad confidence rejected", raises(
        lambda: AgentRequest.from_dict({"intent": "advise", "confidence": 5})))
    check("move without target rejected", raises(
        lambda: AgentRequest.from_dict({"intent": "mutate", "action": "move"})))

    req = AgentRequest.from_dict({
        "intent": "advise", "action": "recommend",
        "constraints": [
            {"kind": "sleep", "hardness": "hard", "source": "system"},
            {"kind": "energy", "hardness": "soft", "source": "explicit_user"},
        ]})
    check("hard/soft split", len(req.hard_constraints()) == 1 and
          len(req.soft_constraints()) == 1)
    check("request round-trips", AgentRequest.from_dict(req.to_dict()).action
          == ActionKind.RECOMMEND)

    inv = AgentResult.invalid("bad", missing=["x"])
    check("invalid result status", inv.status == ResultStatus.INVALID)
    amb = AgentResult.ambiguous([])
    check("ambiguous result status", amb.status == ResultStatus.AMBIGUOUS)


# =====================================================================
# 3. Deterministic interpreter
# =====================================================================
def test_interpreter() -> None:
    print("\n== deterministic interpreter ==")
    c = fresh_container()
    now = int(datetime(2026, 9, 9, 20, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    c.db.add_course("CS168", "Cryptography")
    c.db.add_task("Write CS168 essay", est_minutes=90,
                  deadline=now + 86400, priority=4, tags="cs168")
    c.db.add_task("Read CS188 chapter", est_minutes=60,
                  deadline=now + 3 * 86400, priority=3, tags="cs188")
    interp = DeterministicInterpreter(c, now_ts=now)

    cases = [
        ("what should I do tonight", RequestIntent.ADVISE, ActionKind.RECOMMEND),
        ("help me decide what to work on", RequestIntent.ADVISE, ActionKind.RECOMMEND),
        ("plan my day tomorrow", RequestIntent.PLAN, ActionKind.PLAN_DAY),
        ("plan my week", RequestIntent.PLAN, ActionKind.PLAN_WEEK),
        ("can I fit in two hours of CS168 tomorrow", RequestIntent.EVALUATE,
         ActionKind.FEASIBILITY),
        ("what is most urgent this week", RequestIntent.QUERY, ActionKind.URGENCY),
        ("give me a status overview", RequestIntent.QUERY, ActionKind.STATUS),
        ("move that study block", RequestIntent.MUTATE, ActionKind.MOVE),
        ("defer the other assignment", RequestIntent.MUTATE, ActionKind.DEFER),
    ]
    for text, intent, action in cases:
        req = interp.interpret(text)
        check(f"classify {text!r}", req.intent == intent and req.action == action,
              f"{req.intent.value}/{req.action.value}")

    req = interp.interpret("can I fit in two hours of CS168 tomorrow")
    check("course entity found", any(e.type == EntityType.COURSE and e.name == "CS168"
                                     for e in req.entities))
    check("feasibility has tomorrow scope",
          req.scope.kind == ScopeKind.TOMORROW, req.scope.kind.value)
    check("sleep is a hard system constraint",
          any(x.kind == ConstraintKind.SLEEP and x.hardness == Hardness.HARD
              for x in req.constraints))

    req = interp.interpret("move that study block")
    check("move requires confirmation", req.requires_confirmation)
    check("move has unresolved pronoun target", req.target is not None and
          not req.target.resolved)

    req = interp.interpret("I'm exhausted, what should I do tonight")
    check("low energy detected", "low_energy" in req.preferences, str(req.preferences))

    # LLM interpreter must not trust output
    bad = LLMInterpreter(lambda prompt: "not json at all")
    check("llm non-json rejected", raises(lambda: bad.interpret("hi")))
    good = LLMInterpreter(lambda prompt: (
        '{"intent":"advise","action":"recommend","confidence":0.8}'))
    req = good.interpret("hi")
    check("llm valid proposal parsed", req.action == ActionKind.RECOMMEND)
    evil = LLMInterpreter(lambda prompt: (
        '{"intent":"mutate","action":"move","target":{"type":"task",'
        '"id":"1","name":"x","resolved":true},'
        '"constraints":[{"kind":"routine","hardness":"hard",'
        '"source":"routine_inferred"}]}'))
    check("llm inferred-hard rejected", raises(lambda: evil.interpret("hi")))


# =====================================================================
# 4. Executive service
# =====================================================================
def _service_fixture():
    c = fresh_container()
    now = int(datetime(2026, 9, 9, 20, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    c.db.add_task("CS168 Assignment 1", est_minutes=90,
                  deadline=now + 86400, priority=4, tags="cs168")
    c.db.add_task("CS188 Assignment 2", est_minutes=60,
                  deadline=now + 3 * 86400, priority=3, tags="cs188")
    c.db.add_task("Grocery run", est_minutes=30, deadline=0, priority=1, tags="")
    svc = ExecutiveService(c, now_ts=now)
    return c, svc, now


def test_service() -> None:
    print("\n== executive service ==")
    c, svc, now = _service_fixture()

    res = svc.ask(text="what is most urgent this week")
    check("urgency ok", res.status == ResultStatus.OK, res.status.value)
    check("urgency recommends by deadline",
          [r.title for r in res.recommendations][:2]
          == ["CS168 Assignment 1", "CS188 Assignment 2"],
          str([r.title for r in res.recommendations]))
    check("urgency has provenance", bool(res.provenance))

    res = svc.ask(text="can I fit in two hours of CS168 tomorrow")
    facts = res.facts[0] if res.facts else {}
    check("feasibility requested=120", facts.get("requested_minutes") == 120,
          str(facts.get("requested_minutes")))
    check("feasibility reports capacity", "available_minutes" in facts)

    res = svc.ask(text="plan my day tomorrow")
    check("plan preview ok", res.status == ResultStatus.OK)
    check("plan is read-only",
          any("read-only" in a for a in res.assumptions), str(res.assumptions))

    res = svc.ask(text="give me a status overview")
    check("status ok", res.status == ResultStatus.OK and bool(res.data))

    # gated mutation never mutates
    before = [dict(r) for r in c.db.tasks("active")]
    res = svc.ask(text="what is most urgent this week")  # set focus to #1
    res = svc.ask(text="move that study block")
    check("move gated", res.status == ResultStatus.NEEDS_CONFIRMATION,
          res.status.value)
    check("move confirmation flag", res.confirmation_required)
    check("move targets focused task",
          res.candidate_actions[0]["target"]["name"] == "CS168 Assignment 1",
          str(res.candidate_actions[0].get("target")))
    after = [dict(r) for r in c.db.tasks("active")]
    check("move did not mutate state", before == after)

    # "the other assignment" resolves against focus
    res = svc.ask(text="defer the other assignment")
    check("other assignment resolves", res.status == ResultStatus.NEEDS_CONFIRMATION,
          res.status.value)
    check("other assignment is #2",
          res.candidate_actions[0]["target"]["name"] == "CS188 Assignment 2",
          str(res.candidate_actions[0].get("target")))

    # missing reference -> ambiguity, no guessing
    fresh = fresh_container()
    svc2 = ExecutiveService(fresh, now_ts=now)
    res = svc2.ask(text="move that block")
    check("missing reference ambiguous", res.status == ResultStatus.AMBIGUOUS,
          res.status.value)
    check("ambiguous asks for target", any("referring" in w or "which" in w
                                           for w in res.warnings), str(res.warnings))

    # malformed / unknown input
    res = svc.ask(request={"intent": "advise", "action": "recommend", "bogus": 1})
    check("service rejects unknown field", res.status == ResultStatus.INVALID,
          res.status.value)
    res = svc.ask(request={"intent": "nonsense", "action": "recommend"})
    check("service rejects unknown intent", res.status == ResultStatus.INVALID,
          res.status.value)
    res = svc.ask(request={"intent": "mutate", "action": "move"})
    check("service rejects move w/o target", res.status == ResultStatus.INVALID,
          res.status.value)
    res = svc.ask(text="asdf qwerty zxcv")
    check("uninterpretable text is invalid or chat",
          res.status in (ResultStatus.INVALID, ResultStatus.OK,
                         ResultStatus.UNAVAILABLE), res.status.value)

    # bounded context snapshot
    res = svc.ask(text="what is most urgent this week", include_context=True)
    check("context attached when asked", res.context is not None)
    check("context bounded tasks", res.context is not None and
          len(res.context.tasks) <= 20)


# =====================================================================
# 5. Read-only MCP surface
# =====================================================================
def test_mcp() -> None:
    print("\n== read-only MCP surface ==")
    c = fresh_container()
    now = int(datetime(2026, 9, 9, 20, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    c.db.add_task("Write CS168 essay", est_minutes=90,
                  deadline=now + 86400, priority=4, tags="cs168")
    ro = MCPServer(c, profile="readonly")
    names = {t["name"] for t in ro._tools_spec()}
    check("executive_ask exposed", "executive_ask" in names, str(len(names)))
    check("readonly count is 37", len(names) == 37, str(len(names)))
    full = MCPServer(c, profile="full")
    full_names = {t["name"] for t in full._tools_spec()}
    check("full profile unchanged (51)", len(full_names) == 51, str(len(full_names)))
    check("full profile hides executive_ask", "executive_ask" not in full_names)

    resp = ro._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "executive_ask",
                                  "arguments": {"text": "what is most urgent this week"}}})
    result = resp.get("result", {})
    content = result.get("content", [{}])
    import json as _json
    payload = _json.loads(content[0].get("text", "{}")) if content else {}
    check("executive_ask returns structured result",
          payload.get("status") == "ok", str(payload.get("status")))
    check("executive_ask has recommendations",
          bool(payload.get("recommendations")), str(len(payload.get("recommendations", []))))

    resp = ro._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "executive_ask",
                                  "arguments": {"request": {
                                      "intent": "query", "action": "urgency"}}}})
    result = resp.get("result", {})
    content = result.get("content", [{}])
    payload = _json.loads(content[0].get("text", "{}")) if content else {}
    check("executive_ask accepts structured request",
          payload.get("status") == "ok", str(payload.get("status")))


def main() -> int:
    test_temporal()
    test_models()
    test_interpreter()
    test_service()
    test_mcp()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
