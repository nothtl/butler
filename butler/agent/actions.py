"""Authoritative action registry (P1).

The single source of truth for every executable action: its risk class, whether
it has a side effect, and whether it requires confirmation. The safety policy,
the agent runtime, the semantic tool set and the confirmation flow all read from
here. Unknown actions are NOT in this table and therefore fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskClass(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_MUTATION = "local_mutation"
    SCHEDULE = "schedule"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    risk_class: RiskClass
    side_effect: bool = False
    requires_confirmation: bool = False
    allowed_scopes: tuple[str, ...] = ("any",)
    required_permissions: tuple[str, ...] = ()
    description: str = ""
    when_to_use: str = ""
    when_not_to_use: str = ""
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "risk_class": self.risk_class.value,
            "side_effect": self.side_effect,
            "requires_confirmation": self.requires_confirmation,
            "allowed_scopes": list(self.allowed_scopes),
            "required_permissions": list(self.required_permissions),
            "description": self.description,
            "when_to_use": self.when_to_use,
            "when_not_to_use": self.when_not_to_use,
            "examples": list(self.examples),
        }


_TABLE: dict[str, tuple[str, bool, bool]] = {
    "add_task": ("LOCAL_MUTATION", True, False),
    "around_me": ("READ_ONLY", False, False),
    "ask": ("READ_ONLY", False, False),
    "audit": ("READ_ONLY", False, False),
    "backup": ("READ_ONLY", False, False),
    "block": ("LOCAL_MUTATION", True, False),
    "briefing": ("READ_ONLY", False, False),
    "cancel_task": ("LOCAL_MUTATION", True, False),
    "chat": ("READ_ONLY", False, False),
    "complete_task": ("LOCAL_MUTATION", True, False),
    "context": ("READ_ONLY", False, False),
    "course_add": ("LOCAL_MUTATION", True, False),
    "course_docs": ("READ_ONLY", False, False),
    "course_drop": ("LOCAL_MUTATION", True, False),
    "course_list": ("READ_ONLY", False, False),
    "course_monitor": ("EXTERNAL_WRITE", True, True),
    "create_item": ("LOCAL_MUTATION", True, False),
    "create_project": ("LOCAL_MUTATION", True, False),
    "create_task": ("LOCAL_MUTATION", True, False),
    "create_workspace": ("EXTERNAL_WRITE", True, True),
    "day": ("READ_ONLY", False, False),
    "defer": ("LOCAL_MUTATION", True, False),
    "delete_file": ("DESTRUCTIVE", True, True),
    "delete_files": ("PRIVILEGED", True, True),
    "dupes": ("READ_ONLY", False, False),
    "empty_trash": ("DESTRUCTIVE", True, True),
    "evaluate_schedule": ("READ_ONLY", False, False),
    "favorites": ("READ_ONLY", False, False),
    "feasibility": ("READ_ONLY", False, False),
    "file_delete": ("PRIVILEGED", True, True),
    "find": ("READ_ONLY", False, False),
    "find_best_slot": ("READ_ONLY", False, False),
    "food_add": ("LOCAL_MUTATION", True, False),
    "food_consume": ("LOCAL_MUTATION", True, False),
    "food_expiring": ("READ_ONLY", False, False),
    "food_list": ("READ_ONLY", False, False),
    "gcal_delete": ("EXTERNAL_WRITE", True, True),
    "gcal_sync": ("EXTERNAL_WRITE", True, True),
    "gcal_write": ("EXTERNAL_WRITE", True, True),
    "grocery": ("READ_ONLY", False, False),
    "ha_call": ("EXTERNAL_WRITE", True, True),
    "health": ("READ_ONLY", False, False),
    "help": ("READ_ONLY", False, False),
    "index": ("LOCAL_MUTATION", True, False),
    "knowledge_lookup": ("READ_ONLY", False, False),
    "link_download": ("EXTERNAL_WRITE", True, True),
    "link_items": ("LOCAL_MUTATION", True, False),
    "list": ("READ_ONLY", False, False),
    "location_change": ("LOCAL_MUTATION", True, False),
    "log": ("LOCAL_MUTATION", True, False),
    "mark_done": ("LOCAL_MUTATION", True, False),
    "meal_add_missing": ("LOCAL_MUTATION", True, False),
    "meal_another": ("LOCAL_MUTATION", True, False),
    "meal_cook": ("LOCAL_MUTATION", True, False),
    "meal_not_tonight": ("LOCAL_MUTATION", True, False),
    "meal_plan": ("LOCAL_MUTATION", True, False),
    "memory_confirm": ("LOCAL_MUTATION", True, False),
    "memory_correct": ("LOCAL_MUTATION", True, False),
    "memory_explain": ("READ_ONLY", False, False),
    "memory_forget": ("LOCAL_MUTATION", True, False),
    "memory_learn": ("LOCAL_MUTATION", True, False),
    "memory_query": ("READ_ONLY", False, False),
    "memory_search": ("READ_ONLY", False, False),
    "mkdir": ("EXTERNAL_WRITE", True, True),
    "move": ("LOCAL_MUTATION", True, False),
    "move_block": ("LOCAL_MUTATION", True, False),
    "move_file": ("DESTRUCTIVE", True, True),
    "multimodal": ("READ_ONLY", False, False),
    "nas_ingest": ("EXTERNAL_WRITE", True, True),
    "note": ("LOCAL_MUTATION", True, False),
    "now": ("READ_ONLY", False, False),
    "optimize_day": ("READ_ONLY", False, False),
    "optimize_week": ("READ_ONLY", False, False),
    "organize": ("EXTERNAL_WRITE", True, True),
    "organize_items": ("LOCAL_MUTATION", True, False),
    "plan_apply": ("LOCAL_MUTATION", True, False),
    "plan_day": ("READ_ONLY", False, False),
    "plan_make": ("LOCAL_MUTATION", True, False),
    "plan_tasks": ("READ_ONLY", False, False),
    "plan_week": ("READ_ONLY", False, False),
    "plan_why": ("READ_ONLY", False, False),
    "preview_creation": ("READ_ONLY", False, False),
    "proactive": ("READ_ONLY", False, False),
    "proactive_explain": ("READ_ONLY", False, False),
    "proactive_list": ("READ_ONLY", False, False),
    "proactive_query": ("READ_ONLY", False, False),
    "proactive_snooze": ("LOCAL_MUTATION", True, False),
    "proactive_suppress": ("LOCAL_MUTATION", True, False),
    "project_create": ("LOCAL_MUTATION", True, False),
    "project_dependencies": ("READ_ONLY", False, False),
    "project_dependency": ("LOCAL_MUTATION", True, False),
    "project_link": ("LOCAL_MUTATION", True, False),
    "project_milestone": ("LOCAL_MUTATION", True, False),
    "project_next": ("READ_ONLY", False, False),
    "project_risk": ("READ_ONLY", False, False),
    "project_status": ("READ_ONLY", False, False),
    "project_update": ("LOCAL_MUTATION", True, False),
    "project_workload": ("READ_ONLY", False, False),
    "projects": ("READ_ONLY", False, False),
    "recipe": ("READ_ONLY", False, False),
    "recipe_history": ("READ_ONLY", False, False),
    "recipe_library": ("READ_ONLY", False, False),
    "recipe_mark": ("LOCAL_MUTATION", True, False),
    "recipe_search": ("READ_ONLY", False, False),
    "recommend": ("READ_ONLY", False, False),
    "recover": ("READ_ONLY", False, False),
    "reschedule": ("LOCAL_MUTATION", True, False),
    "reschedule_optimized": ("LOCAL_MUTATION", True, False),
    "resolve_reference": ("READ_ONLY", False, False),
    "resume": ("LOCAL_MUTATION", True, False),
    "review": ("READ_ONLY", False, False),
    "rewards": ("READ_ONLY", False, False),
    "rm": ("PRIVILEGED", True, True),
    "route": ("LOCAL_MUTATION", True, False),
    "routine_change": ("LOCAL_MUTATION", True, False),
    "schedule": ("READ_ONLY", False, False),
    "schedule_change": ("LOCAL_MUTATION", True, False),
    "schedule_why": ("READ_ONLY", False, False),
    "search": ("READ_ONLY", False, False),
    "send_message": ("EXTERNAL_WRITE", True, True),
    "settings_update": ("LOCAL_MUTATION", True, False),
    "settings_view": ("READ_ONLY", False, False),
    "shell": ("PRIVILEGED", True, True),
    "skill_describe": ("READ_ONLY", False, False),
    "skip": ("LOCAL_MUTATION", True, False),
    "sleep_event": ("LOCAL_MUTATION", True, False),
    "status": ("READ_ONLY", False, False),
    "storage": ("READ_ONLY", False, False),
    "task_lifecycle": ("LOCAL_MUTATION", True, False),
    "task_update": ("LOCAL_MUTATION", True, False),
    "tasks": ("READ_ONLY", False, False),
    "teach": ("LOCAL_MUTATION", True, False),
    "telegram_send": ("EXTERNAL_WRITE", True, True),
    "timeline": ("READ_ONLY", False, False),
    "tracker_control": ("LOCAL_MUTATION", True, False),
    "tracker_create": ("LOCAL_MUTATION", True, False),
    "tracker_evaluate": ("LOCAL_MUTATION", True, False),
    "tracker_explain": ("READ_ONLY", False, False),
    "tracker_list": ("READ_ONLY", False, False),
    "tracker_query": ("READ_ONLY", False, False),
    "trash": ("DESTRUCTIVE", True, True),
    "trash_duplicates": ("DESTRUCTIVE", True, True),
    "trash_list": ("READ_ONLY", False, False),
    "undo": ("LOCAL_MUTATION", True, False),
    "undo_list": ("READ_ONLY", False, False),
    "unknown": ("READ_ONLY", False, False),
    "update": ("LOCAL_MUTATION", True, False),
    "update_item": ("LOCAL_MUTATION", True, False),
    "urgency": ("READ_ONLY", False, False),
    "web_fetch": ("READ_ONLY", False, False),
    "web_research": ("READ_ONLY", False, False),
    "web_search": ("READ_ONLY", False, False),
    "where_am_i": ("READ_ONLY", False, False),
    "why": ("READ_ONLY", False, False),
    "came_up": ("READ_ONLY", False, False),
    "cancel": ("LOCAL_MUTATION", True, False),
    "connect": ("LOCAL_MUTATION", True, False),
    "course_assignments": ("READ_ONLY", False, False),
    "course_check": ("READ_ONLY", False, False),
    "defer_feedback": ("READ_ONLY", False, False),
    "delete_duplicates": ("DESTRUCTIVE", True, True),
    "done": ("LOCAL_MUTATION", True, False),
    "duplicates": ("READ_ONLY", False, False),
    "routine_add": ("LOCAL_MUTATION", True, False),
    "routine_confirm": ("LOCAL_MUTATION", True, False),
    "routine_explicit": ("LOCAL_MUTATION", True, False),
    "routine_forget": ("LOCAL_MUTATION", True, False),
    "routine_reject": ("LOCAL_MUTATION", True, False),
    "routine_show": ("READ_ONLY", False, False),
    "start": ("LOCAL_MUTATION", True, False),
    "timeline_today": ("READ_ONLY", False, False),
    "week": ("READ_ONLY", False, False),
    "why_this": ("READ_ONLY", False, False),
    "workspace": ("EXTERNAL_WRITE", True, True),
}

ACTIONS: dict[str, ActionDefinition] = {
    name: ActionDefinition(name=name, risk_class=RiskClass[rc],
                           side_effect=se, requires_confirmation=conf)
    for name, (rc, se, conf) in _TABLE.items()
}


def get_action(name: str) -> ActionDefinition | None:
    return ACTIONS.get(str(name or ""))


def is_registered(name: str) -> bool:
    return str(name or "") in ACTIONS


def risk_for(name: str) -> RiskClass | None:
    d = ACTIONS.get(str(name or ""))
    return d.risk_class if d else None


def requires_confirmation(name: str) -> bool:
    """Fail closed: an unregistered action always needs confirmation."""
    d = ACTIONS.get(str(name or ""))
    return True if d is None else d.requires_confirmation


def all_actions() -> list[ActionDefinition]:
    return list(ACTIONS.values())


def registered_names() -> list[str]:
    return sorted(ACTIONS)


# ---------------------------------------------------------------------------
# Q1: action slot schemas
#
# Each executable action declares the slots it needs. This is the authoritative
# source used by the clarification engine: required slots must be present (or
# safely defaultable) before execution; optional slots improve the result.
# `kind` is one of choice|target|value|time|scope|confirmation. `dynamic`
# marks slots whose options come from live state (candidates) or topic scope.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotSpec:
    name: str
    kind: str = "value"
    required: bool = False
    question: str = ""
    options: tuple[str, ...] = ()
    default: Any = None
    free_text: bool = True
    dynamic: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "required": self.required,
            "question": self.question, "options": list(self.options),
            "default": self.default, "free_text": self.free_text,
            "dynamic": self.dynamic,
        }


SLOT_SCHEMAS: dict[str, tuple[SlotSpec, ...]] = {
    "tracker_create": (
        SlotSpec("target", "target", True,
                 "What should I track?",
                 dynamic="candidates"),
        SlotSpec("event_types", "choice", True,
                 "What should I watch for?",
                 ("Assignments", "Deadlines", "Projects", "Announcements",
                  "Everything important")),
        SlotSpec("cadence", "choice", False, "How often should I check?",
                 ("Hourly", "Daily", "Weekly"), default="daily"),
        SlotSpec("destination", "choice", False, "Where should I notify you?",
                 ("This topic", "Main chat"), default="current_topic"),
        SlotSpec("notification_policy", "choice", False,
                 "How should I notify you?",
                 ("Immediately", "Important only", "Daily summary",
                  "Don't notify"), default="important_only"),
    ),
    "create_item": (
        SlotSpec("title", "value", True, "What should I add?"),
        SlotSpec("target_type", "choice", False, "What kind of item?",
                 ("Task", "Project", "Course", "Food", "Grocery", "Note")),
        SlotSpec("deadline", "time", False, "When is it due?"),
        SlotSpec("project", "target", False, "Which project?",
                 dynamic="candidates"),
    ),
    "link_items": (
        SlotSpec("target", "target", True, "What should I link it to?",
                 dynamic="candidates"),
    ),
    "settings_update": (
        SlotSpec("capability", "choice", True, "Which setting?",
                 ("Web", "Scheduling", "Tracking", "Proactive", "Memory",
                  "Reminders", "Files")),
        SlotSpec("state", "choice", True, "Turn it on or off?",
                 ("On", "Off"), default="on"),
        SlotSpec("scope", "scope", True, "Just here, or everywhere?",
                 ("This topic", "Global")),
    ),
    "find_best_slot": (
        SlotSpec("duration", "choice", True, "How long do you need?",
                 ("30 min", "1 hour", "2 hours", "Custom")),
        SlotSpec("target", "target", False, "For what?",
                 dynamic="candidates"),
        SlotSpec("time_window", "time", False, "When?",
                 ("Now", "Today", "Tomorrow", "This evening", "Custom")),
    ),
    "plan_day": (
        SlotSpec("time_window", "time", False, "Which day?",
                 ("Today", "Tomorrow")),
    ),
    "plan_week": (
        SlotSpec("time_window", "time", False, "Which week?",
                 ("This week", "Next week")),
    ),
    "memory_learn": (
        SlotSpec("content", "value", True, "What should I remember?"),
    ),
    "reschedule": (
        SlotSpec("target", "target", True, "What should I move?",
                 dynamic="candidates"),
        SlotSpec("time_window", "time", False, "When should it move to?",
                 ("Today", "Tomorrow", "This evening", "Custom")),
    ),
    "defer": (
        SlotSpec("target", "target", False, "What should I defer?",
                 dynamic="candidates"),
        SlotSpec("time_window", "time", False, "Until when?",
                 ("Later today", "Tomorrow", "This evening", "Custom")),
    ),
    "move": (
        SlotSpec("target", "target", True, "What should I move?",
                 dynamic="candidates"),
        SlotSpec("time_window", "time", False, "When?",
                 ("Today", "Tomorrow", "This evening", "Custom")),
    ),
}


def slots_for(action: str) -> tuple[SlotSpec, ...]:
    return SLOT_SCHEMAS.get(str(action or ""), ())


def required_slots(action: str) -> tuple[SlotSpec, ...]:
    return tuple(s for s in slots_for(action) if s.required)


def optional_slots(action: str) -> tuple[SlotSpec, ...]:
    return tuple(s for s in slots_for(action) if not s.required)


def slot_spec(action: str, name: str) -> SlotSpec | None:
    for s in slots_for(action):
        if s.name == name:
            return s
    return None
