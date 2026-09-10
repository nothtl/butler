"""Phase 7 / M1: typed tool registry.

A :class:`Tool` declares its name, description, argument schema, the safety
``action`` it maps to, and whether it has a side effect. The registry is the
single source of truth for "what can the agent do", replacing the three
parallel hand-maintained lists (decider dispatch, MCP ``_tools_spec``, Telegram
handlers) that drift apart.

The registry is *pure*: it validates and calls handlers. It does not gate,
audit or retry — that is the runtime's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import ToolNotFound, ToolValidationError

Handler = Callable[[dict[str, Any]], Any]


@dataclass
class Param:
    """One validated argument. ``type`` is a primitive name, not a Python type,
    so the schema is trivially serialisable for prompts and MCP."""

    name: str
    type: str = "str"                 # str | int | float | bool | list
    required: bool = False
    default: Any = None
    description: str = ""
    choices: tuple[str, ...] = ()

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "description": self.description,
            "choices": list(self.choices),
        }


@dataclass
class Tool:
    """A typed capability the agent may invoke.

    A tool is defined *once* and carries every piece of metadata any front-end
    needs: the safety ``action``, whether it has a ``side_effect``, the name it
    is exposed under to MCP clients (``mcp_name``), alternate names it may be
    invoked by (``aliases``), whether the policy requires confirmation, and
    whether the tool owns its own gate (``delegated_gate`` — used by the
    decider bridge, whose handler consults the shared SafetyPolicy itself).
    """

    name: str
    description: str
    handler: Handler
    params: list[Param] = field(default_factory=list)
    action: str = ""                  # safety action name (defaults to name)
    side_effect: bool = False
    returns: str = ""
    aliases: tuple[str, ...] = ()
    mcp_name: str = ""                # exposed name for MCP clients ("" = hidden)
    needs_confirmation: bool = False
    delegated_gate: bool = False      # handler runs its own SafetyPolicy gate
    hidden: bool = False              # omitted from LLM prompt / discovery
    profile: str = "full"             # MCP surface this tool belongs to

    def __post_init__(self) -> None:
        if not self.action:
            self.action = self.name

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "action": self.action,
            "side_effect": self.side_effect,
            "returns": self.returns,
            "params": [p.to_schema() for p in self.params],
        }

    def mcp_schema(self) -> dict[str, Any]:
        """The MCP ``tools/list`` entry for this tool."""
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in self.params:
            props[p.name] = _json_schema(p)
            if p.required:
                required.append(p.name)
        return {
            "name": self.mcp_name,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": props,
                            "required": required},
        }


class ToolRegistry:
    """An ordered, validated collection of :class:`Tool`."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ------------------------------------------------------------------ build
    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(self, name: str, description: str, handler: Handler,
            params: list[Param] | None = None, *, action: str = "",
            side_effect: bool = False, returns: str = "",
            aliases: tuple[str, ...] = (), mcp_name: str = "",
            needs_confirmation: bool = False, delegated_gate: bool = False,
            hidden: bool = False, profile: str = "full") -> Tool:
        return self.register(Tool(name=name, description=description,
                                  handler=handler, params=params or [],
                                  action=action, side_effect=side_effect,
                                  returns=returns, aliases=aliases,
                                  mcp_name=mcp_name,
                                  needs_confirmation=needs_confirmation,
                                  delegated_gate=delegated_gate, hidden=hidden,
                                  profile=profile))

    # ------------------------------------------------------------------ query
    def _resolve(self, name: str) -> str | None:
        if name in self._tools:
            return name
        for tool in self._tools.values():
            if name in tool.aliases:
                return tool.name
        return None

    def get(self, name: str) -> Tool:
        resolved = self._resolve(name)
        if resolved is None:
            raise ToolNotFound(f"unknown tool: {name}")
        return self._tools[resolved]

    def has(self, name: str) -> bool:
        return self._resolve(name) is not None

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def find_mcp(self, name: str, profile: str | None = None) -> Tool | None:
        for tool in self._tools.values():
            if tool.mcp_name != name:
                continue
            if profile is not None and tool.profile != profile:
                continue
            return tool
        return None

    def schema(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values() if not t.hidden]

    def mcp_schema(self, profile: str | None = None) -> list[dict[str, Any]]:
        return [t.mcp_schema() for t in self._tools.values()
                if t.mcp_name and (profile is None or t.profile == profile)]

    def merge(self, other: "ToolRegistry", *, prefix: str = "") -> None:
        """Absorb another registry, renaming collisions with ``prefix``."""
        for tool in other.all():
            if tool.name in self._tools:
                if not prefix:
                    raise ValueError(f"duplicate tool while merging: {tool.name}")
                tool = Tool(
                    name=prefix + tool.name, description=tool.description,
                    handler=tool.handler, params=list(tool.params),
                    action=tool.action, side_effect=tool.side_effect,
                    returns=tool.returns, aliases=tool.aliases,
                    mcp_name=tool.mcp_name,
                    needs_confirmation=tool.needs_confirmation,
                    delegated_gate=tool.delegated_gate, hidden=tool.hidden,
                    profile=tool.profile)
            self.register(tool)

    # --------------------------------------------------------------- validate
    def validate(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        """Return a cleaned argument dict or raise :class:`ToolValidationError`.

        Unknown keys are rejected (they usually mean the model hallucinated a
        parameter); missing required keys and bad types are rejected; defaults
        are filled in.
        """
        tool = self.get(name)
        given = dict(args or {})
        known = {p.name: p for p in tool.params}
        unknown = sorted(set(given) - set(known))
        if unknown:
            raise ToolValidationError(
                f"{name}: unknown argument(s): {', '.join(unknown)}")

        out: dict[str, Any] = {}
        for p in tool.params:
            if p.name in given and given[p.name] is not None:
                value = given[p.name]
            elif p.required:
                raise ToolValidationError(f"{name}: missing required '{p.name}'")
            else:
                if p.default is None:
                    continue
                value = p.default
            out[p.name] = _coerce(name, p, value)
        return out


_JSON_TYPES = {"int": "integer", "float": "number", "bool": "boolean",
               "list": "array", "dict": "object", "object": "object"}


def _json_schema(p: Param) -> dict[str, Any]:
    out: dict[str, Any] = {"type": _JSON_TYPES.get(p.type, "string")}
    if p.description:
        out["description"] = p.description
    if p.choices:
        out["enum"] = list(p.choices)
    if p.default is not None:
        out["default"] = p.default
    return out


def _coerce(tool: str, p: Param, value: Any) -> Any:
    try:
        if p.type == "int":
            if isinstance(value, bool):
                raise ValueError("bool is not int")
            value = int(value)
        elif p.type == "float":
            value = float(value)
        elif p.type == "bool":
            if isinstance(value, str):
                value = value.strip().lower() in ("1", "true", "yes", "on")
            else:
                value = bool(value)
        elif p.type == "list":
            if isinstance(value, str):
                value = [value]
            elif not isinstance(value, (list, tuple)):
                raise ValueError("not a list")
            value = list(value)
        elif p.type in ("dict", "object"):
            if not isinstance(value, dict):
                raise ValueError("not an object")
            value = dict(value)
        else:  # str
            value = str(value)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(
            f"{tool}: '{p.name}' must be {p.type} ({exc})") from exc
    if p.choices and value not in p.choices:
        raise ToolValidationError(
            f"{tool}: '{p.name}' must be one of {', '.join(p.choices)}")
    return value
