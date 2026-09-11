"""Q3 case scoring, failure taxonomy and behavioral components."""
from __future__ import annotations

from typing import Any

FAILURE_CATEGORIES = (
    "WRONG_INTENT", "WRONG_TARGET", "WRONG_SCOPE", "WRONG_SLOT", "WRONG_TIME",
    "MISSED_CLARIFICATION", "UNNECESSARY_CLARIFICATION", "FOLLOWUP_FAILURE",
    "CONTEXT_FAILURE", "TOOL_SELECTION_FAILURE", "ARGUMENT_FAILURE",
    "CONFIRMATION_FAILURE", "PROVENANCE_FAILURE", "MODEL_FAILURE",
    "SERVER_VALIDATION_FAILURE", "TAXONOMY_ONLY", "OTHER",
)

#: coarse behavior families (user-meaningful, not enum labels)
BEHAVIOR_FAMILY = {
    "create_item": "create", "create_task": "create", "create_project": "create",
    "link_items": "link", "organize_items": "link",
    "tracker_create": "track", "tracker_control": "track",
    "tracker_list": "query", "tracker_query": "query", "tracker_explain": "query",
    "status": "query", "knowledge_lookup": "query", "memory_query": "query",
    "project_status": "query", "project_workload": "query", "project_risk": "query",
    "project_dependencies": "query", "project_next": "query", "urgency": "query",
    "recommend": "query", "feasibility": "query", "proactive_query": "query",
    "proactive_list": "query", "proactive_explain": "query",
    "update": "update", "update_item": "update", "reschedule": "update",
    "move": "update", "defer": "update", "complete_task": "update",
    "settings_update": "settings", "settings_view": "settings",
    "memory_learn": "memory", "memory_forget": "memory", "memory_confirm": "memory",
    "memory_correct": "memory", "memory_search": "query", "memory_explain": "query",
    "find_best_slot": "schedule", "plan_day": "schedule", "plan_week": "schedule",
    "optimize_day": "schedule", "optimize_week": "schedule",
    "reschedule_optimized": "schedule", "evaluate_schedule": "schedule",
    "web_search": "web", "web_research": "web", "web_fetch": "web",
    "proactive_snooze": "proactive", "proactive_suppress": "proactive",
    "unknown": "unknown", "chat": "chat",
}


def family(action: str | None) -> str:
    return BEHAVIOR_FAMILY.get(str(action or ""), "other")


def _acceptable(case: dict[str, Any]) -> set[str]:
    acc = set(case.get("acceptable_actions") or [])
    if case.get("expected_action"):
        acc.add(case["expected_action"])
    return acc


def score_case(case: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Score one normalized output. Components are bool or None (n/a)."""
    exp_actions = _acceptable(case)
    exp_family = family(case.get("expected_action"))
    action = out.get("action")
    error = out.get("error") or ""
    has_action = bool(exp_actions)

    intent_ok: bool | None = None
    behavior_ok: bool | None = None
    taxonomy_only = False
    if has_action:
        intent_ok = bool(action) and action in exp_actions
        behavior_ok = bool(action) and (intent_ok or family(action) == exp_family)
        taxonomy_only = bool(action) and not intent_ok and \
            family(action) == exp_family

    # target
    target_ok: bool | None = None
    if case.get("expected_target"):
        target_ok = str((out.get("target") or {}).get("name", "")).lower() == \
            str(case["expected_target"]).lower()

    # temporal
    temporal_ok: bool | None = None
    if "expected_resolved" in case:
        temporal_ok = bool(out.get("resolved")) == bool(case["expected_resolved"])

    # clarification
    clar_ok: bool | None = None
    if case.get("kind") in ("clarification", "ambiguity", "followup") \
            or "expect_clarification" in case:
        needed = bool(case.get("expect_clarification", False))
        offered = bool((out.get("clarification") or {}).get("offered"))
        clar_ok = (needed == offered)

    # follow-up
    followup_ok: bool | None = None
    if case.get("kind") == "followup" or "expect_followup" in case:
        followup_ok = bool(out.get("followup_resolved"))

    # confirmation
    confirmation_ok: bool | None = None
    if "expect_confirmation" in case:
        confirmation_ok = bool(out.get("confirmation")) == \
            bool(case["expect_confirmation"])

    # slot accuracy (if the case lists expected slots)
    slot_ok: bool | None = None
    if case.get("expected_slots"):
        got = out.get("slots") or {}
        slot_ok = all(str(got.get(k, "")).lower() == str(v).lower()
                      for k, v in case["expected_slots"].items())

    task_success = bool((behavior_ok is not False) and (temporal_ok is not False)
                        and (clar_ok is not False)
                        and (followup_ok is not False)
                        and (confirmation_ok is not False)
                        and (slot_ok is not False)
                        and not error)

    # primary failure category (exactly one)
    primary = ""
    secondary: list[str] = []
    if error:
        primary = ("SERVER_VALIDATION_FAILURE"
                   if "SemanticValidation" in error else "MODEL_FAILURE")
    elif not action and case.get("expected_action"):
        primary = "SERVER_VALIDATION_FAILURE"
    elif taxonomy_only and task_success:
        primary = "TAXONOMY_ONLY"
    elif temporal_ok is False:
        primary = "WRONG_TIME"
    elif clar_ok is False:
        primary = ("UNNECESSARY_CLARIFICATION"
                   if not case.get("expect_clarification")
                   else "MISSED_CLARIFICATION")
    elif followup_ok is False:
        primary = "FOLLOWUP_FAILURE"
    elif confirmation_ok is False:
        primary = "CONFIRMATION_FAILURE"
    elif target_ok is False:
        primary = "WRONG_TARGET"
    elif slot_ok is False:
        primary = "WRONG_SLOT"
    elif behavior_ok is False:
        primary = "WRONG_INTENT"
    else:
        primary = ""
    if taxonomy_only and primary not in ("TAXONOMY_ONLY", ""):
        secondary.append("TAXONOMY_ONLY")

    return {
        "task_success": task_success,
        "intent_accuracy": intent_ok,
        "target_accuracy": target_ok,
        "slot_accuracy": slot_ok,
        "clarification_quality": clar_ok,
        "followup_accuracy": followup_ok,
        "tool_selection": intent_ok,
        "confirmation_accuracy": confirmation_ok,
        "temporal_accuracy": temporal_ok,
        "failure": primary,
        "secondary": secondary,
    }
