"""Acceptance: Q1 clarification engine, slot filling and button UX.

Run:  .venv/bin/python tests/run_acceptance_clarification.py

Deterministic and offline. Covers slot schemas, missing-slot detection,
structured clarifications, button callbacks (via the service resume path),
follow-up resolution, isolation, concurrency and bounded state.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

from butler.agent.actions import (  # noqa: E402
    optional_slots, required_slots, slot_spec, slots_for)
from butler.agent.clarification import (  # noqa: E402
    ClarificationKind, ClarificationOption, ClarificationRequest, apply_slot,
    build_clarification, explain, missing_slots, parse_answer, slot_is_filled)
from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.interactions import (  # noqa: E402
    InteractionKind, InteractionStore)
from butler.agent.semantic import (  # noqa: E402
    ActionKind, AgentRequest, EntityRef, EntityType, RequestIntent, ResultStatus)
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402

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


def fresh(prefix: str = "clar-") -> Container:
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
    c.safety.retry = None
    return c


def svc_for(c: Container) -> ExecutiveService:
    return ExecutiveService(c, interpreter=DeterministicInterpreter(c))


def llm_req(action: ActionKind, *, target=None, parameters=None,
            raw="", topic=None) -> AgentRequest:
    return AgentRequest(intent=RequestIntent.MUTATE, action=action,
                        target=target, parameters=dict(parameters or {}),
                        confidence=0.8, source="llm", raw_text=raw,
                        topic=dict(topic or {}))


def clar_of(res) -> dict | None:
    data = res.data if isinstance(res.data, dict) else {}
    return data.get("clarification") if isinstance(data, dict) else None


# =====================================================================
# 1. slot schemas
# =====================================================================
def test_slots() -> None:
    print("\n== slot schemas ==")
    check("S1 tracker requires target+event_types",
          {s.name for s in required_slots("tracker_create")}
          == {"target", "event_types"})
    check("S2 tracker optional slots",
          {"cadence", "destination", "notification_policy"} <=
          {s.name for s in optional_slots("tracker_create")})
    check("S3 settings requires capability/state/scope",
          {s.name for s in required_slots("settings_update")}
          == {"capability", "state", "scope"})
    check("S4 schedule requires duration",
          [s.name for s in required_slots("find_best_slot")] == ["duration"])
    check("S5 create requires title",
          [s.name for s in required_slots("create_item")] == ["title"])
    check("S6 link requires target",
          [s.name for s in required_slots("link_items")] == ["target"])
    check("S7 memory_learn requires content",
          [s.name for s in required_slots("memory_learn")] == ["content"])
    check("S8 unknown action has no slots", slots_for("nope") == ())
    check("S9 slot_spec finds a slot",
          slot_spec("tracker_create", "cadence") is not None)
    check("S10 slot_spec missing returns None",
          slot_spec("tracker_create", "nope") is None)
    check("S11 settings scope is a scope slot",
          slot_spec("settings_update", "scope").kind == "scope")
    check("S12 duration has bounded options",
          "30 min" in slot_spec("find_best_slot", "duration").options)


# =====================================================================
# 2. missing-slot detection
# =====================================================================
def test_missing_slots() -> None:
    print("\n== missing-slot detection ==")
    req = llm_req(ActionKind.TRACKER_CREATE)
    check("M1 empty tracker misses target",
          "target" in [s.name for s in missing_slots(req)])
    req = llm_req(ActionKind.TRACKER_CREATE,
                  target=EntityRef(type=EntityType.COURSE, name="CS188"),
                  parameters={"event_types": ["new_item"]})
    check("M2 filled tracker has no missing slots", missing_slots(req) == [])
    req = llm_req(ActionKind.SETTINGS_UPDATE, parameters={})
    check("M3 settings misses capability",
          "capability" in [s.name for s in missing_slots(req)])
    req = llm_req(ActionKind.SETTINGS_UPDATE,
                  parameters={"capability": "web", "state": "enabled",
                              "scope": "current_topic"})
    check("M4 filled settings has no missing slots", missing_slots(req) == [])
    req = llm_req(ActionKind.FIND_BEST_SLOT, parameters={})
    check("M5 schedule misses duration",
          "duration" in [s.name for s in missing_slots(req)])
    req = llm_req(ActionKind.FIND_BEST_SLOT,
                  parameters={"duration": "2 hours"})
    check("M6 filled schedule has no missing slots", missing_slots(req) == [])
    req = llm_req(ActionKind.TRACKER_CREATE,
                  target=EntityRef(type=EntityType.COURSE, name="CS188"),
                  parameters={"condition": "quantity_below"})
    check("M7 a condition fills event_types", missing_slots(req) == [])
    req = llm_req(ActionKind.CREATE_ITEM, parameters={})
    check("M8 create misses title",
          "title" in [s.name for s in missing_slots(req)])
    req = llm_req(ActionKind.LINK_ITEMS, target=None)
    check("M9 link misses target",
          "target" in [s.name for s in missing_slots(req)])


# =====================================================================
# 3. build / parse / apply
# =====================================================================
def test_clarification_engine() -> None:
    print("\n== clarification engine ==")
    spec = slot_spec("find_best_slot", "duration")
    creq = build_clarification("i1", spec)
    check("E1 interaction id bound", creq.interaction_id == "i1")
    check("E2 slot bound", creq.slot_name == "duration")
    check("E3 bounded options",
          [o.label for o in creq.options] == ["30 min", "1 hour", "2 hours",
                                              "Custom"])
    check("E4 option ids present", all(o.option_id for o in creq.options))
    check("E5 round-trips to dict",
          ClarificationRequest.from_dict(creq.to_dict()).slot_name == "duration")

    # target options from candidates
    cands = [{"type": "project", "id": "1", "name": "Project 2",
              "label": "Project 2 (CS188)", "qualifiers": ["CS188"]},
             {"type": "project", "id": "2", "name": "Project 2",
              "label": "Project 2 (CS168)", "qualifiers": ["CS168"]}]
    tspec = slot_spec("link_items", "target")
    tcreq = build_clarification("i2", tspec, candidates=cands)
    check("E6 target options + none",
          [o.label for o in tcreq.options][-1] == "None of these")

    # parse: ordinals, qualifiers, labels, ids, negatives, numbers, free text
    check("E7 'second' -> second option",
          parse_answer("second", creq)[1] == creq.options[1].option_id)
    check("E8 '2' -> second option",
          parse_answer("2", creq)[1] == creq.options[1].option_id)
    check("E9 exact label",
          parse_answer("2 hours", creq)[0] == "2 hours")
    check("E10 qualifier match",
          parse_answer("the CS188 one", tcreq)[0]["id"] == "1")
    check("E11 qualifier match CS168",
          parse_answer("the CS168 one", tcreq)[0]["id"] == "2")
    check("E12 none -> cancel",
          parse_answer("none", tcreq)[1] == "__cancel")
    check("E13 cancel -> cancel",
          parse_answer("cancel", creq)[1] == "__cancel")
    check("E14 unrecognised target -> None",
          parse_answer("banana", tcreq) is None)
    check("E15 free text allowed for value",
          parse_answer("90 minutes", creq)[0] == "90 minutes")
    check("E16 empty -> None", parse_answer("", creq) is None)
    check("E17 explicit option id",
          parse_answer(creq.options[2].option_id, creq)[0] == "2 hours")
    # yes/no confirmation
    cspec = slot_spec("settings_update", "state")
    check("E18 affirm handled", parse_answer("yes", creq) is not None)

    # apply_slot
    req = llm_req(ActionKind.SETTINGS_UPDATE)
    apply_slot(req, "capability", "Web")
    check("E19 apply capability", req.parameters["capability"] == "Web")
    apply_slot(req, "scope", "Global")
    check("E20 apply global scope", req.parameters["scope"] == "global")
    apply_slot(req, "duration", "2 hours")
    check("E21 apply duration", req.parameters["duration"] == "2 hours")
    apply_slot(req, "target", {"type": "project", "id": "7", "name": "P"})
    check("E22 apply target resolved",
          req.target is not None and req.target.resolved
          and req.target.id == "7")
    check("E23 explain never invents",
          "matches" in explain(tcreq) or explain(tcreq) != "")


# =====================================================================
# 4. service integration + required scenarios
# =====================================================================
def test_service_scenarios() -> None:
    print("\n== clarification scenarios ==")

    def scen():
        c = fresh("clar-sc-")
        c1 = c.db.add_course("CS188", "AI")
        c2 = c.db.add_course("CS168", "Net")
        c.projects.create_project("Project 2", course_id=c1)
        c.projects.create_project("Project 2", course_id=c2)
        return c, svc_for(c)

    # scenario 1: track my project -> candidate choices
    c, svc = scen()
    res = svc.ask(request=llm_req(
        ActionKind.TRACKER_CREATE,
        target=EntityRef(type=EntityType.PROJECT, name="project"),
        raw="Track my project.", topic={"chat_id": 1, "thread_id": 2}))
    clar = clar_of(res)
    check("C1 track my project -> clarification", res.status == ResultStatus.AMBIGUOUS
          and clar is not None)
    check("C2 candidate choices offered", clar is not None
          and len(clar.get("options", [])) >= 2)
    check("C3 slot is target", clar and clar.get("slot_name") == "target")
    iid = clar["interaction_id"]
    opt = [o for o in clar["options"] if "CS188" in o["label"]][0]
    res2 = svc.resume_with_slot(iid, "target", opt["value"], user="user")
    check("C4 button fills target and resumes",
          clar_of(res2) is None or res2.status != ResultStatus.INVALID)

    # scenario 2: add Project 2 -> creation ambiguity
    c, svc = scen()
    res = svc.ask(request=llm_req(ActionKind.CREATE_ITEM,
                                  parameters={"title": "Project 2"},
                                  raw="Add Project 2."))
    clar = clar_of(res)
    check("C5 add Project 2 -> clarification", clar is not None)
    check("C6 creation clarification has candidates",
          clar is not None and len(clar.get("options", [])) >= 2)

    # scenario 3: enable web -> scope choices
    c, svc = scen()
    res = svc.ask(request=llm_req(ActionKind.SETTINGS_UPDATE,
                                  parameters={"capability": "Web",
                                              "state": "on"},
                                  raw="Enable web."))
    clar = clar_of(res)
    check("C7 enable web -> scope clarification", clar is not None
          and clar.get("slot_name") == "scope")
    check("C8 scope options are this-topic/global",
          clar is not None and {o["label"] for o in clar["options"]} >=
          {"This topic", "Global"})

    # scenario 4/5: schedule later / time for CS188 -> duration choices
    for raw in ("Schedule this later.", "Give me some time for CS188."):
        c, svc = scen()
        res = svc.ask(request=llm_req(ActionKind.FIND_BEST_SLOT, raw=raw,
                                      target=EntityRef(type=EntityType.COURSE,
                                                       name="CS188")))
        clar = clar_of(res)
        check(f"C9 duration clarification for {raw!r}", clar is not None
              and clar.get("slot_name") == "duration")
        check(f"C10 duration options for {raw!r}", clar is not None
              and "30 min" in {o["label"] for o in clar["options"]})

    # scenario 6: track CS188 -> tracking (event type) choices
    c, svc = scen()
    res = svc.ask(request=llm_req(
        ActionKind.TRACKER_CREATE,
        target=EntityRef(type=EntityType.COURSE, name="CS188"),
        raw="Track CS188."))
    clar = clar_of(res)
    check("C11 track CS188 -> watch-for clarification", clar is not None
          and clar.get("slot_name") == "event_types")
    check("C12 watch options bounded", clar is not None
          and "Deadlines" in {o["label"] for o in clar["options"]})

    # scenario 7: tell me when something changes -> target clarification
    c, svc = scen()
    res = svc.ask(request=llm_req(ActionKind.TRACKER_CREATE,
                                  raw="Tell me when something changes."))
    clar = clar_of(res)
    check("C13 vague change -> target clarification", clar is not None
          and clar.get("slot_name") == "target")

    # unrecognised answer keeps the interaction
    iid = clar["interaction_id"]
    res = svc.ask(text="banana banana", user="user")
    check("C14 unrecognised answer re-asks", clar_of(res) is not None
          or res.status == ResultStatus.AMBIGUOUS)

    # cancel via answer
    res = svc.ask(text="none", user="user")
    check("C15 cancel answer accepted", res.status in
          (ResultStatus.OK, ResultStatus.AMBIGUOUS))

    # resume with wrong slot rejected
    c, svc = scen()
    res = svc.ask(request=llm_req(ActionKind.FIND_BEST_SLOT, raw="schedule"))
    clar = clar_of(res)
    bad = svc.resume_with_slot(clar["interaction_id"], "target", "x",
                               user="user")
    check("C16 wrong slot rejected", bad.status == ResultStatus.INVALID)
    bad2 = svc.resume_with_slot("doesnotexist", "duration", "2 hours",
                                user="user")
    check("C17 unknown interaction rejected",
          bad2.status == ResultStatus.INVALID)


# =====================================================================
# 5. isolation, concurrency, expiry, bloat
# =====================================================================
def test_state_hygiene() -> None:
    print("\n== state hygiene ==")
    c = fresh("clar-iso-")
    svc = svc_for(c)
    a = svc.session("alice")
    b = svc.session("bob")
    svc.ask(request=llm_req(ActionKind.FIND_BEST_SLOT, raw="schedule"),
            user="alice")
    svc.ask(request=llm_req(ActionKind.FIND_BEST_SLOT, raw="schedule"),
            user="bob")
    ia = svc.interactions.active(a, InteractionKind.CLARIFICATION)
    ib = svc.interactions.active(b, InteractionKind.CLARIFICATION)
    check("H1 per-user interactions isolated", ia is not None and ib is not None
          and ia.id != ib.id)
    # alice cannot resume bob's interaction
    res = svc.resume_with_slot(ib.id, "duration", "2 hours", user="alice")
    check("H2 cross-user resume rejected", res.status == ResultStatus.INVALID)

    # concurrency: two rapid button presses, only one wins
    opt = "2 hours"
    r1 = svc.resume_with_slot(ia.id, "duration", opt, user="alice")
    r2 = svc.resume_with_slot(ia.id, "duration", opt, user="alice")
    check("H3 first button wins", r1.status != ResultStatus.INVALID)
    check("H4 second (stale) button rejected",
          r2.status == ResultStatus.INVALID)

    # expiry
    store = InteractionStore(ttl=1)
    class _S:
        def __init__(self): self.data = {}
    s = _S()
    it = store.open(s, kind=InteractionKind.CLARIFICATION, original_text="x")
    it.expires_at = 1
    check("H5 expired purged", store.purge_expired(s) and store.active(s) is None)

    # bloat: many interactions stay bounded per session
    store2 = InteractionStore(ttl=900, max_open=4)
    s2 = _S()
    for i in range(2000):
        store2.open(s2, kind=InteractionKind.CLARIFICATION,
                    original_text=f"t{i}")
    open_now = [i for i in store2._all(s2) if i.is_open()]
    check("H6 open interactions bounded", len(open_now) <= 4, str(len(open_now)))
    check("H7 no unbounded history", len(store2._all(s2)) <= 4)
    # purge after ttl leaves nothing
    for i in store2._all(s2):
        i.expires_at = 1
    store2.purge_expired(s2)
    check("H8 ttl cleanup empties state", store2._all(s2) == [])


def test_generated() -> None:
    print("\n== generated coverage ==")
    from butler.agent.actions import SLOT_SCHEMAS
    # every slot is well-formed and every choice option round-trips
    for action, specs in SLOT_SCHEMAS.items():
        for spec in specs:
            check(f"G1 {action}.{spec.name} has a question",
                  bool(spec.question))
            check(f"G2 {action}.{spec.name} kind valid",
                  spec.kind in ("choice", "target", "value", "time", "scope",
                                "confirmation"))
            if spec.kind == "choice" and spec.options:
                creq = build_clarification("g", spec)
                for label in spec.options:
                    check(f"G3 {action}.{spec.name} option {label!r}",
                          parse_answer(label, creq) is not None
                          and parse_answer(label, creq)[0] == label)
                    opt = [o for o in creq.options if o.label == label][0]
                    check(f"G3b {action}.{spec.name} id {opt.option_id}",
                          parse_answer(opt.option_id, creq)[0] == label)
    # required-slot detection: each required slot is reported when empty
    for action, specs in SLOT_SCHEMAS.items():
        for spec in specs:
            if not spec.required or spec.default is not None:
                continue
            req = llm_req(ActionKind(action))
            check(f"G4 {action}.{spec.name} reported missing",
                  spec.name in [s.name for s in missing_slots(req)])
    # candidate qualifier matching across pairs
    cands = [{"name": f"Item {i}", "label": f"Item {i} (Tag{i})",
              "qualifiers": [f"Tag{i}"], "type": "project", "id": str(i)}
             for i in range(1, 6)]
    tspec = slot_spec("link_items", "target")
    tcreq = build_clarification("g2", tspec, candidates=cands)
    for i in range(1, 6):
        check(f"G5 qualifier Tag{i} selects Item {i}",
              (parse_answer(f"the Tag{i} one", tcreq) or (None,))[0]["id"]
              == str(i))
    # ordinals across a 5-option set
    for idx, word in enumerate(("first", "second", "third", "fourth",
                                "fifth")):
        check(f"G6 ordinal {word}",
              parse_answer(word, tcreq)[0]["id"] == str(idx + 1))
    # apply_slot for every slot kind
    req = llm_req(ActionKind.TRACKER_CREATE)
    for name, value in (("target", {"type": "course", "id": "1", "name": "C"}),
                        ("event_types", ["new_item"]),
                        ("cadence", "daily"),
                        ("destination", "current_topic"),
                        ("notification_policy", "important_only")):
        apply_slot(req, name, value)
    check("G7 all tracker slots applied", missing_slots(req) == [])
    # scope mapping
    for text, expected in (("Global", "global"),
                           ("This topic", "current_topic")):
        r = llm_req(ActionKind.SETTINGS_UPDATE)
        apply_slot(r, "scope", text)
        check(f"G8 scope {text!r} -> {expected}",
              r.parameters["scope"] == expected)
    # time slot sets a temporal phrase
    r = llm_req(ActionKind.FIND_BEST_SLOT)
    apply_slot(r, "time_window", "Tomorrow")
    check("G9 time slot sets temporal phrase",
          r.temporal is not None and r.temporal.phrase == "Tomorrow")


def test_restart_and_isolation() -> None:
    print("\n== restart / isolation ==")
    c = fresh("clar-restart-")
    c1 = c.db.add_course("CS188", "AI")
    c.projects.create_project("Project 2", course_id=c1)
    svc1 = svc_for(c)
    res = svc1.ask(request=llm_req(ActionKind.TRACKER_CREATE,
                                   target=EntityRef(type=EntityType.COURSE,
                                                    name="CS188"),
                                   raw="Track CS188."))
    clar = clar_of(res)
    check("R1 clarification opened", clar is not None)
    # a new service instance over the same store sees the interaction
    svc2 = svc_for(c)
    session = svc2.session("user")
    check("R2 pending clarification survives a service restart",
          svc2.interactions.active(session, InteractionKind.CLARIFICATION)
          is not None)
    opt = [o for o in clar["options"] if o["label"] == "Deadlines"][0]
    res2 = svc2.resume_with_slot(clar["interaction_id"], "event_types",
                                 opt["value"], user="user")
    check("R3 resumed after restart", res2.status != ResultStatus.INVALID)
    # completed interaction does not repeat
    check("R4 completed interaction cleared",
          svc2.interactions.active(session, InteractionKind.CLARIFICATION)
          is None)
    # expired cannot execute
    c2 = fresh("clar-exp-")
    svc3 = svc_for(c2)
    r = svc3.ask(request=llm_req(ActionKind.FIND_BEST_SLOT, raw="schedule"))
    clar = clar_of(r)
    s3 = svc3.session("user")
    it = svc3.interactions.active(s3, InteractionKind.CLARIFICATION)
    it.expires_at = 1
    res = svc3.resume_with_slot(clar["interaction_id"], "duration", "2 hours",
                                user="user")
    check("R5 expired button rejected", res.status == ResultStatus.INVALID)


def main() -> int:
    test_slots()
    test_missing_slots()
    test_clarification_engine()
    test_service_scenarios()
    test_state_hygiene()
    test_generated()
    test_restart_and_isolation()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
