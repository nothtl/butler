"""Phase 7 / semantic refactor: the structured request schema.

Single source of truth for what a semantic interpreter (DeepSeek or the
deterministic fallback) may emit. The schema is *derived* from the typed
:mod:`butler.agent.semantic` contract, so the prompt, the validation and the
runtime can never drift apart.

Two consumers:

* :func:`request_json_schema` — a JSON-Schema object sent to the model as the
  response format (``response_format={"type": "json_schema", ...}``) or embedded
  in the system prompt when only JSON mode is available.
* :func:`schema_prompt` — a compact human-readable rendering for prompts.

Nothing here executes anything; it only describes the contract.
"""

from __future__ import annotations

import json
from typing import Any

from .semantic import (
    ActionKind, AmbiguityKind, ConstraintKind, ConstraintSource, EntityType,
    Hardness, RequestIntent, ResultStatus, ScopeKind, TemporalResolution,
)

#: Top-level fields a model may emit. Anything else is rejected by
#: ``AgentRequest.from_dict(strict=True)``.
ALLOWED_REQUEST_FIELDS = (
    "intent", "action", "target", "entities", "scope", "constraints",
    "preferences", "temporal", "confidence", "ambiguity",
    "requires_confirmation", "parameters", "raw_text", "source",
)

_ENTITY_FIELDS = ("type", "name", "id", "resolved", "confidence", "source",
                  "candidates")
_TEMPORAL_FIELDS = ("phrase", "start", "end", "timezone", "resolution",
                    "confidence", "all_day", "source")
_SCOPE_FIELDS = ("kind", "start", "end", "timezone")
_CONSTRAINT_FIELDS = ("kind", "hardness", "source", "label", "start", "end",
                      "value", "confidence", "note")
_AMBIGUITY_FIELDS = ("kind", "field_name", "mention", "candidates", "reason")


def _enum_values(cls: type) -> list[str]:
    return [e.value for e in cls]


def request_json_schema() -> dict[str, Any]:
    """A JSON-Schema description of the semantic request."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "confidence"],
        "properties": {
            "intent": {"type": "string",
                       "enum": _enum_values(RequestIntent)},
            "action": {"type": "string", "enum": _enum_values(ActionKind)},
            "target": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string",
                             "enum": _enum_values(EntityType)},
                    "name": {"type": "string"},
                    # id/resolved are accepted for compatibility but are
                    # ALWAYS re-resolved server-side and never trusted.
                    "id": {"type": "string"},
                    "resolved": {"type": "boolean"},
                    "confidence": {"type": "number",
                                   "minimum": 0, "maximum": 1},
                    "source": {"type": "string"},
                },
            },
            "entities": {"type": "array", "items": {"type": "object"}},
            "scope": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string",
                             "enum": _enum_values(ScopeKind)},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "timezone": {"type": "string"},
                },
            },
            "constraints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": _enum_values(ConstraintKind)},
                        "hardness": {"type": "string",
                                     "enum": _enum_values(Hardness)},
                        "source": {"type": "string",
                                   "enum": _enum_values(ConstraintSource)},
                        "label": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                        "value": {},
                        "confidence": {"type": "number"},
                        "note": {"type": "string"},
                    },
                },
            },
            "preferences": {"type": "array", "items": {"type": "string"}},
            "temporal": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "phrase": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "timezone": {"type": "string"},
                    "resolution": {"type": "string",
                                   "enum": _enum_values(TemporalResolution)},
                    "confidence": {"type": "number"},
                    "all_day": {"type": "boolean"},
                    "source": {"type": "string"},
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "ambiguity": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {"type": "string",
                                 "enum": _enum_values(AmbiguityKind)},
                        "field_name": {"type": "string"},
                        "mention": {"type": "string"},
                        "candidates": {"type": "array"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "requires_confirmation": {"type": "boolean"},
            "parameters": {"type": "object"},
            "raw_text": {"type": "string"},
            "source": {"type": "string"},
        },
    }


def schema_prompt() -> str:
    """A compact, model-friendly rendering of the contract."""
    intents = _enum_values(RequestIntent)
    actions = _enum_values(ActionKind)
    scopes = _enum_values(ScopeKind)
    temporals = _enum_values(TemporalResolution)
    hardness = _enum_values(Hardness)
    sources = _enum_values(ConstraintSource)
    entities = _enum_values(EntityType)
    ckinds = _enum_values(ConstraintKind)
    lines = [
        "Return ONE JSON object with these keys:",
        "- intent: one of " + str(intents),
        "- action: one of " + str(actions),
        "- target: {type, name} where type is one of " + str(entities)
        + " (use unknown if unsure) and name is the user's words; "
        "NEVER invent id, the server resolves names",
        "- entities: optional list of the same shape",
        "- scope: {kind: one of " + str(scopes) + "}",
        "- temporal: {phrase, resolution: one of " + str(temporals)
        + "} — leave phrase; the server resolves the clock",
        "- constraints: [{kind, hardness, source}] where kind is one of "
        + str(ckinds) + ", hardness is "
        + str(hardness) + " and source is " + str(sources)
        + "; include a constraint ONLY when the user states an explicit "
        "boundary, otherwise use []; an inferred source may NEVER be hard",
        "- preferences: optional string list",
        "- confidence: number in [0,1]",
        "- requires_confirmation: boolean",
        "- parameters: optional action-specific object (e.g. tracker "
        "condition, setting name/value, link relation)",
        "Output ONLY the JSON object. No prose, no markdown, no code fences. "
        "If unsure, use action=unknown and a low confidence.",
        "",
        "DISAMBIGUATION EXAMPLES (pick the specific action, never a generic one):",
        '- "What\'s my CS188 HW4 status?" -> status (a QUERY, not create)',
        '- "Add CS188 HW4." -> create_item (CREATE)',
        '- "Change HW4 to Sunday." -> update_item (UPDATE, not create)',
        '- "Did HW4 move?" -> status (QUERY)',
        '- "Track HW4 deadline changes." -> tracker_create',
        '- "What assignments are in CS188?" -> status (QUERY, not tracker_create)',
        '- "Keep me updated when new CS188 homework appears." -> tracker_create',
        '- "Don\'t schedule after 10 PM." -> settings_update, scope=global, '
        'parameters={"setting":"work_end","value":"22:00"}',
        '- "Stop scheduling in this topic." -> settings_update, '
        'scope=current_topic, parameters={"capability":"scheduling",'
        '"state":"disabled"}',
        '- "Don\'t schedule this tonight." -> defer (a specific task, not a setting)',
        '- "Turn off proactive messages here." -> settings_update, '
        'scope=current_topic, parameters={"capability":"proactive",'
        '"state":"disabled"}',
        '- "Link this to CS188." -> link_items (LINK, not create)',
        '- "Create a project for X." -> create_item',
        "If an object already exists, prefer update_item/link_items over a "
        "duplicate create_item. Use scope=current_topic for 'here/this topic' "
        "and scope=global for 'globally/everywhere/system'.",
    ]
    return "\n".join(lines)


# Context -> relevant action names. This is a *bounded hint* to reduce tool
# confusion; the full enum above is still authoritative and every proposal is
# validated server-side against the complete registry.
_TOPIC_ACTIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("food", "meal", "pantry", "grocer", "cook", "recipe", "dinner"),
     ("recommend", "create_item", "link_items", "tracker_create", "status",
      "settings_update")),
    (("course", "class", "homework", "assignment", "cs1", "cs2", "school",
      "university"),
     ("status", "create_item", "update_item", "tracker_create", "plan_day",
      "find_best_slot", "web_research")),
    (("project", "repo", "startup", "research", "thesis"),
     ("project_status", "project_next", "create_item", "update_item",
      "tracker_create", "plan_week")),
    (("club", "team", "society", "basketball", "practice"),
     ("tracker_create", "status", "create_item", "plan_week")),
)


def relevant_actions(topic: Any = None) -> list[str]:
    """A bounded, topic-relevant action hint (never the whole universe)."""
    text = ""
    if isinstance(topic, dict):
        text = " ".join(str(topic.get(k, "")) for k in
                        ("topic_name", "purpose", "name")).lower()
    else:
        text = str(topic or "").lower()
    for keys, acts in _TOPIC_ACTIONS:
        if any(k in text for k in keys):
            return list(acts)
    return ["status", "recommend", "create_item", "link_items", "update_item",
            "tracker_create", "plan_day", "settings_update", "memory_learn"]



def strict_tool_schema() -> dict[str, Any]:
    """A flat, strict-mode-compatible schema for DeepSeek tool calling.

    DeepSeek strict mode requires every object property to be ``required`` and
    ``additionalProperties=false``. Action-specific slots are therefore passed
    as a JSON *string* (``parameters_json``) and parsed server-side.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["intent", "action", "target_name", "target_type",
                     "scope", "parameters_json", "confidence",
                     "requires_confirmation"],
        "properties": {
            "intent": {"type": "string",
                       "enum": _enum_values(RequestIntent)},
            "action": {"type": "string", "enum": _enum_values(ActionKind)},
            "target_name": {"type": "string",
                            "description": "The user's words for the target; "
                                           "empty if none. Never an id."},
            "target_type": {"type": "string",
                            "enum": _enum_values(EntityType)},
            "scope": {"type": "string",
                      "enum": ["current_topic", "global", "unknown"]},
            "parameters_json": {
                "type": "string",
                "description": "A JSON object string of action-specific "
                               "slots (e.g. {\"capability\":\"web\"}), "
                               "or {} when none."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "requires_confirmation": {"type": "boolean"},
        },
    }


def strict_to_request_dict(flat: dict[str, Any]) -> dict[str, Any]:
    """Map a strict-mode tool payload onto the AgentRequest contract."""
    params: dict[str, Any] = {}
    pj = flat.get("parameters_json")
    if pj:
        try:
            parsed = json.loads(pj) if isinstance(pj, str) else dict(pj)
            if isinstance(parsed, dict):
                params = parsed
        except (TypeError, ValueError):
            params = {}
    target = None
    if str(flat.get("target_name") or "").strip():
        target = {"type": flat.get("target_type", "unknown"),
                  "name": str(flat["target_name"])}
    return {
        "intent": flat.get("intent", "unknown"),
        "action": flat.get("action", "unknown"),
        "confidence": flat.get("confidence", 0.0),
        "requires_confirmation": bool(flat.get("requires_confirmation", False)),
        "target": target,
        "scope": {"kind": flat.get("scope", "unknown")},
        "parameters": params,
        "source": "llm",
    }


__all__ = [
    "ALLOWED_REQUEST_FIELDS",
    "request_json_schema",
    "schema_prompt",
    "strict_tool_schema",
    "strict_to_request_dict",
    "ResultStatus",
]
