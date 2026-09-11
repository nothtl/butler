"""Q7 acceptance: targetless state-query dispatch bypasses target resolution.
Deterministic/offline."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

from butler.agent.semantic import (  # noqa: E402
    ActionKind, AgentRequest, EntityRef, EntityType, RequestIntent, ResultStatus)
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.tracking import SnapshotProvider  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def fresh(prefix="q7-"):
    base = tempfile.mkdtemp(prefix=prefix, dir="/tmp/opencode")
    cfg = Config()
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


def req(action, subject=None, target=None, raw="x"):
    params = {"query_subject": subject} if subject else {}
    return AgentRequest(intent=RequestIntent.QUERY, action=action, target=target,
                        parameters=params, confidence=0.8, source="llm",
                        raw_text=raw)


def main():
    c = fresh()
    c.db.add_course("CS188", "AI")
    c.trackers.register_provider(SnapshotProvider(c, {}))
    c.trackers.create(name="CS188 Fall 26", source="course",
                      target_type="course", target_ref="CS188",
                      condition={"type": "new_item"},
                      action={"type": "CREATE_SUGGESTION", "params": {}},
                      destination={"chat_id": 1, "thread_id": 7}, cadence=86400)
    prof, _ = c.topics.ensure(1, 7, "CS188")
    c.topics.update(prof, purpose="Course management", status="active",
                    capabilities={**prof.capabilities, "tracking": "enabled",
                                  "web": "enabled"})
    c.topics.add_link(c.topics.get(1, 7), "course", 1, "about", 0.9, "user")
    svc = ExecutiveService(c)
    topic = {"chat_id": 1, "thread_id": 7}

    # 1. target variants must all produce a state answer, never AMBIGUOUS
    variants = [
        ("none", None),
        ("empty-entity", EntityRef()),
        ("empty-name", EntityRef(name="")),
        ("unknown-name", EntityRef(type=EntityType.UNKNOWN, name="here")),
        ("spurious", EntityRef(name="monitoring")),
    ]
    combos = [(ActionKind.STATUS, "capabilities"), (ActionKind.STATUS, "connections"),
              (ActionKind.TRACKER_LIST, "tracking"), (ActionKind.SETTINGS_VIEW, None)]
    for action, subject in combos:
        for vname, target in variants:
            r = svc.ask(request=req(action, subject, target), topic=topic)
            d = r.data if isinstance(r.data, dict) else {}
            check(f"D1 {action.value}/{subject or '-'} {vname} answers",
                  r.status == ResultStatus.OK and bool(d.get("text")),
                  f"{r.status.value}")

    # 2. targeted state query still resolves (real target)
    r = svc.ask(request=req(ActionKind.TRACKER_LIST, "tracking",
                            EntityRef(type=EntityType.COURSE, name="CS188")),
                topic=topic)
    check("D2 targeted state query is not AMBIGUOUS",
          r.status != ResultStatus.AMBIGUOUS)
    check("D3 targeted state query answers",
          bool((r.data or {}).get("text")))

    # 3. state queries must not mutate
    before = {t: c.db.one(f"SELECT COUNT(*) n FROM {t}")["n"]
              for t in ("trackers", "topic_links")}
    for action, subject in combos:
        svc.ask(request=req(action, subject), topic=topic)
    after = {t: c.db.one(f"SELECT COUNT(*) n FROM {t}")["n"]
             for t in ("trackers", "topic_links")}
    check("D4 state queries create no trackers", before["trackers"] == after["trackers"])
    check("D5 state queries create no links", before["topic_links"] == after["topic_links"])

    # 4. every enabled capability appears in the capabilities answer
    cap = svc.ask(request=req(ActionKind.STATUS, "capabilities"),
                  topic=topic).data.get("text") or ""
    for label in ("Know", "Track", "Web"):
        check(f"D6 capabilities answer includes {label}", label in cap)

    # 5. tracking answer reflects live state, and updates after change
    t = svc.ask(request=req(ActionKind.TRACKER_LIST, "tracking"),
                topic=topic).data.get("text") or ""
    check("D7 tracking answer names the tracker", "CS188 Fall 26" in t)
    c.trackers.update(c.trackers.list()[0], state="paused")
    t2 = svc.ask(request=req(ActionKind.TRACKER_LIST, "tracking"),
                 topic=topic).data.get("text") or ""
    check("D8 tracking answer reflects state change", "⏸" in t2 or "paused" in t2)

    # 6. no trackers -> honest, topic-scoped
    c2 = fresh("q7-notrk-")
    p2, _ = c2.topics.ensure(1, 9, "Ideas")
    c2.topics.update(p2, purpose="Ideas", status="active",
                     capabilities=dict(p2.capabilities))
    svc2 = ExecutiveService(c2)
    t3 = svc2.ask(request=req(ActionKind.TRACKER_LIST, "tracking"),
                  topic={"chat_id": 1, "thread_id": 9}).data.get("text") or ""
    check("D9 no trackers is honest", "not currently monitoring" in t3)

    # 7. generated paraphrase dispatch (subject-driven, no wording)
    subjects = ["capabilities", "tracking", "configuration", "connections"]
    phrases = [f"query {i}" for i in range(25)]
    for subj in subjects:
        action = {"capabilities": ActionKind.STATUS, "tracking": ActionKind.TRACKER_LIST,
                  "configuration": ActionKind.SETTINGS_VIEW,
                  "connections": ActionKind.STATUS}[subj]
        for ph in phrases:
            r = svc.ask(request=req(action, subj, raw=ph), topic=topic)
            check(f"D10 {subj} dispatch {ph!r}",
                  r.status == ResultStatus.OK
                  and bool((r.data or {}).get("text")))
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
