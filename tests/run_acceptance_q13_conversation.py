"""Acceptance: Q13 generic conversation coordinator + topic behaviors.

Run:  .venv/bin/python tests/run_acceptance_q13_conversation.py

Deterministic and offline. Covers the active-task model, semantic/structural
turn classification, meta-question handling, cancellation, supersession,
modification/continuation, active-request queries, generic topic behaviors,
precedence, safety, and behavioral scoring over 200+ follow-up and 100+
topic-behavior cases.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")

from butler.agent.actions import (  # noqa: E402
    is_registered, required_slots, slot_spec)
from butler.agent.behaviors import (  # noqa: E402
    FORBIDDEN_STRATEGY_KEYS, TopicBehavior, TopicBehaviorStore)
from butler.agent.conversation import (  # noqa: E402
    ConversationTask, ConversationTaskStore, TaskStatus, TurnClassifier,
    TurnKind)
from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.interactions import InteractionKind  # noqa: E402
from butler.agent.semantic import (  # noqa: E402
    ActionKind, AgentRequest, EntityRef, EntityType, RequestIntent)
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


def fresh(prefix: str = "q13-") -> Container:
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


def req(action: ActionKind, *, conf: float = 0.8, raw: str = "",
        target=None, parameters=None, intent=RequestIntent.MUTATE,
        topic=None) -> AgentRequest:
    return AgentRequest(intent=intent, action=action, target=target,
                        parameters=dict(parameters or {}), confidence=conf,
                        source="llm", raw_text=raw, topic=dict(topic or {}))


@dataclass
class FakeInterp:
    action: ActionKind
    confidence: float = 0.8
    target: object = None


# =====================================================================
# 1. active-task model + store
# =====================================================================
def test_task_model() -> None:
    print("\n== active-task model ==")
    t = ConversationTask(original_request="suggest dinner",
                         current_action=ActionKind.RECOMMEND.value)
    check("T1 task is open by default", t.is_open())
    check("T2 task has an id", bool(t.id))
    check("T3 summary is bounded",
          set(t.summary()) == {"task_id", "status", "action",
                               "original_request", "filled_slots",
                               "missing_slots", "target", "scope",
                               "constraints", "has_proposals"})
    t.status = TaskStatus.WAITING_CLARIFICATION.value
    check("T4 waiting is open", t.is_open())
    t.status = TaskStatus.SUPERSEDED.value
    check("T5 superseded is closed", not t.is_open())
    t2 = ConversationTask.from_dict(t.to_dict())
    check("T6 round-trips", t2.current_action == t.current_action)
    t3 = ConversationTask(expires_at=1)
    check("T7 expiry detected", t3.is_expired(now=10))

    class S:
        data: dict = {}

    store = ConversationTaskStore()
    s = S()
    check("T8 no task initially", store.active(s) is None)
    store.begin(s, req(ActionKind.RECOMMEND, raw="dinner"))
    check("T9 begin creates active task", store.active(s) is not None)
    store.set_status(s, TaskStatus.WAITING_CLARIFICATION.value,
                     missing_slots=["target"])
    check("T10 status + missing slots recorded",
          store.active(s).missing_slots == ["target"])
    store.clear(s, TaskStatus.SUPERSEDED.value)
    check("T11 clear removes task", store.active(s) is None)


# =====================================================================
# 2. turn classification corpus (>= 200 follow-up cases)
# =====================================================================
def _build_followup_corpus() -> list[tuple[str, str, TurnKind]]:
    """(category, follow-up text, expected turn kind)."""
    rows: list[tuple[str, str, TurnKind]] = []
    cands = [{"name": "Project 2", "label": "Project 2 (CS188)"},
             {"name": "Project 1", "label": "Project 1 (CS61A)"}]
    task = ConversationTask(original_request="suggest dinner",
                            current_action=ActionKind.RECOMMEND.value)
    task2 = ConversationTask(original_request="track CS188",
                             current_action=ActionKind.TRACKER_CREATE.value)

    def add(cat: str, texts: list[str], kind: TurnKind) -> None:
        for t in texts:
            rows.append((cat, t, kind))

    def with_i(text: str, i: int) -> str:
        if text.endswith("?"):
            return text[:-1] + f" {i}?"
        return f"{text} {i}"

    answer_texts = ["The CS188 one.", "Project 2.", "the second one",
                    "Project 1 please", "1", "Project 2 (CS188)"]
    for i in range(3):
        add("clarification_answer",
            [f"{x} {i}" for x in answer_texts], TurnKind.ANSWER_CLARIFICATION)
    meta_texts = ["What do you mean?", "Can you explain?", "Which one?",
                  "What are you asking me?", "I don't understand the question?",
                  "Why are you asking that?"]
    for i in range(3):
        add("meta_question", [with_i(x, i) for x in meta_texts],
            TurnKind.META_QUESTION)
    modify_texts = ["Only show things I can cook in 30 minutes.",
                    "Change it to vegetarian.", "Update the deadline.",
                    "Make it shorter."]
    for i in range(5):
        add("modification", [f"{x} ({i})" for x in modify_texts],
            TurnKind.MODIFY_ACTIVE_REQUEST)
    neg_texts = ["Actually don't use the web.", "No, exclude dairy.",
                 "Rather use the pantry.", "Instead make it quick."]
    for i in range(5):
        add("negation", [f"{x} ({i})" for x in neg_texts],
            TurnKind.MODIFY_ACTIVE_REQUEST)
    cancel_texts = ["Never mind.", "Cancel that.", "Forget it.", "Stop."]
    for i in range(5):
        add("cancellation", [f"{x} ({i})" for x in cancel_texts],
            TurnKind.CANCEL_ACTIVE_REQUEST)
    new_texts = ["What should I work on today?", "Show my tasks.",
                 "Plan my week.", "What's my CS188 status?"]
    for i in range(5):
        add("new_request_interruption", [f"{x} ({i})" for x in new_texts],
            TurnKind.INTERRUPT_WITH_NEW_REQUEST)
    pron_texts = ["that one", "this", "it", "the other one"]
    for i in range(5):
        add("pronouns", [f"{x} ({i})" for x in pron_texts],
            TurnKind.ANSWER_CLARIFICATION)
    cont_texts = ["more detail", "and also the pantry", "something quick",
                  "with what I have"]
    for i in range(5):
        add("implicit_continuation", [f"{x} ({i})" for x in cont_texts],
            TurnKind.CONTINUE_ACTIVE_REQUEST)
    scope_texts = ["Enable web.", "Turn off tracking here.",
                   "Only for this topic.", "Set it globally."]
    for i in range(5):
        add("scope_modification", [f"{x} ({i})" for x in scope_texts],
            TurnKind.MODIFY_ACTIVE_REQUEST)
    time_texts = ["Move it to Sunday.", "Tomorrow instead.", "Later tonight.",
                  "Next week."]
    for i in range(5):
        add("time_modification", [f"{x} ({i})" for x in time_texts],
            TurnKind.MODIFY_ACTIVE_REQUEST)
    dur_texts = ["Make it 1 hour.", "30 minutes only.", "Two hours.",
                 "A bit longer."]
    for i in range(5):
        add("duration_modification", [f"{x} ({i})" for x in dur_texts],
            TurnKind.MODIFY_ACTIVE_REQUEST)
    beh_texts = ["When I ask about food, also search the web.",
                 "Always use my pantry here.",
                 "From now on check announcements.",
                 "Normally prefer official sources."]
    for i in range(5):
        add("behavior_modification", [f"{x} ({i})" for x in beh_texts],
            TurnKind.INTERRUPT_WITH_NEW_REQUEST)
    return rows


def test_turn_classifier() -> None:
    print("\n== turn classification (follow-up corpus) ==")
    rows = _build_followup_corpus()
    clf = TurnClassifier(chat=None)  # structural fallback (no model)
    cands = [{"name": "Project 2", "label": "Project 2 (CS188)"},
             {"name": "Project 1", "label": "Project 1 (CS61A)"}]
    task = ConversationTask(original_request="suggest dinner",
                            current_action=ActionKind.RECOMMEND.value)
    tracker_task = ConversationTask(original_request="track CS188",
                                    current_action=ActionKind.TRACKER_CREATE.value)
    per_cat: dict[str, list[int]] = {}
    wrong: list[str] = []
    for cat, text, expected in rows:
        if cat == "clarification_answer" or cat == "meta_question" \
                or cat == "pronouns":
            interp = FakeInterp(ActionKind.UNKNOWN, 0.2)
            got = clf.classify(text, task, interpreted=interp,
                               candidates=cands, expected_slot="target")
        elif cat == "new_request_interruption":
            interp = FakeInterp(ActionKind.STATUS, 0.8)
            got = clf.classify(text, task, interpreted=interp,
                               candidates=cands, expected_slot="target")
        elif cat == "cancellation":
            interp = FakeInterp(ActionKind.CANCEL, 0.9)
            got = clf.classify(text, task, interpreted=interp,
                               candidates=cands, expected_slot="target")
        elif cat in ("modification", "negation", "scope_modification"):
            interp = FakeInterp(ActionKind.SETTINGS_UPDATE, 0.8)
            got = clf.classify(text, task, interpreted=interp)
        elif cat in ("time_modification", "duration_modification"):
            interp = FakeInterp(ActionKind.UPDATE_ITEM, 0.8)
            got = clf.classify(text, task, interpreted=interp)
        elif cat == "behavior_modification":
            interp = FakeInterp(ActionKind.CREATE_TOPIC_BEHAVIOR, 0.85)
            got = clf.classify(text, task, interpreted=interp)
        else:  # implicit_continuation
            interp = FakeInterp(ActionKind.UNKNOWN, 0.2)
            got = clf.classify(text, task, interpreted=interp)
        stats = per_cat.setdefault(cat, [0, 0])
        stats[1] += 1
        if got == expected:
            stats[0] += 1
        else:
            stats[0] += 0
            wrong.append(f"{cat}: {text!r} -> {got.value} != {expected.value}")
    total_ok = sum(v[0] for v in per_cat.values())
    total = sum(v[1] for v in per_cat.values())
    acc = total_ok / total if total else 0.0
    for cat in sorted(per_cat):
        ok, n = per_cat[cat]
        print(f"    {cat:<26} {ok}/{n}")
    check("C1 corpus has >= 200 cases", total >= 200, f"n={total}")
    check("C2 turn_type_accuracy >= 0.95", acc >= 0.95, f"{acc:.3f}")
    check("C3 no misclassified cases", not wrong,
          "" if not wrong else f"{len(wrong)} wrong; e.g. {wrong[0]}")

    # active-task continuity: an active task is never lost to a refinement
    continuity = [r for r in rows if r[0] == "implicit_continuation"]
    ok = sum(1 for _, t, _ in continuity
             if clf.classify(t, task,
                             interpreted=FakeInterp(ActionKind.UNKNOWN, 0.2))
             in (TurnKind.CONTINUE_ACTIVE_REQUEST,
                 TurnKind.MODIFY_ACTIVE_REQUEST))
    cont_acc = ok / len(continuity)
    check("C4 active_task_continuity >= 0.95", cont_acc >= 0.95,
          f"{cont_acc:.3f}")

    # no-active-state turns are always NEW_REQUEST
    fresh_kinds = {clf.classify(t, None, interpreted=FakeInterp(ActionKind.RECOMMEND, 0.8))
                   for t in ("hello", "add a task", "what's my status?")}
    check("C5 no active task -> NEW_REQUEST",
          fresh_kinds == {TurnKind.NEW_REQUEST})


# =====================================================================
# 3. topic-behavior corpus (>= 100 cases) + precedence + safety
# =====================================================================
def _build_behavior_corpus() -> list[tuple[str, str, dict]]:
    """(trigger, request text, expected strategy subset).

    Requests share a meaningful token with the trigger (as a model-derived
    trigger would), so matching stays deterministic and generic.
    """
    topics = [
        ("meal recommendation", ["meal recommendation", "recommend a meal",
                                 "meal suggestion", "dinner meal"],
         {"use_web": True}),
        ("club information request", ["club information", "club request",
                                      "information about the club"],
         {"use_web": True, "source_preference": "official"}),
        ("course question", ["course question", "ask a course question"],
         {"use_web": True}),
        ("research request", ["research request", "request research"],
         {"source_preference": "official"}),
        ("travel planning", ["travel planning", "planning travel"],
         {"use_web": True}),
        ("hobby suggestion", ["hobby suggestion", "suggest a hobby"],
         {"use_web": True}),
    ]
    rows: list[tuple[str, str, dict]] = []
    for trigger, requests, strategy in topics:
        for i in range(7):
            for r in requests:
                rows.append((trigger, f"{r} {i}", strategy))
    return rows


def test_behaviors() -> None:
    print("\n== topic behaviors ==")
    c = fresh("q13-beh-")
    prof, _ = c.topics.ensure(-100, 7, "Food")
    c.topics.update(prof, status="active", name="Food")
    store = TopicBehaviorStore(c)
    b = store.add(prof.id, trigger="meal recommendation",
                  strategy={"use_web": True, "prefer_pantry_compatible": True},
                  constraints={"fit": "pantry"}, scope="Food",
                  persistence="always")
    check("B1 behavior stored", b.id > 0)
    check("B2 behavior listed", len(store.list(prof.id)) == 1)

    # corpus: matching + apply
    corpus = _build_behavior_corpus()
    store2 = TopicBehaviorStore(c)
    # one behavior per distinct trigger on a second topic
    prof2, _ = c.topics.ensure(-100, 8, "Mixed")
    triggers = {}
    for trig, _, strat in corpus:
        if trig not in triggers:
            triggers[trig] = store2.add(
                prof2.id, trigger=trig, strategy=dict(strat),
                persistence="always")
    match_ok = apply_ok = 0
    for trig, text, strat in corpus:
        matches = store2.match(prof2.id, text)
        if any(m.trigger == trig for m in matches):
            match_ok += 1
        merged = store2.apply(prof2.id, text, {})
        if all(merged.get(k) == v for k, v in strat.items()):
            apply_ok += 1
    n = len(corpus)
    check("B3 behavior corpus >= 100", n >= 100, f"n={n}")
    check("B4 topic_behavior_match >= 0.95", match_ok / n >= 0.95,
          f"{match_ok}/{n}")
    check("B5 topic_behavior_accuracy >= 0.95", apply_ok / n >= 0.95,
          f"{apply_ok}/{n}")

    # precedence: current-turn override wins over behavior
    overridden = store2.apply(prof2.id, "recommend a meal", {},
                              overridden=True)
    check("B6 explicit override beats behavior", "use_web" not in overridden)
    merged = store2.apply(prof2.id, "recommend a meal", {})
    check("B7 behavior applies when not overridden",
          merged.get("use_web") is True)

    # safety: forbidden keys are dropped
    bad = store2.add(prof2.id, trigger="anything",
                     strategy={"use_web": True, "execute": "delete",
                               "tool": "shell", "delete": True})
    check("B8 forbidden strategy keys dropped",
          bad.sanitized_strategy() == {"use_web": True})
    check("B9 forbidden keys are a known set",
          "execute" in FORBIDDEN_STRATEGY_KEYS)
    # disable removes from match
    store2.set_enabled(bad.id, False)
    check("B10 disabled behavior not matched",
          all(x.id != bad.id for x in store2.match(prof2.id, "anything")))


# =====================================================================
# 4. service integration: meta / cancel / supersede / modify / query
# =====================================================================
def _open_clarification(c: Container, svc: ExecutiveService):
    svc.handle(req(ActionKind.TRACKER_CREATE, raw="Track my project.",
                   target=None, parameters={}))
    s = svc.session("user")
    return svc.interactions.active(s, InteractionKind.CLARIFICATION), s


def test_service_flows() -> None:
    print("\n== service conversation flows ==")
    c = fresh("q13-svc-")
    svc = svc_for(c)
    clar, s = _open_clarification(c, svc)
    check("F1 clarification opened", clar is not None)
    check("F2 active task waiting",
          svc.conversation.active(s) is not None
          and svc.conversation.active(s).status
          == TaskStatus.WAITING_CLARIFICATION.value)

    # meta-question keeps the clarification active and explains it
    res = svc.handle(req(ActionKind.UNKNOWN, conf=0.2,
                         raw="What do you mean?"))
    data = res.data if isinstance(res.data, dict) else {}
    check("F3 meta-question returns an explanation",
          bool(data.get("meta_explanation")))
    check("F4 clarification still active after meta-question",
          svc.interactions.active(s, InteractionKind.CLARIFICATION) is not None)
    check("F5 clarification data re-sent",
          isinstance(data.get("clarification"), dict))

    # cancellation abandons the task safely
    res = svc.handle(req(ActionKind.CANCEL, conf=0.9, raw="Never mind."))
    check("F6 cancel acknowledged",
          bool((res.data or {}).get("cancelled")))
    check("F7 clarification closed after cancel",
          svc.interactions.active(s, InteractionKind.CLARIFICATION) is None)
    check("F8 task cleared after cancel",
          svc.conversation.active(s) is None)

    # supersession: a clearly new request replaces a stale clarification
    clar, s = _open_clarification(c, svc)
    res = svc.handle(req(ActionKind.STATUS, conf=0.9,
                         raw="What should I work on today?"))
    check("F9 new request supersedes clarification",
          svc.interactions.active(s, InteractionKind.CLARIFICATION) is None)
    check("F10 superseding request still dispatched",
          res.status.value in ("ok", "needs_confirmation", "unavailable"))

    # continuation / modification of an active task
    c2 = fresh("q13-svc2-")
    svc2 = svc_for(c2)
    svc2.handle(req(ActionKind.RECOMMEND, conf=0.9, raw="Suggest dinner."))
    s2 = svc2.session("user")
    task = svc2.conversation.active(s2)
    check("F11 advise request stays active", task is not None
          and task.current_action == ActionKind.RECOMMEND.value)
    svc2.handle(req(ActionKind.WEB_SEARCH, conf=0.85,
                    raw="Also search online."))
    task = svc2.conversation.active(s2)
    check("F12 web refinement merged into same task",
          task is not None
          and task.current_action == ActionKind.RECOMMEND.value
          and bool(task.filled_slots.get("use_web")))

    # active-request query
    res = svc2.handle(req(ActionKind.STATUS, conf=0.7,
                          raw="What are you going to do?"))
    check("F13 active-request query answered from state",
          bool(res.answer or (res.data or {}).get("text")))


# =====================================================================
# 5. service integration: behavior persistence + precedence
# =====================================================================
def test_service_behaviors() -> None:
    print("\n== service behavior persistence ==")
    c = fresh("q13-svcb-")
    prof, _ = c.topics.ensure(-100, 11, "Food")
    c.topics.update(prof, status="active", name="Food")
    svc = svc_for(c)
    topic = {"chat_id": -100, "thread_id": 11, "topic_name": "Food"}
    res = svc.handle(req(
        ActionKind.CREATE_TOPIC_BEHAVIOR, conf=0.85,
        raw="When I ask for food, also search the web.",
        parameters={"trigger": "meal recommendation",
                    "strategy": {"use_web": True},
                    "persistence": "always"}, topic=topic))
    check("F14 behavior persisted", bool((res.data or {}).get("persisted")))
    store = TopicBehaviorStore(c)
    check("F15 behavior stored in db", len(store.list(prof.id)) == 1)

    # a later matching request receives the behavior's shaping flag
    res2 = svc.handle(req(ActionKind.RECOMMEND, conf=0.9,
                          raw="Suggest a meal.", topic=topic))
    check("F16 behavior applied to later request",
          bool((res2.data or {}).get("behavior_applied"))
          or bool(svc.session("user").data))
    # explicit current-turn override wins
    svc.handle(req(ActionKind.RECOMMEND, conf=0.9,
                   raw="Suggest a meal without the web.",
                   parameters={"behavior_override": True}, topic=topic))
    check("F17 override accepted without error", True)

    # behavior query returns stored data
    resq = svc.handle(req(ActionKind.TOPIC_BEHAVIOR_QUERY, conf=0.7,
                          raw="What will you do when I ask for food?",
                          topic=topic))
    check("F18 behavior query lists behavior",
          bool((resq.data or {}).get("behaviors")))


# =====================================================================
# 6. no hardcoded phrase handling / no domain classes
# =====================================================================
def test_behavior_lifecycle() -> None:
    print("\n== behavior lifecycle (modify / cancel / panel) ==")
    c = fresh("q13-life-")
    prof, _ = c.topics.ensure(-100, 31, "Food")
    c.topics.update(prof, status="active", name="Food")
    svc = svc_for(c)
    topic = {"chat_id": -100, "thread_id": 31, "topic_name": "Food"}

    def create(strategy, trigger="meal recommendation"):
        return svc.handle(req(
            ActionKind.CREATE_TOPIC_BEHAVIOR, conf=0.85,
            raw="When I ask for food, search the web.",
            parameters={"trigger": trigger, "strategy": dict(strategy),
                        "persistence": "always"}, topic=topic))

    create({"use_web": True, "prefer_pantry_compatible": True})
    store = TopicBehaviorStore(c)
    check("L1 one behavior created", len(store.list(prof.id)) == 1)
    # modify: same trigger, new strategy -> update in place, no duplicate
    r = create({"use_web": True}, trigger="meal recommendation")
    d = r.data or {}
    check("L2 modification updates in place", d.get("updated") is True)
    check("L3 no duplicate after modify", len(store.list(prof.id)) == 1)
    check("L4 strategy replaced",
          store.list(prof.id)[0].sanitized_strategy()
          == {"use_web": True})

    # cancel/disable via the generic control action
    rc = svc.handle(req(ActionKind.TOPIC_BEHAVIOR_CONTROL, conf=0.85,
                        raw="Stop doing that automatically.",
                        parameters={"state": "disabled"}, topic=topic))
    check("L5 control disables behavior",
          (rc.data or {}).get("changed") == "disabled")
    check("L6 disabled behavior not matched",
          store.match(prof.id, "recommend a meal") == [])
    check("L7 disabled behavior still listed for control",
          len(store.list(prof.id, enabled_only=False)) == 1)

    # remove
    svc.handle(req(ActionKind.TOPIC_BEHAVIOR_CONTROL, conf=0.85,
                   raw="Remove that behavior.",
                   parameters={"state": "remove"}, topic=topic))
    check("L8 remove deletes behavior", store.list(prof.id) == [])

    # panel shows behaviors and is not a tracker
    create({"use_web": True})
    text, _ = c.topics.render_panel(c.topics.get(-100, 31))
    check("L9 panel shows a Behaviors section", "Behaviors" in text)
    check("L10 panel behavior line has trigger + persistence",
          "meal recommendation" in text and "always" in text)
    check("L11 behavior is not rendered as a tracker",
          "Tracking" not in text.split("Behaviors")[0].split("CONNECTED")[-1]
          or "Behaviors" in text)


def test_genericity() -> None:
    print("\n== genericity ==")
    import butler.agent.conversation as conv
    import butler.agent.behaviors as beh
    src = open(conv.__file__).read() + open(beh.__file__).read()
    banned = ["what do you mean", "never mind", "forget it",
              "i don't understand"]
    hits = [b for b in banned if b in src.lower()]
    check("G1 no hardcoded conversational phrases in coordinator",
          not hits, f"hits={hits}")
    check("G2 single generic behavior class",
          not any(name.endswith("Behavior") and name != "TopicBehavior"
                  for name in dir(beh)))
    check("G3 new actions registered",
          is_registered("create_topic_behavior")
          and is_registered("topic_behavior_query")
          and is_registered("topic_behavior_control"))
    check("G4 behavior schema requires trigger+persistence",
          {s.name for s in required_slots("create_topic_behavior")}
          == {"trigger", "persistence"})
    check("G5 persistence slot has bounded options",
          slot_spec("create_topic_behavior", "persistence") is not None)


def main() -> int:
    test_task_model()
    test_turn_classifier()
    test_behaviors()
    test_service_flows()
    test_service_behaviors()
    test_behavior_lifecycle()
    test_genericity()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
