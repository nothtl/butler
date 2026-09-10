"""Phase 7 / M1: the agent runtime.

This is the single deterministic control loop:

    message -> Intent -> ToolCall -> validate -> safety gate -> idempotency
            -> handler -> audit -> ToolResult -> AgentReply

The LLM (if any) may only *propose* the ``ToolCall``. Every side effect passes
through :class:`butler.safety.SafetyPolicy`; every write is recorded in the
audit log; side-effecting calls are made exactly-once via
:class:`butler.idempotency.Idempotency`. A tool that needs consent parks a
:class:`PendingAction` in the session so a later confirmation can run it.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ..idempotency import make_key
from .context import ContextBuilder
from .errors import ToolNotFound, ToolValidationError, UnknownIntent
from .intent import IntentParser
from .models import AgentReply, Intent, ToolCall, ToolResult
from .registry import Tool, ToolRegistry
from .session import PendingAction, SessionStore
from .tools import build_default_registry

log = logging.getLogger("butler.agent.runtime")

Renderer = Callable[[Intent, ToolResult], str]

# decider intent kinds that map onto a differently-named tool.
_ALIASES: dict[str, str] = {
    "plan_tasks": "tasks",
    "storage": "status",
    "why_this": "why",
    "routine_show": "routines",
    "course_help": "course_list",
    "course_docs": "course_list",
    "help": "chat",
}


@dataclass
class _Decision:
    allow: bool
    reason: str = ""
    needed: str = ""


class AgentRuntime:
    def __init__(self, container: Any, *, registry: ToolRegistry | None = None,
                 parser: IntentParser | None = None,
                 store: SessionStore | None = None,
                 renderer: Renderer | None = None, render: bool = False):
        self.container = container
        self.registry = registry or build_default_registry(container)
        decider = getattr(container, "decider", None)
        chat = getattr(container, "chat", None)
        self.parser = parser or IntentParser(decider=decider, chat=chat,
                                             registry=self.registry)
        self.store = store or SessionStore()
        self.context = ContextBuilder(container)
        self.renderer = renderer
        self.render = render

    # ------------------------------------------------------------- public
    def tools(self) -> list[dict[str, Any]]:
        return self.registry.schema()

    def run(self, message: str, user: str = "user",
            confirm: bool = False) -> AgentReply:
        """Parse, gate and execute one user turn."""
        session = self.store.get(user)

        # A confirmation turn executes the parked action instead of parsing.
        if confirm:
            pending = session.take()
            if pending is not None:
                call = ToolCall(name=pending.tool, args=pending.args,
                                reason="user confirmed")
                return self._finish(session, user, message, None, call,
                                    confirmed=True)

        try:
            intent = self.parser.parse(message)
        except UnknownIntent as exc:
            return AgentReply(ok=False, error=str(exc), source="agent")

        call = self._plan(intent)
        session.last_intent = intent
        return self._finish(session, user, message, intent, call,
                            confirmed=bool(confirm or intent.confirmed))

    def run_tool(self, name: str, args: dict[str, Any] | None = None,
                 user: str = "user", confirm: bool = False) -> AgentReply:
        """Invoke a tool directly (used by the LLM tool-selection path / MCP)."""
        call = ToolCall(name=name, args=dict(args or {}), reason="direct")
        session = self.store.get(user)
        return self._finish(session, user, "", None, call, confirmed=confirm)

    # ------------------------------------------------------------ planning
    def _plan(self, intent: Intent) -> ToolCall:
        name = intent.kind
        if not self.registry.has(name):
            name = _ALIASES.get(name, name)
        if not self.registry.has(name):
            if self.registry.has("chat"):
                name = "chat"
            else:
                raise UnknownIntent(f"no tool for intent '{intent.kind}'")
        tool = self.registry.get(name)
        return ToolCall(name=name, args=self._args_for(tool, intent),
                        reason=f"intent:{intent.kind}")

    @staticmethod
    def _args_for(tool: Tool, intent: Intent) -> dict[str, Any]:
        """Deterministically map intent fields onto the tool's declared args."""
        params = {p.name for p in tool.params}
        args: dict[str, Any] = {k: v for k, v in intent.params.items()
                                if k in params}
        if "query" in params and "query" not in args:
            args["query"] = intent.query
        if "target" in params and "target" not in args:
            args["target"] = intent.target
        if "path" in params and "path" not in args:
            args["path"] = intent.target or intent.params.get("path", "")
        if "code" in params and "code" not in args:
            args["code"] = (intent.target or intent.params.get("code")
                            or intent.query)
        if "title" in params and "title" not in args:
            args["title"] = intent.query or intent.target
        if "name" in params and "name" not in args:
            args["name"] = intent.query or intent.target
        if "task_id" in params and "task_id" not in args:
            raw = (intent.target or intent.params.get("task_id")
                   or intent.query)
            digits = "".join(ch for ch in str(raw) if ch.isdigit())
            if digits:
                args["task_id"] = int(digits)
        return args

    # ----------------------------------------------------------- execution
    def execute(self, call: ToolCall, *, user: str = "user",
                confirmed: bool = False) -> ToolResult:
        try:
            tool = self.registry.get(call.name)
        except ToolNotFound as exc:
            return ToolResult(ok=False, name=call.name, error=str(exc),
                              decision="denied")
        try:
            args = self.registry.validate(call.name, call.args)
        except ToolValidationError as exc:
            return ToolResult(ok=False, name=call.name, error=str(exc),
                              decision="invalid")

        run_id = _run_id()
        decision = self._gate(tool, args, user=user, run_id=run_id,
                              confirmed=confirmed)
        if not decision.allow:
            pending_id = ""
            if "confirmation" in (decision.needed or ""):
                pending = PendingAction(id=uuid.uuid4().hex[:12],
                                        tool=tool.name, args=args,
                                        reason=decision.reason,
                                        created=int(time.time()))
                self.store.get(user).park(pending)
                pending_id = pending.id
            return ToolResult(ok=False, name=tool.name, decision="denied",
                              error=decision.reason or "denied",
                              meta={"needed": decision.needed,
                                    "pending_id": pending_id})

        idem = getattr(self.container, "idempotency", None)
        key = ""
        if tool.side_effect and idem is not None:
            key = make_key("agent:" + tool.name, args)
            stored = idem.replay(key)
            if stored is not None:
                self._audit(tool, user, run_id, key, "replayed", "ok")
                return ToolResult(ok=True, name=tool.name, data=stored,
                                  decision="replayed", replayed=True)
            if not idem.start(key, actor=user, action=tool.action):
                return ToolResult(ok=False, name=tool.name,
                                  error="operation already in progress",
                                  decision="denied")

        try:
            data = tool.handler(args)
        except Exception as exc:  # noqa: BLE001 — a tool failure is data, not a crash
            if key:
                idem.fail(key)
            self._audit(tool, user, run_id, key, "error", str(exc))
            log.warning("agent tool %s failed: %s", tool.name, exc)
            return ToolResult(ok=False, name=tool.name, error=str(exc),
                              decision="error")
        if key:
            try:
                idem.finish(key, data)
            except Exception:  # noqa: BLE001 — result not serialisable
                idem.fail(key)
        self._audit(tool, user, run_id, key, "allowed", "ok")
        return ToolResult(ok=True, name=tool.name, data=data,
                          decision="allowed")

    # --------------------------------------------------------------- gate
    def _gate(self, tool: Tool, args: dict[str, Any], *, user: str,
              run_id: str, confirmed: bool) -> _Decision:
        safety = getattr(self.container, "safety", None)
        if safety is None:
            if tool.side_effect:
                return _Decision(False, reason="safety layer unavailable")
            return _Decision(True)
        try:
            decision = safety.check(tool.action or tool.name, actor=user,
                                    run_id=run_id, target=_target(args),
                                    confirmed=confirmed)
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("safety gate error: %s", exc)
            return _Decision(False, reason="safety check failed")
        return _Decision(bool(decision.allow), reason=decision.reason,
                         needed=getattr(decision, "needed", ""))

    # -------------------------------------------------------------- audit
    def _audit(self, tool: Tool, user: str, run_id: str, key: str,
               outcome: str, reason: str) -> None:
        audit = getattr(self.container, "audit", None)
        if audit is None:
            return
        try:
            audit.record(tool.action or tool.name, run_id=run_id, actor=user,
                         kind="agent", decision="allowed" if outcome == "ok"
                         else outcome, reason=reason, outcome=outcome,
                         idem_key=key)
        except Exception:  # noqa: BLE001 — never fail a call on audit trouble
            log.debug("audit record failed", exc_info=True)

    # ------------------------------------------------------------- finish
    def _finish(self, session: Any, user: str, message: str,
                intent: Intent | None, call: ToolCall,
                *, confirmed: bool) -> AgentReply:
        result = self.execute(call, user=user, confirmed=confirmed)
        reply = AgentReply(
            ok=result.ok,
            data=result.data,
            intent=intent,
            tool_calls=[call],
            results=[result],
            source="agent",
            error=result.error,
        )
        if self.render and result.ok and intent is not None:
            reply.text = self._render(intent, result)
        if self.renderer is not None and intent is not None:
            try:
                reply.text = self.renderer(intent, result) or reply.text
            except Exception:  # noqa: BLE001
                log.debug("renderer failed", exc_info=True)
        session.add_turn("user", message)
        session.add_turn("assistant", reply.text or _compact(result.data),
                         meta={"ok": result.ok, "tool": call.name,
                               "decision": result.decision})
        return reply

    def _render(self, intent: Intent, result: ToolResult) -> str:
        chat = getattr(self.container, "chat", None)
        if chat is None or not hasattr(chat, "respond_kind"):
            return ""
        try:
            text = chat.respond_kind(intent.kind, result.data)
            return str(text) if text else ""
        except Exception:  # noqa: BLE001
            return ""


def _target(args: dict[str, Any]) -> str:
    for key in ("path", "code", "title", "name", "task_id", "query"):
        if key in args and args[key] not in (None, ""):
            return f"{key}={args[key]}"
    return ""


def _run_id() -> str:
    try:
        from .. import logging as blog
        rid = blog.get_run_id()
        return rid if rid and rid != "-" else uuid.uuid4().hex[:12]
    except Exception:  # noqa: BLE001
        return uuid.uuid4().hex[:12]


def _compact(data: Any, limit: int = 2000) -> str:
    import json
    try:
        s = json.dumps(data, default=str, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        s = str(data)
    return s if len(s) <= limit else s[:limit] + "…"


def build_runtime(container: Any, **kwargs: Any) -> AgentRuntime:
    """Convenience factory used by the container and tests."""
    return AgentRuntime(container, **kwargs)
