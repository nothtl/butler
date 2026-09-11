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
    ]
    return "\n".join(lines)


__all__ = [
    "ALLOWED_REQUEST_FIELDS",
    "request_json_schema",
    "schema_prompt",
    "ResultStatus",
]
