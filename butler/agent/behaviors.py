"""Q13: generic persistent topic behaviors.

A *TopicBehavior* changes how Butler responds to **future** user requests in a
topic. It is deliberately distinct from the other durable concepts:

* a **Tracker** monitors external state over time and emits events;
* **Memory** stores facts;
* a **TopicBehavior** is a standing instruction ("when I ask about X, also do
  Y") that biases how a future request is handled.

There is exactly one generic class — no FoodBehavior / CourseBehavior. A
behavior is ``trigger + strategy + constraints + scope + persistence``:

    trigger:     "meal recommendation"
    strategy:    {"use_web": true}
    constraints: {"fit": "pantry"}
    scope:       "Food topic"
    persistence: "always" | "once"

Behaviors may *shape* a request (e.g. add a web search) but can never bypass
safety, confirmation, authorization, provider health or the tool registry:
application only merges non-destructive parameters and is validated by the same
deterministic layer as any other request. Precedence (highest first):

    explicit current-turn instruction
    → active conversation task
    → topic behavior
    → topic defaults
    → global preferences
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

#: strategies/params a behavior may contribute; all are read-only shaping flags.
#: Anything not in this set is ignored (fail closed toward safety).
ALLOWED_STRATEGY_KEYS = frozenset({
    "use_web", "source_preference", "prefer_pantry_compatible",
    "include_context", "tone", "detail_level",
})

#: keys that must never be contributed by a persistent behavior.
FORBIDDEN_STRATEGY_KEYS = frozenset({
    "execute", "action", "target", "tool", "command", "delete", "send",
    "write", "confirm", "bypass", "override_safety",
})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", _norm(text)) if len(t) > 2}


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{}"


@dataclass
class TopicBehavior:
    id: int = 0
    topic_profile_id: int = 0
    trigger: str = ""
    strategy: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    scope: str = ""
    persistence: str = "always"
    enabled: int = 1
    priority: int = 0
    created_from: str = ""
    created_at: int = 0
    updated_at: int = 0

    def sanitized_strategy(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in (self.strategy or {}).items():
            key = str(k).strip().lower()
            if key in FORBIDDEN_STRATEGY_KEYS:
                continue
            if key in ALLOWED_STRATEGY_KEYS:
                out[key] = v
        return out

    def matches(self, text: str) -> bool:
        trig = _tokens(self.trigger)
        if not trig:
            return False
        return bool(trig & _tokens(text))

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["strategy"] = dict(self.strategy or {})
        d["constraints"] = dict(self.constraints or {})
        return d

    @classmethod
    def from_row(cls, row: Any) -> "TopicBehavior":
        return cls(
            id=int(row["id"]),
            topic_profile_id=int(row["topic_profile_id"] or 0),
            trigger=str(row["trigger"] or ""),
            strategy=_loads(row["strategy"], {}),
            constraints=_loads(row["constraints"], {}),
            scope=str(row["scope"] or ""),
            persistence=str(row["persistence"] or "always"),
            enabled=int(row["enabled"] or 0),
            priority=int(row["priority"] or 0),
            created_from=str(row["created_from"] or ""),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
        )


class TopicBehaviorStore:
    """Durable, generic behaviors attached to a TopicProfile."""

    def __init__(self, container: Any):
        self.container = container
        self.db = getattr(container, "db", None)

    # ------------------------------------------------------------- writing
    def add(self, topic_profile_id: int, *, trigger: str,
            strategy: dict[str, Any] | None = None,
            constraints: dict[str, Any] | None = None,
            scope: str = "", persistence: str = "always",
            priority: int = 0, created_from: str = "user") -> TopicBehavior:
        now = int(time.time())
        behavior = TopicBehavior(
            topic_profile_id=int(topic_profile_id),
            trigger=str(trigger or "").strip(),
            strategy=dict(strategy or {}),
            constraints=dict(constraints or {}),
            scope=str(scope or ""),
            persistence=str(persistence or "always"),
            enabled=1, priority=int(priority or 0),
            created_from=str(created_from or "user"),
            created_at=now, updated_at=now)
        if self.db is None:
            return behavior
        cur = self.db.execute(
            "INSERT INTO topic_behaviors(topic_profile_id,trigger,strategy,"
            "constraints,scope,persistence,enabled,priority,created_from,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (behavior.topic_profile_id, behavior.trigger,
             _dumps(behavior.strategy), _dumps(behavior.constraints),
             behavior.scope, behavior.persistence, behavior.enabled,
             behavior.priority, behavior.created_from, now, now))
        behavior.id = int(getattr(cur, "lastrowid", 0) or 0)
        return behavior

    def set_enabled(self, behavior_id: int, enabled: bool) -> bool:
        if self.db is None:
            return False
        self.db.execute("UPDATE topic_behaviors SET enabled=?, updated_at=? "
                        "WHERE id=?", (1 if enabled else 0, int(time.time()),
                                       int(behavior_id)))
        return True

    def remove(self, behavior_id: int) -> bool:
        if self.db is None:
            return False
        self.db.execute("DELETE FROM topic_behaviors WHERE id=?",
                        (int(behavior_id),))
        return True

    # ------------------------------------------------------------- reading
    def list(self, topic_profile_id: int, *,
             enabled_only: bool = True) -> list[TopicBehavior]:
        if self.db is None:
            return []
        sql = ("SELECT * FROM topic_behaviors WHERE topic_profile_id=?"
               + (" AND enabled=1" if enabled_only else "")
               + " ORDER BY priority DESC, id")
        return [TopicBehavior.from_row(r)
                for r in self.db.query(sql, (int(topic_profile_id),))]

    def match(self, topic_profile_id: int, text: str) -> list[TopicBehavior]:
        """Enabled behaviors whose trigger shares meaning with the request.

        Matching is token overlap (generic, domain-free); the model proposes
        the trigger at creation time and the server re-checks it here.
        """
        return [b for b in self.list(topic_profile_id) if b.matches(text)]

    def select_semantic(self, chat: Any, topic_profile_id: int, text: str
                        ) -> list[TopicBehavior]:
        """Ask the model which stored behaviors apply to a request.

        Used only when deterministic matching finds nothing, so paraphrases
        ("dinner" vs a "food request" trigger) still resolve. The server keeps
        authority: the returned ids are re-checked against the stored rows.
        """
        items = self.list(topic_profile_id)
        if not items or chat is None \
                or not getattr(chat, "_llm_ready", lambda: False)():
            return []
        catalog = [{"id": b.id, "trigger": b.trigger} for b in items]
        system = (
            "Select which stored topic behaviors apply to the user's request. "
            "A behavior applies when the request matches its trigger in "
            "MEANING, not exact words. Reply ONLY JSON {\"ids\":[<int>...]}. "
            "If none apply, return {\"ids\":[]}.")
        try:
            out = chat.complete(system, json.dumps(
                {"request": text, "behaviors": catalog}), json_mode=True)
            ids = json.loads(out or "{}").get("ids") or []
            wanted = {int(i) for i in ids if str(i).lstrip("-").isdigit()}
            return [b for b in items if b.id in wanted]
        except Exception:  # noqa: BLE001 — semantic match is best effort
            return []

    def apply(self, topic_profile_id: int, text: str, parameters: dict[str, Any],
              *, overridden: bool = False) -> dict[str, Any]:
        """Merge matching behavior strategy into request parameters.

        A current-turn override (``overridden=True``) wins over every behavior:
        the merged result only fills keys the user did not set this turn.
        """
        if overridden:
            return dict(parameters or {})
        out = dict(parameters or {})
        for behavior in self.match(topic_profile_id, text):
            for key, value in behavior.sanitized_strategy().items():
                out.setdefault(key, value)
            if behavior.constraints:
                out.setdefault("behavior_constraints", dict(behavior.constraints))
            out.setdefault("behavior_applied", []).append(
                {"id": behavior.id, "trigger": behavior.trigger})
        return out

    def describe(self, topic_profile_id: int) -> str:
        """A compact, honest description of stored behaviors (data, not prose)."""
        items = self.list(topic_profile_id)
        if not items:
            return ""
        lines = []
        for b in items:
            bits = []
            strat = b.sanitized_strategy()
            if strat.get("use_web"):
                bits.append("search the web")
            if strat.get("source_preference"):
                bits.append(f"prefer {strat['source_preference']}")
            if b.constraints:
                bits.append("constraints: " + ", ".join(
                    f"{k}={v}" for k, v in b.constraints.items()))
            lines.append(f"• when: {b.trigger} → " + ("; ".join(bits) or "adjust"))
        return "\n".join(lines)
