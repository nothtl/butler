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
    """A typed capability the agent may invoke."""

    name: str
    description: str
    handler: Handler
    params: list[Param] = field(default_factory=list)
    action: str = ""                  # safety action name (defaults to name)
    side_effect: bool = False
    returns: str = ""

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
            side_effect: bool = False, returns: str = "") -> Tool:
        return self.register(Tool(name=name, description=description,
                                  handler=handler, params=params or [],
                                  action=action, side_effect=side_effect,
                                  returns=returns))

    # ------------------------------------------------------------------ query
    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolNotFound(f"unknown tool: {name}")
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return list(self._tools)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schema(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

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
        else:  # str
            value = str(value)
    except (TypeError, ValueError) as exc:
        raise ToolValidationError(
            f"{tool}: '{p.name}' must be {p.type} ({exc})") from exc
    if p.choices and value not in p.choices:
        raise ToolValidationError(
            f"{tool}: '{p.name}' must be one of {', '.join(p.choices)}")
    return value
