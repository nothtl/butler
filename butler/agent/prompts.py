"""Phase 7 / M1: prompt templates for the agent reasoning step.

The LLM's *only* job here is to choose a registered tool and fill its arguments,
or to phrase a live-state answer. It never executes anything and never invents
facts. The rendered tool schema is injected at call time so the prompt can never
drift from the registry.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = (
    "You are Butler's planning component. You may only choose ONE registered "
    "tool and provide its arguments, or answer from the supplied live state. "
    "Never invent data, never claim an action succeeded, and never bypass "
    "safety. If no tool fits, choose the conversational answer tool. Reply with "
    "a single JSON object: {\"tool\": <name>, \"args\": {...}, \"reason\": "
    "<short>} and nothing else."
)


def render_tools(schema: list[dict[str, Any]]) -> str:
    """Compact, stable tool list for the prompt (names + params + description)."""
    lines = []
    for t in schema:
        params = ", ".join(
            f"{p['name']}:{p['type']}" + ("" if p["required"] else "?")
            for p in t.get("params", [])
        )
        lines.append(f"- {t['name']}({params}): {t.get('description', '')}")
    return "\n".join(lines)


def build_intent_prompt(message: str, schema: list[dict[str, Any]],
                        context_text: str = "") -> tuple[str, str]:
    """Return the ``(system, user)`` pair for tool selection."""
    user = (
        "Registered tools:\n"
        + render_tools(schema)
        + "\n\nLive state:\n"
        + (context_text or "(none)")
        + "\n\nUser message:\n"
        + message
    )
    return SYSTEM_PROMPT, user


def parse_tool_choice(raw: str | None) -> dict[str, Any] | None:
    """Extract ``{"tool", "args", "reason"}`` from a model reply, or None."""
    if not raw:
        return None
    import re
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(obj, dict) or not obj.get("tool"):
        return None
    args = obj.get("args")
    return {"tool": str(obj["tool"]),
            "args": dict(args) if isinstance(args, dict) else {},
            "reason": str(obj.get("reason", ""))}
