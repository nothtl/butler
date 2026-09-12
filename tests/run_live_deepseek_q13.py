"""LIVE (optional) Q13 turn-classification evaluation.

Run:  .venv/bin/python tests/run_live_deepseek_q13.py --allow-live

Makes ~24 REAL DeepSeek calls to measure the semantic turn classifier against
an active-task context. NOT part of the deterministic aggregate. Exits 0 with
BLOCKED when no key is configured, non-zero only on a real failure.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.agent.conversation import (  # noqa: E402
    ConversationTask, TurnClassifier, TurnKind)
from butler.agent.semantic import ActionKind  # noqa: E402
from butler.core import Container  # noqa: E402

#: (text, expected ActionKind) — behavior lifecycle + behavior-vs-tracker/memory
ACTION_CASES = [
    ("When I ask you for food, also search the Web for good menus that fit my pantry.", ActionKind.CREATE_TOPIC_BEHAVIOR),
    ("Always use my pantry when I ask for meals.", ActionKind.CREATE_TOPIC_BEHAVIOR),
    ("Actually, only use the web when you don't have enough ingredients.", ActionKind.CREATE_TOPIC_BEHAVIOR),
    ("For this topic, use the web for course updates.", ActionKind.CREATE_TOPIC_BEHAVIOR),
    ("Stop doing that automatically.", ActionKind.TOPIC_BEHAVIOR_CONTROL),
    ("Disable that behavior here.", ActionKind.TOPIC_BEHAVIOR_CONTROL),
    ("What will you do when I ask for food?", ActionKind.TOPIC_BEHAVIOR_QUERY),
    ("Why do you search the web for food?", ActionKind.TOPIC_BEHAVIOR_QUERY),
    ("What behaviors do you have here?", ActionKind.TOPIC_BEHAVIOR_QUERY),
    ("Tell me when chicken is low.", ActionKind.TRACKER_CREATE),
    ("Track CS188 assignments.", ActionKind.TRACKER_CREATE),
    ("Remember that I prefer morning study sessions.", ActionKind.MEMORY_LEARN),
    ("Never mind.", ActionKind.CANCEL),
    ("Cancel that.", ActionKind.CANCEL),
    ("What should I work on today?", ActionKind.RECOMMEND),
    ("Schedule two hours for CS188 tomorrow afternoon.", ActionKind.FIND_BEST_SLOT),
]

CANDS = [{"name": "Project 2", "label": "Project 2 (CS188)"},
         {"name": "Project 1", "label": "Project 1 (CS61A)"}]


def _task(action: str, text: str) -> ConversationTask:
    return ConversationTask(original_request=text, current_action=action)


#: (category, text, task, candidates, expected_slot, awaiting, expected)
CASES = [
    ("clarification_answer", "The CS188 one.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.ANSWER_CLARIFICATION),
    ("clarification_answer", "Project 2.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.ANSWER_CLARIFICATION),
    ("clarification_answer", "the second one", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.ANSWER_CLARIFICATION),
    ("clarification_answer", "I mean my current food inventory.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.ANSWER_CLARIFICATION),
    ("meta_question", "What do you mean?", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.META_QUESTION),
    ("meta_question", "Can you explain?", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.META_QUESTION),
    ("meta_question", "Which one?", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.META_QUESTION),
    ("meta_question", "I don't understand the question.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.META_QUESTION),
    ("modification", "Only show things I can cook in 30 minutes.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.MODIFY_ACTIVE_REQUEST),
    ("modification", "Actually don't use the web.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.MODIFY_ACTIVE_REQUEST),
    ("modification", "Change it to vegetarian.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.MODIFY_ACTIVE_REQUEST),
    ("cancellation", "Never mind.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.CANCEL_ACTIVE_REQUEST),
    ("cancellation", "Cancel that.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.CANCEL_ACTIVE_REQUEST),
    ("cancellation", "Forget it.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.CANCEL_ACTIVE_REQUEST),
    ("new_request_interruption", "What should I work on today?", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.INTERRUPT_WITH_NEW_REQUEST),
    ("new_request_interruption", "Show my tasks.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.INTERRUPT_WITH_NEW_REQUEST),
    ("continuation", "Use my pantry.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.CONTINUE_ACTIVE_REQUEST),
    ("continuation", "Prefer Mexican.", _task("recommend", "suggest dinner"), [], "", False, TurnKind.CONTINUE_ACTIVE_REQUEST),
    ("query_active", "What are you going to do?", _task("recommend", "suggest dinner"), [], "", False, TurnKind.QUERY_ACTIVE_REQUEST),
    ("meta_question", "Why did you ask me that?", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.META_QUESTION),
    ("behavior", "When I ask for food, also search the web.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.INTERRUPT_WITH_NEW_REQUEST),
    ("behavior", "Always use my pantry here.", _task("recommend", "suggest dinner"), CANDS, "target", False, TurnKind.INTERRUPT_WITH_NEW_REQUEST),
    ("confirmation", "Yes, go ahead.", _task("tracker_create", "track CS188"), [], "", True, TurnKind.ANSWER_CONFIRMATION),
    ("confirmation", "No, change the cadence to weekly.", _task("tracker_create", "track CS188"), [], "", True, TurnKind.MODIFY_ACTIVE_REQUEST),
]


def _equivalent(a: TurnKind, b: TurnKind) -> bool:
    """Behavioral equivalence: modify/continue both refine; new/interrupt both
    supersede and start fresh. Scoring cares about the outcome, not the label."""
    refine = {TurnKind.CONTINUE_ACTIVE_REQUEST, TurnKind.MODIFY_ACTIVE_REQUEST}
    supersede = {TurnKind.NEW_REQUEST, TurnKind.INTERRUPT_WITH_NEW_REQUEST}
    if a == b:
        return True
    if a in refine and b in refine:
        return True
    if a in supersede and b in supersede:
        return True
    return False


def main() -> int:
    if "--allow-live" not in sys.argv:
        print("BLOCKED: pass --allow-live to make real DeepSeek calls")
        return 0
    max_calls = 0
    for i, arg in enumerate(sys.argv):
        if arg == "--max-calls" and i + 1 < len(sys.argv):
            max_calls = int(sys.argv[i + 1])
    container = Container()
    cfg = container.cfg
    if not (getattr(cfg, "llm_api_key", "") and getattr(cfg, "llm_base_url", "")):
        print("BLOCKED: no LLM key/base_url configured")
        return 0
    chat = getattr(container, "chat", None)
    if chat is None:
        print("BLOCKED: no chat client")
        return 0
    clf = TurnClassifier(chat=chat)
    if not clf.available():
        print("BLOCKED: classifier model not ready")
        return 0
    print(f"endpoint: {cfg.llm_base_url}  model: {cfg.llm_model}")
    per_cat: dict[str, list[int]] = {}
    wrong: list[str] = []
    calls = 0
    for cat, text, task, cands, slot, awaiting, expected in CASES:
        t0 = time.perf_counter()
        got = clf.classify(text, task, interpreted=None, candidates=cands,
                           expected_slot=slot, awaiting_confirmation=awaiting)
        calls += 1
        ms = int((time.perf_counter() - t0) * 1000)
        stats = per_cat.setdefault(cat, [0, 0])
        stats[1] += 1
        ok = _equivalent(got, expected)
        if ok:
            stats[0] += 1
        else:
            wrong.append(f"{cat}: {text!r} -> {got.value} != {expected.value}")
        print(f"  {'PASS' if ok else 'FAIL'}  "
              f"{text[:48]!r} -> {got.value} ({ms}ms)")
    total_ok = sum(v[0] for v in per_cat.values())
    total = sum(v[1] for v in per_cat.values())
    acc = total_ok / total if total else 0.0
    print("\nper-category:")
    for cat in sorted(per_cat):
        ok, n = per_cat[cat]
        print(f"  {cat:<26} {ok}/{n}")
    print(f"\nturn_type_accuracy: {acc:.3f} ({total_ok}/{total}); calls={calls}")
    if wrong:
        print("misclassified:")
        for w in wrong:
            print("  " + w)

    # --- section 2: action mapping for the behavior lifecycle (real DeepSeek) --
    from butler.agent.interpret import resolve_interpreter
    interp = resolve_interpreter(container)
    a_ok = 0
    print("\naction mapping (behavior lifecycle / behavior-vs-tracker):")
    for text, expected in ACTION_CASES:
        if max_calls and calls >= max_calls:
            break
        try:
            req = interp.interpret(text, topic={"topic_name": "Food"})
            got = req.action
        except Exception as exc:  # noqa: BLE001
            got = f"error:{exc}"
        calls += 1
        ok = got == expected
        a_ok += 1 if ok else 0
        print(f"  {'PASS' if ok else 'FAIL'}  {text[:52]!r} -> {got}"
              f" (want {expected.value})")
    a_total = len(ACTION_CASES)
    a_acc = a_ok / a_total if a_total else 0.0
    print(f"\naction_mapping_accuracy: {a_acc:.3f} ({a_ok}/{a_total})")
    print(f"total live calls: {calls}")
    ok_all = acc >= 0.95 and a_acc >= 0.90
    print("RESULT: PASS" if ok_all else "RESULT: FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
