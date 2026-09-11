"""Acceptance: semantic reasoning + fail-closed safety hardening (P0-P5).

Run:  .venv/bin/python tests/run_acceptance_semantic_hardening.py

Deterministic and offline. Live DeepSeek evaluation lives in
``tests/run_live_deepseek_eval.py`` and is never part of this gate.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

from butler.agent.actions import (  # noqa: E402
    ACTIONS, RiskClass, get_action, is_registered, registered_names,
    requires_confirmation)
from butler.agent.interpret import (  # noqa: E402
    DeterministicInterpreter, HybridInterpreter, LLMInterpreter)
from butler.agent.interactions import (  # noqa: E402
    InteractionKind, InteractionStore, match_candidates)
from butler.agent.schema import (  # noqa: E402
    relevant_actions, request_json_schema, schema_prompt)
from butler.agent.semantic import (  # noqa: E402
    ActionKind, AgentRequest, ConstraintSource, Hardness, ResultStatus)
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.agent.temporal import Clock, TemporalResolver  # noqa: E402
from butler.agent.tools import build_default_registry  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.safety import ActionClass  # noqa: E402

PASS = 0
FAIL = 0
EVALS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals")


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def fresh(prefix: str = "hard-") -> Container:
    base = tempfile.mkdtemp(prefix=prefix, dir="/tmp/opencode")
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
    c.safety.retry = None  # deterministic: no rate-limit interference
    return c


def load_jsonl(name: str) -> list[dict]:
    path = os.path.join(EVALS, name)
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# =====================================================================
# 1. action registry + fail-closed safety (P0/P1)
# =====================================================================
def test_registry_and_safety() -> None:
    print("\n== action registry / fail-closed safety ==")
    c = fresh("hard-safety-")
    # every registered action is one of the six risk classes
    check("R1 every action has a valid risk class",
          all(a.risk_class in RiskClass for a in ACTIONS.values()))
    check("R2 read-only actions never require confirmation",
          all(not a.requires_confirmation for a in ACTIONS.values()
              if a.risk_class == RiskClass.READ_ONLY))
    check("R3 external/destructive/privileged require confirmation",
          all(a.requires_confirmation for a in ACTIONS.values()
              if a.risk_class in (RiskClass.EXTERNAL_WRITE,
                                  RiskClass.DESTRUCTIVE,
                                  RiskClass.PRIVILEGED)))
    # every tool action is registered (single source of truth)
    reg = build_default_registry(c)
    for tool in reg.all():
        check(f"R4 tool action registered: {tool.action}",
              is_registered(tool.action))
    # every ActionKind is registered
    for kind in ActionKind:
        check(f"R5 ActionKind registered: {kind.value}",
              is_registered(kind.value))

    # unknown -> DENY (fail closed), even when "confirmed"
    for name in ("unknown_future_action", "totally_unknown_action",
                 "frobnicate", "delete_everything", "exfiltrate",
                 "another_user_record", "shell", "rm", "file_delete",
                 "delete_files"):
        d = c.safety.check(name, actor="u", confirmed=False)
        check(f"R6 unconfirmed {name} denied", d.allow is False, d.reason)
        d2 = c.safety.check(name, actor="u", confirmed=True)
        check(f"R6b {name} still denied when 'confirmed'", d2.allow is False,
              d2.reason)
    for name in ("telegram_send", "send_message", "ha_call", "gcal_write",
                 "gcal_delete", "organize", "trash", "nas_ingest",
                 "course_monitor", "workspace"):
        check(f"R7 external {name} needs confirmation",
              c.safety.check(name, actor="u", confirmed=False).allow is False)
    for name in ("search", "find", "status", "tasks", "web_search",
                 "knowledge_lookup", "tracker_list", "context"):
        check(f"R8 read-only {name} allowed",
              c.safety.check(name, actor="u").allow is True)
    for name in ("food_add", "add_task", "memory_learn", "project_create"):
        check(f"R9 local mutation {name} allowed",
              c.safety.check(name, actor="u").allow is True)
    # unknown classifies as UNKNOWN (not external)
    check("R10 unknown classifies UNKNOWN",
          c.safety.classify("some_future_unknown") == ActionClass.UNKNOWN)
    check("R11 needs_confirmation fails closed",
          c.safety.needs_confirmation("some_future_unknown") is True)

    # corpus-driven safety checks
    rows = load_jsonl("safety.jsonl")
    check("R12 safety corpus has cases", len(rows) >= 60, str(len(rows)))
    for row in rows:
        d = c.safety.check(row["action"], actor="u",
                           confirmed=row["confirmed"])
        check(f"R13 safety {row['action']} confirmed={row['confirmed']}",
              d.allow is row["expected_allow"],
              f"allow={d.allow} expected={row['expected_allow']}")

    # generated unknown names must all fail closed
    for i in range(20):
        name = f"unregistered_action_{i}"
        check(f"R14 generated unknown {name} denied",
              c.safety.check(name, actor="u", confirmed=True).allow is False)

    # degraded mode still blocks external + non-allowlisted local writes
    c.cfg.degraded_mode = True
    check("R15 degraded blocks external",
          c.safety.check("gcal_write", actor="u", confirmed=True).allow is False)
    check("R16 degraded blocks non-allowlisted local write",
          c.safety.check("schedule_change", actor="u").allow is False)
    check("R17 degraded allows Butler-owned write",
          c.safety.check("food_add", actor="u").allow is True)
    c.cfg.degraded_mode = False


# =====================================================================
# 2. temporal resolution (P4)
# =====================================================================
def _resolver(now: datetime) -> TemporalResolver:
    tz = ZoneInfo("UTC")
    clock = Clock(tz=tz, now_ts=int(now.timestamp()))
    cls = [(int(datetime(2026, 9, 9, 14, 0, tzinfo=tz).timestamp()),
            int(datetime(2026, 9, 9, 15, 0, tzinfo=tz).timestamp()), "CS188")]
    meet = [(int(datetime(2026, 9, 9, 15, 0, tzinfo=tz).timestamp()),
             int(datetime(2026, 9, 9, 16, 0, tzinfo=tz).timestamp()), "sync")]
    return TemporalResolver(clock, class_windows=cls, meeting_windows=meet)


def test_temporal() -> None:
    print("\n== temporal resolution (P4) ==")
    # Wednesday 2026-09-09 10:00
    now = datetime(2026, 9, 9, 10, 0, tzinfo=ZoneInfo("UTC"))
    r = _resolver(now)
    for row in load_jsonl("temporal.jsonl"):
        phrase = row["input"]
        resolvable = row["expected_resolvable"]
        t = r.resolve(phrase)
        if resolvable:
            check(f"T1 resolves {phrase!r}", t.is_resolved(),
                  f"{t.resolution.value}")
        else:
            check(f"T1 unresolved {phrase!r}", not t.is_resolved(),
                  f"{t.resolution.value}")
    # explicit windows
    t = r.resolve("after lunch")
    check("T2 after lunch starts at 13:00",
          t.start == int(datetime(2026, 9, 9, 13, 0,
                                  tzinfo=ZoneInfo("UTC")).timestamp()))
    t = r.resolve("next Tuesday")
    check("T3 next Tuesday is 2026-09-15",
          t.start == int(datetime(2026, 9, 15, tzinfo=ZoneInfo("UTC")).timestamp()))
    t = r.resolve("before my meeting")
    check("T4 before my meeting ends at 15:00",
          t.end == int(datetime(2026, 9, 9, 15, 0,
                                tzinfo=ZoneInfo("UTC")).timestamp()))
    t = r.resolve("tomorrow morning")
    check("T5 tomorrow morning 08:00-12:00",
          t.start == int(datetime(2026, 9, 10, 8, tzinfo=ZoneInfo("UTC")).timestamp())
          and t.end == int(datetime(2026, 9, 10, 12, tzinfo=ZoneInfo("UTC")).timestamp()))
    t = r.resolve("next weekend")
    check("T6 next weekend is Sat-Sun",
          t.start == int(datetime(2026, 9, 12, tzinfo=ZoneInfo("UTC")).timestamp())
          and t.end == int(datetime(2026, 9, 14, tzinfo=ZoneInfo("UTC")).timestamp()))
    # generated dayparts/weekdays
    for dp, res in (("morning", True), ("afternoon", True),
                    ("evening", True)):
        check(f"T7 tomorrow {dp} resolves",
              _resolver(now).resolve(f"tomorrow {dp}").is_resolved() is res)
    for wd in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
               "Saturday", "Sunday"):
        check(f"T8 next {wd} resolves",
              _resolver(now).resolve(f"next {wd}").is_resolved())
    # no calendar context -> unresolved (ask), never invent
    bare = TemporalResolver(Clock(tz=ZoneInfo("UTC"),
                                  now_ts=int(now.timestamp())))
    check("T9 before my meeting without calendar -> unresolved",
          not bare.resolve("before my meeting").is_resolved())
    check("T10 after my lecture without calendar -> unresolved",
          not bare.resolve("after my lecture").is_resolved())


# =====================================================================
# 3. conversation state / follow-ups (P2)
# =====================================================================
def _service(c: Container) -> ExecutiveService:
    return ExecutiveService(c, interpreter=DeterministicInterpreter(c))


def test_interactions() -> None:
    print("\n== conversation state (P2) ==")
    c = fresh("hard-conv-")
    c1 = c.db.add_course("CS188", "Intro to AI")
    c2 = c.db.add_course("CS168", "Networks")
    c.projects.create_project("Project 2", course_id=c1)
    c.projects.create_project("Project 2", course_id=c2)
    svc = _service(c)
    s = svc.session("u")

    r1 = svc.ask(text="Add Project 2.", user="u")
    check("C1 ambiguous request is AMBIGUOUS", r1.status == ResultStatus.AMBIGUOUS)
    clar = svc.interactions.active(s, InteractionKind.CLARIFICATION)
    check("C2 clarification state opened", clar is not None)
    check("C3 candidates are qualified",
          clar is not None and all("qualifiers" in c for c in clar.candidates),
          str(clar.candidates if clar else None))
    r2 = svc.ask(text="The CS188 one.", user="u")
    check("C4 follow-up resolves against pending candidates",
          svc.interactions.active(s, InteractionKind.CLARIFICATION) is None)
    check("C5 resolved focus is the CS188 project",
          s.focus_entity is not None and s.focus_entity.id == "1",
          str(s.focus_entity))

    # pending proposal modification
    svc2 = _service(c)
    s2 = svc2.session("v")
    p1 = svc2.ask(text="Track CS188 homework.", user="v")
    conf = svc2.interactions.active(s2, InteractionKind.CONFIRMATION)
    check("C6 pending confirmation recorded", conf is not None)
    p2 = svc2.ask(text="Only deadline changes.", user="v")
    check("C7 follow-up modifies the pending proposal",
          p2.status == ResultStatus.NEEDS_CONFIRMATION, p2.status.value)
    check("C8 modification does not create a second tracker",
          c.db.one("SELECT COUNT(*) n FROM trackers")["n"] == 1)
    check("C9 modification is acknowledged", "update" in (p2.answer or "").lower(),
          p2.answer)

    # expiry never applies stale state
    svc3 = _service(c)
    s3 = svc3.session("w")
    svc3.ask(text="Add Project 2.", user="w")
    cl = svc3.interactions.active(s3, InteractionKind.CLARIFICATION)
    check("C10 clarification opened for expiry test", cl is not None)
    if cl is not None:
        cl.expires_at = 1
    r = svc3.ask(text="The CS188 one.", user="w")
    check("C11 expired state is not applied",
          any("expired" in w.lower() for w in r.warnings), str(r.warnings))

    # store unit behaviour
    store = InteractionStore(ttl=10)
    class _S:
        def __init__(self): self.data = {}
    s4 = _S()
    it = store.open(s4, kind=InteractionKind.CLARIFICATION, original_text="x")
    check("C12 interaction is open", store.active(s4) is not None)
    it.expires_at = 1
    expired = store.purge_expired(s4)
    check("C13 expired interaction purged", expired and store.active(s4) is None)
    check("C14 expired status set", expired[0].status == "expired")

    # deterministic candidate matching
    cands = [{"name": "Project 2", "qualifiers": ["CS188"], "type": "project",
              "id": "1"},
             {"name": "Project 2", "qualifiers": ["CS168"], "type": "project",
              "id": "2"}]
    check("C15 qualifier match picks CS188",
          (match_candidates("the CS188 one", cands) or {}).get("id") == "1")
    check("C16 qualifier match picks CS168",
          (match_candidates("the CS168 one", cands) or {}).get("id") == "2")
    check("C17 ordinal match picks second",
          (match_candidates("the second one", cands) or {}).get("id") == "2")
    check("C18 no match returns None",
          match_candidates("something unrelated", cands) is None)


# =====================================================================
# 4. schema / validation / prompt design (P1.1/P3)
# =====================================================================
def test_schema_validation() -> None:
    print("\n== schema / validation / prompt ==")
    bad = [
        {"action": "not_real", "confidence": 0.5},
        {"action": "recommend", "confidence": 2.0},
        {"action": "recommend", "confidence": -1},
        {"action": "recommend", "confidence": 0.5, "bogus": 1},
        {"action": "recommend", "confidence": 0.5,
         "constraints": [{"hardness": "hard", "source": "routine_inferred"}]},
        {"action": "recommend", "confidence": 0.5,
         "constraints": [{"source": "preference_learned", "hardness": "hard"}]},
    ]
    for i, payload in enumerate(bad):
        try:
            AgentRequest.from_dict(payload, strict=True)
            ok = False
        except Exception:
            ok = True
        check(f"V{i + 1} invalid payload rejected", ok, str(payload)[:50])
    for action in (ActionKind.MOVE, ActionKind.UPDATE, ActionKind.COMPLETE_TASK):
        try:
            AgentRequest.from_dict({"action": action.value, "confidence": 0.9},
                                   strict=True)
            ok = False
        except Exception:
            ok = True
        check(f"V{action.value} without target rejected", ok)
    prompt = schema_prompt()
    for needle in ("DISAMBIGUATION EXAMPLES", "QUERY", "UPDATE",
                   "settings_update", "tracker_create", "link_items"):
        check(f"P1 prompt teaches {needle}", needle in prompt)
    check("P2 relevant_actions is bounded",
          len(relevant_actions({"purpose": "food and pantry"})) <= 10)
    check("P3 relevant_actions is topic-aware",
          "create_item" in relevant_actions({"purpose": "course CS188"}))
    check("P4 schema exposes parameters",
          "parameters" in request_json_schema()["properties"])
    # model-supplied ids are discarded
    payload = {"action": "recommend", "confidence": 0.9,
               "target": {"type": "course", "name": "CS188", "id": "999",
                          "resolved": True}}
    req = AgentRequest.from_dict(payload, strict=True)
    LLMInterpreter._sanitize(req)
    check("V5 model-supplied id discarded", req.target.id == "")
    check("V6 model-supplied resolved flag discarded",
          req.target.resolved is False)


# =====================================================================
# 5. corpora well-formedness
# =====================================================================
def test_corpora() -> None:
    print("\n== eval corpora ==")
    for name, minimum in (("intent.jsonl", 50), ("temporal.jsonl", 60),
                          ("ambiguity.jsonl", 15), ("followup.jsonl", 10),
                          ("safety.jsonl", 60)):
        rows = load_jsonl(name)
        check(f"E {name} present and sized", len(rows) >= minimum,
              str(len(rows)))
    intent = load_jsonl("intent.jsonl")
    check("E intent has expected action+scope",
          all("expected_action" in r and "expected_scope" in r for r in intent))
    temporal = load_jsonl("temporal.jsonl")
    check("E temporal has resolvable flag",
          all("expected_resolvable" in r for r in temporal))
    safety = load_jsonl("safety.jsonl")
    check("E safety has expected_allow",
          all("expected_allow" in r for r in safety))
    # every intent expected action is a real ActionKind
    check("E intent actions are valid ActionKinds",
          all(r["expected_action"] in {k.value for k in ActionKind}
              or r["expected_action"] == "unknown" for r in intent))


def main() -> int:
    test_registry_and_safety()
    test_temporal()
    test_interactions()
    test_schema_validation()
    test_corpora()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
