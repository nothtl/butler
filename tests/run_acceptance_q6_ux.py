"""Q6 acceptance: setup-state, dashboard, capability reasons, tracker summary,
web honesty. Deterministic/offline."""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

_spec = importlib.util.spec_from_file_location(
    "n4h", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "run_acceptance_n4_hardening.py"))
n4h = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(n4h)

from butler.agent.clarification import classify_slot_text  # noqa: E402
from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.semantic import ActionKind, ResultStatus  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.topics import CAPABILITIES, CAP_LABELS  # noqa: E402

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


def fresh(prefix="q6-"):
    return n4h.fresh(prefix)


def run(coro):
    return asyncio.run(coro)


class FakeChat:
    def __init__(self, kind):
        self.kind = kind
    def _llm_ready(self):
        return True
    def complete(self, system, user, json_mode=False):
        return '{"kind": "%s"}' % self.kind


def test_slot_compat():
    print("\n== purpose-slot compatibility ==")
    meta = ["what can you do?", "what can you do", "help", "/help",
            "what is this?", "what can this topic do?", "how does this work?",
            "what is this for?"]
    for t in meta:
        check(f"S meta {t!r}", classify_slot_text(None, t) == "meta")
    vague = ["stuff", "everything", "things", "anything", "whatever", "idk"]
    for t in vague:
        check(f"S vague {t!r}", classify_slot_text(None, t) == "vague")
    purpose = ["This is my CS188 course.", "Use this for meal planning.",
               "My basketball club practices and announcements.",
               "Track my Stock Bot GitHub releases."]
    for t in purpose:
        check(f"S purpose {t!r}", classify_slot_text(None, t) == "purpose")
    # model path is used when available
    for kind in ("purpose", "meta", "vague"):
        check(f"S model {kind}", classify_slot_text(FakeChat(kind), "x") == kind)
    check("S empty -> vague", classify_slot_text(None, "  ") == "vague")


def test_setup_state():
    print("\n== setup-state preservation ==")
    c = fresh("q6-setup-")
    c.cfg.telegram_open_when_empty = True
    b = n4h.tbot(c)
    fb = n4h.FakeBot(topic_name="Testing")
    ctx = n4h.FakeContext(fb)
    run(b.on_message(n4h.FakeUpdate("hi", 7), ctx))
    check("U1 new topic pending", c.topics.get(1, 7).status == "pending_setup")
    u = n4h.FakeUpdate("what can you do?", 7)
    run(b.on_message(u, ctx))
    check("U2 meta does not activate", c.topics.get(1, 7).status == "pending_setup")
    check("U3 no panel pinned", len(fb.pins) == 0)
    from butler.topics import DEFAULT_CAPS
    check("U4 capabilities unchanged",
          c.topics.get(1, 7).capabilities == DEFAULT_CAPS)
    check("U5 answer is contextual capabilities",
          any("I can help with" in s for s in u.message.sent))
    check("U6 setup question re-asked",
          any("what is this topic for" in s.lower() for s in u.message.sent))
    # vague stays pending
    u2 = n4h.FakeUpdate("stuff", 7)
    run(b.on_message(u2, ctx))
    check("U7 vague does not activate", c.topics.get(1, 7).status == "pending_setup")
    # genuine purpose -> proposal (still pending until confirm)
    u3 = n4h.FakeUpdate("This is my CS188 course. Track homework.", 7)
    run(b.on_message(u3, ctx))
    check("U8 purpose keeps pending until confirm",
          c.topics.get(1, 7).status == "pending_setup")
    pending = b._pending_topic_map().get(b._pending_topic_key(1, 7))
    check("U9 proposal produced", pending is not None and pending.get("purpose"))
    check("U10 no pin before confirm", len(fb.pins) == 0)
    run(b._apply_topic_setup(1, 7, fb))
    check("U11 confirm activates", c.topics.get(1, 7).status == "active")
    check("U12 one panel pinned", len(fb.pins) == 1)


def test_dashboard():
    print("\n== operating dashboard ==")
    c = fresh("q6-panel-")
    b = n4h.tbot(c)
    fb = n4h.FakeBot(topic_name="Food")
    prof = n4h.activate(c, 1, 7, "Food", "Food & meal planning")
    text, h = c.topics.render_panel(prof)
    check("P1 has purpose", "Purpose" in text)
    check("P2 has doing section", "WHAT I'M DOING" in text)
    check("P3 uses a topic icon", text[0] in "🍳🛒📚🗂🏀✈️🔬")
    check("P4 no raw json", "{" not in text and "target_type" not in text)
    check("P5 content hash", len(h) == 16)
    check("P6 shows not-active section", "NOT ACTIVE" in text)
    # tracking summary
    c.food.add("chicken", quantity=3)
    c.trackers.register_provider(n4h.SnapshotProvider(c, {}))
    t = c.trackers.create(name="Pantry stock", source="snapshot",
                          target_type="food_item", target_ref="chicken",
                          condition={"type": "threshold_below", "item": "chicken",
                                     "field": "quantity", "value": 2},
                          action={"type": "CREATE_SUGGESTION", "params": {}},
                          destination={"chat_id": 1, "thread_id": 7}, cadence=86400)
    text2, _ = c.topics.render_panel(c.topics.get(1, 7))
    check("P7 panel shows tracker", "Pantry stock" in text2)
    check("P8 panel shows cadence", "1d" in text2 or "24h" in text2)
    det = c.topics.tracker_details(c.topics.get(1, 7))
    for field in ("Source:", "Frequency:", "Events:", "Notify:", "Destination:"):
        check(f"P9 details has {field}", field in det)
    check("P10 details shows tracker name", "Pantry stock" in det)
    # capability reason
    for cap, needle in (("web", "online"), ("tracking", "monitor"),
                        ("proactive", "notify"), ("scheduling", "schedule")):
        r = b._capability_reason(cap, "enabled", prof, "enable it")
        check(f"P11 reason {cap}", needle in r.lower())
    r = b._capability_reason("web", "disabled", prof, "turn off")
    check("P12 disabled reason", "turn off" in r.lower())


def test_web_and_search():
    print("\n== web capability vs search ==")
    c = fresh("q6-web-")
    it = DeterministicInterpreter(c)
    check("W1 enable web is settings",
          it.interpret("enable web here").action == ActionKind.SETTINGS_UPDATE)
    check("W2 search is a web action",
          it.interpret("search the internet for CS168").action in
          (ActionKind.WEB_SEARCH, ActionKind.WEB_RESEARCH))
    # provider offline -> honest unavailable, never fake success
    svc = ExecutiveService(c, interpreter=it)
    res = svc.ask(text="search the internet for CS168")
    check("W3 offline search is not OK", res.status != ResultStatus.OK,
          res.status.value)
    check("W4 offline search warns",
          bool(res.warnings) or res.status == ResultStatus.UNAVAILABLE)
    check("W5 web provider is offline by default",
          getattr(c.cfg, "web_search_provider", "offline") == "offline")
    # search must not create a tracker
    check("W6 search does not create a tracker",
          c.db.one("SELECT COUNT(*) n FROM trackers")["n"] == 0)


def test_tracker_visibility():
    print("\n== tracker visibility ==")
    c = fresh("q6-trk-")
    n4h.activate(c, 1, 7, "CS188", "Course management")
    c.trackers.register_provider(n4h.SnapshotProvider(c, {}))
    c.trackers.create(name="CS188 Fall 26", source="course",
                      target_type="course", target_ref="CS188",
                      condition={"type": "new_item"},
                      action={"type": "CREATE_SUGGESTION", "params": {}},
                      destination={"chat_id": 1, "thread_id": 7}, cadence=86400)
    lines = c.topics.tracking_lines(c.topics.get(1, 7))
    check("T1 tracking_lines lists the tracker",
          any("CS188 Fall 26" in x for x in lines))
    det = c.topics.tracker_details(c.topics.get(1, 7))
    check("T2 details is a tracking view", det.startswith("🔎 Tracking"))
    check("T3 details lists the tracker", "CS188 Fall 26" in det)
    check("T4 details has source", "Source:" in det)
    check("T5 details has events", "Events:" in det)
    # no duplicate trackers
    check("T6 one tracker only",
          c.db.one("SELECT COUNT(*) n FROM trackers")["n"] == 1)


def test_generated():
    print("\n== generated coverage ==")
    # classify a larger phrase set through the deterministic fallback
    meta_q = [f"what {w}?" for w in ("can you do", "is this", "can this topic do",
                                     "should I do", "are you able to do")]
    for t in meta_q:
        check(f"G1 meta {t!r}", classify_slot_text(None, t) == "meta")
    purpose_phrases = [
        "This is for my CS188 course.", "Use this for meal planning.",
        "My basketball club practices.", "Track my Stock Bot repo.",
        "Research notes for UAV navigation.", "Travel planning for Japan.",
        "This topic manages my groceries.", "Personal ideas and notes.",
    ]
    for t in purpose_phrases:
        check(f"G2 purpose {t!r}", classify_slot_text(None, t) == "purpose")
    # every capability has a reason and a label
    c = fresh("q6-gen-")
    b = n4h.tbot(c)
    prof = n4h.activate(c, 1, 9, "Food", "meal planning")
    for cap in CAPABILITIES:
        r = b._capability_reason(cap, "enabled", prof, "x")
        check(f"G3 reason for {cap}", bool(r) and len(r) > 5)
        check(f"G4 label for {cap}", cap in CAP_LABELS)
    # enabling each capability reflects in the panel
    for cap in ("web", "tracking", "planning", "scheduling", "proactive"):
        c.topics.set_capability(c.topics.get(1, 9), cap, "enabled")
        text, _ = c.topics.render_panel(c.topics.get(1, 9))
        check(f"G5 panel reflects {cap}",
              CAP_LABELS[cap] in text or cap.capitalize() in text)
    # cadence labels
    for secs, want in ((0, "manual"), (60, "every 1m"), (3600, "every 1h"),
                       (86400, "every 1d"), (172800, "every 2d")):
        check(f"G6 cadence {secs}", c.topics._cadence_label(secs) == want)
    # disabled reason for every capability
    for cap in CAPABILITIES:
        r = b._capability_reason(cap, "disabled", prof, "x")
        check(f"G7 disabled reason {cap}", "turn off" in r.lower())
    # search phrasing variants map to a web action
    it2 = DeterministicInterpreter(c)
    for t in ("check the web for CS168", "research CS168",
              "search online for CS168"):
        check(f"G8 web action {t!r}",
              it2.interpret(t).action in (ActionKind.WEB_SEARCH,
                                          ActionKind.WEB_RESEARCH))
    # more meta phrases
    for w in ("how do you work", "what are you", "can you help me",
              "what's your purpose", "are you a bot"):
        check(f"G9 meta {w!r}", classify_slot_text(None, w + "?") == "meta")
    # more genuine purposes
    for t in ("My thesis experiments and papers.",
              "Keep track of my basketball club announcements.",
              "Plan meals from what is in my fridge.",
              "Organize research for my UAV project."):
        check(f"G10 purpose {t!r}", classify_slot_text(None, t) == "purpose")
    # multiple trackers with cadences in the details view
    for name, cad in (("T-A", 3600), ("T-B", 86400), ("T-C", 604800)):
        c.trackers.create(name=name, source="snapshot", target_type="food_item",
                          target_ref="chicken",
                          condition={"type": "any_change"},
                          action={"type": "CREATE_SUGGESTION", "params": {}},
                          destination={"chat_id": 1, "thread_id": 9},
                          cadence=cad)
    det = c.topics.tracker_details(c.topics.get(1, 9))
    for name in ("T-A", "T-B", "T-C"):
        check(f"G11 details {name}", name in det)
    check("G12 details cadence hourly", "every 1h" in det)
    check("G13 details cadence daily", "every 1d" in det)
    c.topics.add_link(c.topics.get(1, 9), "food", 1, "uses_pantry", 0.8, "user")
    t3, _ = c.topics.render_panel(c.topics.get(1, 9))
    check("G14 panel connected section", "CONNECTED" in t3)
    # broad phrase battery
    battery = [
        ("what can you do", "meta"), ("how do I use this", "meta"),
        ("what are your features", "meta"), ("can you schedule things", "meta"),
        ("who made you", "meta"), ("why do you ask", "meta"),
        ("stuff", "vague"), ("things", "vague"), ("whatever", "vague"),
        ("This is for my CS168 networks course.", "purpose"),
        ("Meal planning and pantry.", "purpose"),
        ("Club practices and matches.", "purpose"),
        ("Research notes for UAV navigation.", "purpose"),
        ("Track my GitHub releases.", "purpose"),
        ("Grocery shopping and restocking.", "purpose"),
    ]
    for text, want in battery:
        check(f"G15 {want} {text!r}", classify_slot_text(None, text) == want)
    for text, want in (("help me?", "meta"), ("what now", "meta"),
                       ("This is my travel plan.", "purpose"),
                       ("Ideas and notes for later.", "purpose")):
        check(f"G16 {want} {text!r}", classify_slot_text(None, text) == want)


def main():
    test_slot_compat()
    test_setup_state()
    test_dashboard()
    test_web_and_search()
    test_tracker_visibility()
    test_generated()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
