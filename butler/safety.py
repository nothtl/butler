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
    UNKNOWN = "unknown"


# The authoritative action registry (butler/agent/actions.py) is the single
# source of truth for risk class, side effects and confirmation. The policy
# reads it directly. Unknown actions are NOT registered and therefore fail
# closed: unknown -> DENY, unclassified risk -> DENY, unregistered -> DENY.
from .agent.actions import (  # noqa: E402
    RiskClass, get_action, registered_names,
    requires_confirmation as _requires_confirmation,
)

# Actions Butler may perform to files it owns even in degraded mode.
_DEGRADED_ALLOW_WRITE = {
    "food_add", "food_consume", "meal_plan", "meal_cook", "meal_another",
    "meal_add_missing", "meal_not_tonight", "recipe_mark", "note", "log",
    "add_task", "mark_done", "task_lifecycle", "plan_make", "plan_apply",
    "index", "route", "project_create", "project_update", "project_link",
    "project_milestone", "project_dependency",
    "memory_learn", "memory_forget", "memory_confirm", "memory_correct",
    "proactive_snooze", "proactive_suppress",
}


def _risk_lookup(action: str) -> ActionClass:
    definition = get_action(action)
    if definition is None:
        return ActionClass.UNKNOWN
    rc = definition.risk_class
    if rc == RiskClass.READ_ONLY:
        return ActionClass.READ
    if rc in (RiskClass.LOCAL_MUTATION, RiskClass.SCHEDULE):
        return ActionClass.LOW_RISK_WRITE
    return ActionClass.CONSEQUENT_EXTERNAL


# Compatibility views derived from the registry (never hand-maintained).
_KNOWN_RISK: dict[str, str] = {}
_CONFIRM_REQUIRED: set[str] = set()
for _name in registered_names():
    _cls = _risk_lookup(_name)
    _KNOWN_RISK[_name] = {
        ActionClass.READ: "read",
        ActionClass.LOW_RISK_WRITE: "low_risk_write",
        ActionClass.CONSEQUENT_EXTERNAL: "consequent_external",
        ActionClass.UNKNOWN: "unknown",
    }[_cls]
    if _requires_confirmation(_name):
        _CONFIRM_REQUIRED.add(_name)


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
        # Fail closed: an unregistered action always needs confirmation.
        return _requires_confirmation(action)

    # ------------------------------------------------------------------
    def check(self, action: str, *, actor: str = "", run_id: str = "",
              target: str = "", confirmed: bool = False) -> Decision:
        definition = get_action(action)
        # FAIL CLOSED: unknown / unregistered / unclassified actions are denied.
        if definition is None:
            self._record(run_id, actor, action, ActionClass.UNKNOWN, target,
                         "denied", "unregistered_action")
            return Decision(False, action, ActionClass.UNKNOWN,
                            reason=f"action {action!r} is not registered",
                            needed="")
        cls = self.classify(action)

        # Privileged actions are never executable, confirmed or not.
        if definition.risk_class == RiskClass.PRIVILEGED:
            self._record(run_id, actor, action, cls, target,
                         "denied", "privileged_action")
            return Decision(False, action, cls,
                            reason="privileged actions are never allowed",
                            needed="")

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

        # External/destructive/privileged actions require confirmation. This is
        # driven by the registry, so a new external action is confirmation-first
        # by construction rather than by being added to a second list.
        if definition.requires_confirmation and not confirmed:
            self._record(run_id, actor, action, cls, target,
                         "denied", "confirmation_required")
            return Decision(False, action, cls,
                            reason="confirmation required",
                            needed=("user confirmation to run '"
                                    + action + "'"))

        # Registered read-only actions may proceed without confirmation.
        if cls == ActionClass.READ:
            self._record(run_id, actor, action, cls, target, "allowed",
                         "read_only")
            return Decision(True, action, cls, reason="read-only")

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
