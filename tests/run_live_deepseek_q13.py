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
    print("RESULT: PASS" if acc >= 0.95 else "RESULT: FAIL")
    return 0 if acc >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
