"""Phase 6: safety & action policy boundary.

This is the *deterministic* gate between intent and execution. The LLM never
performs an external side effect on its own: it can only produce an intent; this
module classifies that intent and decides whether the side effect may proceed.
Uncertainty is NEVER turned into false success — if a decision cannot be proven
safe, it is denied (or degraded) and recorded accordingly.

Classification:

* ``READ``               — inspect-only: search, list, snapshot, describe.
* ``LOW_RISK_WRITE``     — mutates Butler-owned durable state, fully reversible
                           by Butler itself (task/plan/exec state, own files).
* ``CONSEQUENT_EXTERNAL``— reaches outside Butler's own sandbox (Google Calendar
                           writes/delete, Telegram send, Home Assistant calls,
                           trash/delete, and any file outside a Butler-owned
                           root). Deny-by-default unless owned/confirmed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import audit as audit_mod


class ActionClass(str, Enum):
    READ = "read"
    LOW_RISK_WRITE = "low_risk_write"
    CONSEQUENT_EXTERNAL = "consequent_external"


# A deterministic intent -> classification table. When an intent is unknown it
# is treated as the *least* trusted class unless it demonstrably has no side
# effect. Route updates live in one place so a new command is reviewed here
# before it can act at all.
_KNOWN_RISK: dict[str, str] = {
    # --- read / inspect ---
    "help": "read", "storage": "read", "status": "read", "list": "read",
    "find": "read", "search": "read", "dupes": "read", "trash_list": "read",
    "chat": "read",
    "day": "read", "now": "read", "tasks": "read", "why": "read",
    "context": "read", "timeline": "read", "briefing": "read", "review": "read",
    "recipe": "read", "recipe_search": "read", "recipe_library": "read",
    "favorites": "read", "recipe_history": "read", "grocery": "read",
    "food_list": "read", "food_expiring": "read", "course_list": "read",
    "course_docs": "read", "where_am_i": "read", "around_me": "read",
    "backup": "read", "health": "read", "audit": "read", "undo_list": "read",
    "recover": "read", "multimodal": "read", "skill_describe": "read",
    "proactive": "read", "plan_tasks": "read", "plan_why": "read",
    "schedule": "read", "schedule_why": "read",
    # --- low-risk write (Butler-owned, reversible) ---
    "add_task": "low_risk_write", "task_update": "low_risk_write",
    "plan_make": "low_risk_write", "plan_apply": "low_risk_write",
    "task_lifecycle": "low_risk_write", "defer": "low_risk_write",
    "block": "low_risk_write", "resume": "low_risk_write",
    "skip": "low_risk_write", "cancel_task": "low_risk_write",
    "mark_done": "low_risk_write", "note": "low_risk_write",
    "food_add": "low_risk_write", "food_consume": "low_risk_write",
    "meal_plan": "low_risk_write", "meal_cook": "low_risk_write",
    "meal_another": "low_risk_write", "meal_add_missing": "low_risk_write",
    "meal_not_tonight": "low_risk_write", "recipe_mark": "low_risk_write",
    "route": "low_risk_write", "index": "low_risk_write",
    "location_change": "low_risk_write", "move_block": "low_risk_write",
    "teach": "low_risk_write", "routine_change": "low_risk_write",
    "log": "low_risk_write", "sleep_event": "low_risk_write",
    "schedule_change": "low_risk_write",
    # M3: project data is Butler-owned and reversible.
    "project_create": "low_risk_write", "project_update": "low_risk_write",
    "project_link": "low_risk_write", "project_milestone": "low_risk_write",
    "project_dependency": "low_risk_write",
    # --- consequent external (deny-by-default) ---
    "organize": "consequent_external", "create_workspace": "consequent_external",
    "trash_duplicates": "consequent_external", "empty_trash": "consequent_external",
    "mkdir": "consequent_external", "trash": "consequent_external",
    "delete_file": "consequent_external", "move_file": "consequent_external",
    "gcal_write": "consequent_external", "gcal_delete": "consequent_external",
    "gcal_sync": "consequent_external", "telegram_send": "consequent_external",
    "ha_call": "consequent_external", "nas_ingest": "consequent_external",
    "course_monitor": "consequent_external", "link_download": "consequent_external",
    "send_message": "consequent_external",
}

# External actions that ALWAYS require a human confirmation before executing.
_CONFIRM_REQUIRED = {
    "organize", "create_workspace", "trash_duplicates", "empty_trash",
    "mkdir", "trash", "delete_file", "move_file", "gcal_delete",
    "gcal_write", "course_delete",
}

# Actions Butler may perform to files it owns even in degraded mode.
_DEGRADED_ALLOW_WRITE = {
    "food_add", "food_consume", "meal_plan", "meal_cook", "meal_another",
    "meal_add_missing", "meal_not_tonight", "recipe_mark", "note", "log",
    "add_task", "mark_done", "task_lifecycle", "plan_make", "plan_apply",
    "index", "route", "project_create", "project_update", "project_link",
    "project_milestone", "project_dependency",
}


def _risk_lookup(action: str) -> ActionClass:
    raw = _KNOWN_RISK.get(action, "")
    if raw == "read":
        return ActionClass.READ
    if raw == "low_risk_write":
        return ActionClass.LOW_RISK_WRITE
    if raw == "consequent_external":
        return ActionClass.CONSEQUENT_EXTERNAL
    # Unknown: the safe default is to treat it as *external* so the gate forces
    # an explicit policy decision rather than silently running side effects.
    return ActionClass.CONSEQUENT_EXTERNAL


@dataclass
class Decision:
    """A policy verdict. ``allow`` is True only when safe to run."""
    allow: bool
    action: str
    cls: ActionClass
    reason: str = ""
    needed: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "action": self.action,
                "cls": self.cls.value, "reason": self.reason,
                "needed": self.needed}


class SafetyPolicy:
    """The deterministic gate between Intent and Execution.

    ``container`` provides the Config and Audit used for rate limiting and for
    recording every decision. The gate is callable: ``policy(intent)`` and, more
    conveniently, ``policy.check(action, actor=..., run_id=...)``.
    """

    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.audit: audit_mod.Audit = getattr(container, "audit", None)
        self.retry = getattr(container, "retry", None)

    # ------------------------------------------------------------------
    def classify(self, action: str) -> ActionClass:
        return _risk_lookup(action)

    def needs_confirmation(self, action: str) -> bool:
        return action in _CONFIRM_REQUIRED

    # ------------------------------------------------------------------
    def check(self, action: str, *, actor: str = "", run_id: str = "",
              target: str = "", confirmed: bool = False) -> Decision:
        cls = self.classify(action)
        reason = ""

        # Rate limit: refuses to run too many consequent-external actions in a
        # short window (a runaway/retry storm). Only applies to the destructive
        # class — reads and local writes are cheap and never rate-limited.
        if cls == ActionClass.CONSEQUENT_EXTERNAL and self.retry is not None:
            if not self._rate_ok(action):
                self._record(run_id, actor, action, cls, target,
                             "denied", "rate_limit_exceeded")
                return Decision(False, action, cls,
                                reason="rate limit exceeded", needed="later")

        # Circuit breaker: if a class has been failing repeatedly, pause it.
        if cls == ActionClass.CONSEQUENT_EXTERNAL and self.retry is not None:
            if self.retry.breaker_open():
                self._record(run_id, actor, action, cls, target,
                             "degraded", "circuit_open")
                return Decision(False, action, cls,
                                reason="circuit breaker open", needed="recover")

        # Deny-by-default for external actions unless the caller proves
        # ownership/confirmation. Low-risk writes and reads always proceed.
        if cls == ActionClass.CONSEQUENT_EXTERNAL:
            if not confirmed and self.needs_confirmation(action):
                self._record(run_id, actor, action, cls, target,
                             "denied", "confirmation_required")
                return Decision(False, action, cls,
                                reason="confirmation required",
                                needed=("user confirmation to run '"
                                        + action + "'"))
            self._record(run_id, actor, action, cls, target, "allowed",
                         "policy_allowed")
            return Decision(True, action, cls,
                            reason="external action confirmed as safe")

        # Degraded mode: block writes that reach outside but allow local ones.
        if self.cfg is not None and getattr(self.cfg, "degraded_mode", False):
            if cls == ActionClass.CONSEQUENT_EXTERNAL:
                self._record(run_id, actor, action, cls, target,
                             "denied", "degraded_mode")
                return Decision(False, action, cls,
                                reason="degraded mode: external action blocked")
            if cls == ActionClass.LOW_RISK_WRITE and \
                    action not in _DEGRADED_ALLOW_WRITE:
                self._record(run_id, actor, action, cls, target,
                             "degraded", "degraded_write")
                return Decision(False, action, cls,
                                reason="degraded mode: write paused")

        self._record(run_id, actor, action, cls, target, "allowed", "ok")
        return Decision(True, action, cls, reason="allowed")

    # ------------------------------------------------------------------
    def _rate_ok(self, action: str) -> bool:
        assert self.retry is not None
        return self.retry.acquire_permits("external", n=1)

    def _record(self, run_id: str, actor: str, action: str,
                cls: ActionClass, target: str, decision: str,
                reason: str) -> None:
        if self.audit is None:
            return
        self.audit.record(action, run_id=run_id, actor=actor, kind=cls.value,
                          target=target, decision=decision, reason=reason,
                          outcome="not_applied" if decision != "allowed" else "ok")
