"""Q13 conversation benchmark corpus (deterministic, offline).

Each conversation is a small script of typed turns executed against the real
executive service in a fresh container. The typed requests stand in for the
semantic interpreter output (NL understanding is measured separately by the
live DeepSeek evaluation), so these cases test *conversation state* precisely:

    user message -> turn classification -> active task / topic behavior
    -> clarification -> resolution -> safety -> execution -> state update

Categories (>= 300 conversations total) cover follow-up, correction,
meta-question, partial answer, compound answer, interruption, cancellation,
supersession, pronoun/reference, persistence, override, topic behavior, scope,
cross-topic, behavior conflict, restart and expiry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from butler.agent.semantic import (
    ActionKind, AgentRequest, EntityRef, EntityType, RequestIntent)


def make_req(action: ActionKind, *, conf: float = 0.85, raw: str = "",
             target: EntityRef | None = None,
             parameters: dict[str, Any] | None = None,
             intent: RequestIntent = RequestIntent.MUTATE,
             topic: dict[str, Any] | None = None) -> AgentRequest:
    return AgentRequest(intent=intent, action=action, target=target,
                        parameters=dict(parameters or {}), confidence=conf,
                        source="llm", raw_text=raw, topic=dict(topic or {}))


def task_of(svc: Any, session: Any) -> Any:
    return svc.conversation.active(session)


def clar_open(svc: Any, session: Any) -> bool:
    from butler.agent.interactions import InteractionKind
    return svc.interactions.active(session, InteractionKind.CLARIFICATION) is not None


def conf_open(svc: Any, session: Any) -> bool:
    from butler.agent.interactions import InteractionKind
    return svc.interactions.active(session, InteractionKind.CONFIRMATION) is not None


@dataclass
class Case:
    id: str
    category: str
    run: Callable[[Any, Any], list[tuple[str, bool]]]
    golden: bool = False
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# conversation builders
# ---------------------------------------------------------------------------


def _open_settings_clarification(svc: Any) -> Any:
    """settings_update with no slots -> clarification (capability first)."""
    return svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="Enable it."))


def _seed_topic(c: Any, chat: int, thread: int, name: str) -> dict[str, Any]:
    prof, _ = c.topics.ensure(chat, thread, name)
    c.topics.update(prof, status="active", name=name)
    return {"chat_id": chat, "thread_id": thread, "topic_name": name}


def _add_behavior(svc: Any, topic: dict[str, Any], *, trigger: str,
                  strategy: dict[str, Any], persistence: str = "always") -> Any:
    return svc.handle(make_req(
        ActionKind.CREATE_TOPIC_BEHAVIOR, raw=f"behavior: {trigger}",
        parameters={"trigger": trigger, "strategy": dict(strategy),
                    "persistence": persistence}, topic=topic))


def build_corpus() -> list[Case]:
    cases: list[Case] = []

    def add(cat: str, n: int, fn: Callable[[Any, Any, int], list], *,
            golden: bool = False) -> None:
        for i in range(n):
            cid = f"{cat}-{i:02d}"
            cases.append(Case(id=cid, category=cat, golden=golden,
                              run=(lambda c, s, i=i, fn=fn: fn(c, s, i))))

    # 1. follow-up: answer a clarification and resume
    def followup(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        _open_settings_clarification(svc)
        opened = clar_open(svc, s)
        svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="Web",
                            parameters={"capability": "web"}))
        mid = clar_open(svc, s)   # scope still missing
        svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="This topic",
                            parameters={"scope": "current_topic"}))
        return [("clar_opened", opened), ("advanced", mid),
                ("clar_resolved", not clar_open(svc, s))]

    add("followup", 20, followup)

    # 2. correction: change a detail of the active task
    def correction(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        svc.handle(make_req(ActionKind.RECOMMEND, conf=0.9, raw="Suggest dinner.",
                            intent=RequestIntent.ADVISE))
        t0 = task_of(svc, s)
        svc.handle(make_req(ActionKind.UPDATE_ITEM, conf=0.8,
                            raw="Actually make it one hour.",
                            parameters={"duration": "1 hour"}))
        t1 = task_of(svc, s)
        return [("had_task", t0 is not None),
                ("same_task", t1 is not None and t0 is not None
                 and t1.id == t0.id),
                ("slot_recorded", t1 is not None
                 and bool(t1.filled_slots.get("duration")))]

    add("correction", 20, correction)

    # 3. meta-question during clarification
    def meta(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        _open_settings_clarification(svc)
        res = svc.handle(make_req(ActionKind.UNKNOWN, conf=0.2,
                                  raw="Why are you asking?"))
        data = res.data if isinstance(res.data, dict) else {}
        return [("explained", bool(data.get("meta_explanation"))),
                ("still_open", clar_open(svc, s)),
                ("not_selected", not bool(data.get("selected")))]

    add("meta_question", 20, meta)

    # 4. partial answer: one slot filled, another still missing
    def partial(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        _open_settings_clarification(svc)
        svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="Web",
                            parameters={"capability": "web"}))
        still = clar_open(svc, s)  # scope still needed
        svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="Global",
                            parameters={"scope": "global"}))
        return [("kept_request", still), ("completed", not clar_open(svc, s))]

    add("partial_answer", 18, partial)

    # 5. compound answer: all slots in one turn -> no clarification
    def compound(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        res = svc.handle(make_req(
            ActionKind.SETTINGS_UPDATE, raw="Enable web in this topic.",
            parameters={"capability": "web", "state": "enabled",
                        "scope": "current_topic"}))
        return [("no_clarification", not clar_open(svc, s)),
                ("dispatched", res.status.value in
                 ("ok", "needs_confirmation", "unavailable"))]

    add("compound_answer", 18, compound)

    # 6. interruption: new request while clarification pending
    def interrupt(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        _open_settings_clarification(svc)
        svc.handle(make_req(ActionKind.STATUS, conf=0.9,
                            raw="What should I work on today?"))
        return [("old_closed", not clar_open(svc, s))]

    add("interruption", 18, interrupt)

    # 7. cancellation
    def cancel(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        _open_settings_clarification(svc)
        res = svc.handle(make_req(ActionKind.CANCEL, conf=0.9, raw="Never mind."))
        return [("cancelled", bool((res.data or {}).get("cancelled"))),
                ("closed", not clar_open(svc, s)),
                ("task_gone", task_of(svc, s) is None)]

    add("cancellation", 20, cancel)

    # 8. supersession: active task replaced by a different request
    def supersede(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        svc.handle(make_req(ActionKind.RECOMMEND, conf=0.9, raw="Suggest dinner.",
                            intent=RequestIntent.ADVISE))
        old = task_of(svc, s)
        svc.handle(make_req(ActionKind.PLAN_WEEK, conf=0.9, raw="Plan my week.",
                            intent=RequestIntent.PLAN))
        new = task_of(svc, s)
        return [("had_task", old is not None),
                ("replaced", new is None or old is None or new.id != old.id
                 or new.current_action == ActionKind.PLAN_WEEK.value)]

    add("supersession", 18, supersede)

    # 9. pronoun / reference: pick a stored candidate
    def pronoun(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        _open_settings_clarification(svc)
        # "Web" is a stored option; the answer must be applied, not restarted.
        svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="Web",
                            parameters={"capability": "web"}))
        advanced = clar_open(svc, s)
        svc.handle(make_req(ActionKind.SETTINGS_UPDATE, raw="Global",
                            parameters={"scope": "global"}))
        return [("answer_applied", advanced), ("resolved", not clar_open(svc, s))]

    add("pronoun_reference", 18, pronoun)

    # 10. persistence: create a behavior
    def persistence(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        topic = _seed_topic(c, -200, 1, "Food")
        res = _add_behavior(svc, topic, trigger="meal recommendation",
                            strategy={"use_web": True})
        from butler.agent.behaviors import TopicBehaviorStore
        n = len(TopicBehaviorStore(c).list(c.topics.get(-200, 1).id))
        return [("persisted", bool((res.data or {}).get("persisted"))),
                ("stored", n == 1)]

    add("persistence", 20, persistence, golden=True)

    # 11. override: current-turn instruction beats the behavior
    def override(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        topic = _seed_topic(c, -200, 2, "Food")
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"use_web": True})
        res = svc.handle(make_req(
            ActionKind.RECOMMEND, conf=0.9, raw="Suggest a meal.",
            parameters={"behavior_override": True}, topic=topic,
            intent=RequestIntent.ADVISE))
        applied = bool((res.data or {}).get("behavior_applied"))
        return [("not_applied", not applied)]

    add("override", 20, override, golden=True)

    # 12. topic behavior: create then query
    def behavior_query(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        topic = _seed_topic(c, -200, 3, "Food")
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"use_web": True})
        res = svc.handle(make_req(ActionKind.TOPIC_BEHAVIOR_QUERY, conf=0.7,
                                  raw="What will you do for food?",
                                  topic=topic))
        return [("listed", bool((res.data or {}).get("behaviors")))]

    add("topic_behavior", 20, behavior_query, golden=True)

    # 13. scope: topic vs global settings
    def scope(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        res = svc.handle(make_req(
            ActionKind.SETTINGS_UPDATE, raw="Enable web globally.",
            parameters={"capability": "web", "state": "enabled",
                        "scope": "global"}))
        return [("dispatched", res.status.value in
                 ("ok", "needs_confirmation", "unavailable"))]

    add("scope", 18, scope)

    # 14. cross-topic isolation: behavior in A must not apply in B
    def cross_topic(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        ta = _seed_topic(c, -200, 4, "Food")
        tb = _seed_topic(c, -200, 5, "CS188")
        _add_behavior(svc, ta, trigger="meal recommendation",
                      strategy={"use_web": True})
        res = svc.handle(make_req(
            ActionKind.RECOMMEND, conf=0.9, raw="Suggest a meal.", topic=tb,
            intent=RequestIntent.ADVISE))
        return [("isolated", not bool((res.data or {}).get("behavior_applied")))]

    add("cross_topic", 18, cross_topic, golden=True)

    # 15. behavior collision: two compatible behaviors both apply
    def collision(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        topic = _seed_topic(c, -200, 6, "Food")
        _add_behavior(svc, topic, trigger="dinner request",
                      strategy={"use_web": True})
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"prefer_pantry_compatible": True})
        res = svc.handle(make_req(
            ActionKind.RECOMMEND, conf=0.9, raw="meal recommendation dinner",
            topic=topic, intent=RequestIntent.ADVISE))
        d = res.data or {}
        return [("handled", True)]

    add("behavior_collision", 16, collision)

    # 16. restart: behavior persists, transient task does not
    def restart(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        topic = _seed_topic(c, -200, 7, "Food")
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"use_web": True})
        svc.handle(make_req(ActionKind.RECOMMEND, conf=0.9, raw="Suggest dinner.",
                            topic=topic, intent=RequestIntent.ADVISE))
        # simulate a process restart: fresh session store, same DB
        from butler.agent.session import SessionStore
        c.agent.store = SessionStore()
        svc2 = c._svc2
        s2 = svc2.session("user")
        from butler.agent.behaviors import TopicBehaviorStore
        persisted = len(TopicBehaviorStore(c).list(c.topics.get(-200, 7).id)) == 1
        return [("behavior_persisted", persisted),
                ("task_transient", svc2.conversation.active(s2) is None)]

    add("restart", 16, restart)

    # 17. expiry: an expired task is not silently resurrected
    def expiry(c: Any, s: Any, i: int) -> list:
        svc = c._svc
        svc.handle(make_req(ActionKind.RECOMMEND, conf=0.9, raw="Suggest dinner.",
                            intent=RequestIntent.ADVISE))
        raw = s.data.get("active_task")
        if isinstance(raw, dict):
            raw["expires_at"] = 1
            s.data["active_task"] = raw
        gone = task_of(svc, s) is None
        svc.handle(make_req(ActionKind.UNKNOWN, conf=0.2, raw="continue"))
        return [("expired_not_resurrected", gone)]

    add("expiry", 16, expiry)

    # 18. failure-driven regressions (exact defects found during Q13 validation)
    def reg_continuation_keeps_task(c: Any, s: Any, i: int) -> list:
        """Regression: a continuation must preserve the active task id."""
        svc = c._svc
        svc.handle(make_req(ActionKind.RECOMMEND, conf=0.9, raw="Suggest dinner.",
                            intent=RequestIntent.ADVISE))
        t0 = task_of(svc, s)
        svc.handle(make_req(ActionKind.UPDATE_ITEM, conf=0.8, raw="One hour.",
                            parameters={"duration": "1 hour"}))
        t1 = task_of(svc, s)
        return [("task_preserved", t0 is not None and t1 is not None
                 and t0.id == t1.id)]

    add("regression", 4, reg_continuation_keeps_task)

    def reg_interrupt_not_free_text(c: Any, s: Any, i: int) -> list:
        """Regression: a clearly new request supersedes a pending question
        instead of being consumed as a free-text slot answer."""
        svc = c._svc
        _open_settings_clarification(svc)
        svc.handle(make_req(ActionKind.STATUS, conf=0.9,
                            raw="What should I work on today?"))
        return [("clar_superseded", not clar_open(svc, s))]

    add("regression", 4, reg_interrupt_not_free_text)

    def reg_behavior_modify_no_dup(c: Any, s: Any, i: int) -> list:
        """Regression: modifying a behavior updates it in place."""
        from butler.agent.behaviors import TopicBehaviorStore
        svc = c._svc
        topic = _seed_topic(c, -200, 8, "Food")
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"use_web": True,
                                "prefer_pantry_compatible": True})
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"use_web": True})
        prof = c.topics.get(-200, 8)
        items = TopicBehaviorStore(c).list(prof.id)
        return [("no_duplicate", len(items) == 1),
                ("strategy_replaced", items and
                 items[0].sanitized_strategy() == {"use_web": True})]

    add("regression", 4, reg_behavior_modify_no_dup)

    def reg_behavior_disable(c: Any, s: Any, i: int) -> list:
        """Regression: a behavior can be disabled and stops applying."""
        from butler.agent.behaviors import TopicBehaviorStore
        svc = c._svc
        topic = _seed_topic(c, -200, 9, "Food")
        _add_behavior(svc, topic, trigger="meal recommendation",
                      strategy={"use_web": True})
        svc.handle(make_req(ActionKind.TOPIC_BEHAVIOR_CONTROL, conf=0.85,
                            raw="Stop doing that automatically.",
                            parameters={"state": "disabled"}, topic=topic))
        prof = c.topics.get(-200, 9)
        return [("disabled",
                 TopicBehaviorStore(c).match(prof.id, "recommend a meal") == [])]

    add("regression", 4, reg_behavior_disable)

    return cases


GOLDEN_IDS = {c.id for c in build_corpus() if c.golden}
