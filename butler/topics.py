"""N1: universal topics as shared-context views.

A Telegram forum topic is modelled as a durable :class:`TopicProfile` — a
lightweight *context/view* over Butler's existing domain data, not a separate
routing destination and not a new datastore.

Core principle:

    TOPIC = CONTEXT
    DOMAIN DATA = SOURCE OF TRUTH

There is deliberately **no graph database** and **no universal ontology**. A
topic keeps a small set of capability flags and a handful of lightweight
``topic_links`` rows that *reference* existing courses/projects/tasks/food by id.
Nothing is copied; two topics that reference the same project point at the same
record.

This module is the deterministic domain layer (storage, interpretation,
matching, rendering). The Telegram I/O (sending/pinning messages) stays in
``butler/telebot.py`` and renders from here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("butler.topics")

# ---------------------------------------------------------------------------
# status + capabilities
# ---------------------------------------------------------------------------
PENDING_SETUP = "pending_setup"
ACTIVE = "active"
PAUSED = "paused"
ARCHIVED = "archived"
STATUSES: tuple[str, ...] = (PENDING_SETUP, ACTIVE, PAUSED, ARCHIVED)

# Capability ids and their user-facing labels.
CAPABILITIES: tuple[str, ...] = (
    "knowledge", "tracking", "memory", "planning", "scheduling",
    "reminders", "proactive", "web", "file_organization",
)
CAP_LABELS: dict[str, str] = {
    "knowledge": "Know",
    "tracking": "Track",
    "memory": "Remember",
    "planning": "Plan",
    "scheduling": "Schedule",
    "reminders": "Remind",
    "proactive": "Proactive",
    "web": "Web",
    "file_organization": "Files",
}
CAP_STATES: tuple[str, ...] = (
    "enabled", "disabled", "paused", "degraded", "not_applicable",
)
CAP_ICONS: dict[str, str] = {
    "enabled": "✅", "disabled": "❌", "paused": "⏸", "degraded": "⚠️",
    "not_applicable": "➖",
}
# Capabilities on by default for a new topic (unless the description says
# otherwise). ``tracking``/``web``/``file_organization``/``reminders`` are
# opt-in from the description so we never assume.
DEFAULT_CAPS: dict[str, str] = {
    "knowledge": "enabled",
    "tracking": "disabled",
    "memory": "enabled",
    "planning": "enabled",
    "scheduling": "enabled",
    "reminders": "disabled",
    "proactive": "enabled",
    "web": "disabled",
    "file_organization": "disabled",
}

# Keyword -> capability that the words imply.
_CAP_HINTS: dict[str, tuple[str, ...]] = {
    "tracking": ("track", "homework", "assignment", "deadline", "due",
                 "grade", "progress", "milestone", "task"),
    "scheduling": ("schedule", "study", "work on", "block", "plan time",
                   "time for"),
    "planning": ("plan", "roadmap", "organise", "organize", "strategy",
                 "break down", "breakdown"),
    "reminders": ("remind", "reminder", "nudge", "alert me"),
    "proactive": ("proactive", "alert", "notify", "warn", "heads up"),
    "web": ("web", "online", "latest", "news", "search the internet",
            "look up", "research"),
    "file_organization": ("file", "folder", "document", "organize files",
                          "organise files", "storage", "archive"),
    "memory": ("remember", "preference", "prefer", "habit"),
    "knowledge": ("know", "knowledge", "notes", "reference"),
}

# Purpose keyword -> friendly label (contextual, not an ontology).
_PURPOSE_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(course|class|lecture|homework|assignment|semester|cs\d+|eecs)\b",
     "Course management"),
    (r"\b(project|milestone|repo|codebase|startup|software)\b",
     "Project work"),
    (r"\b(pantry|grocery|groceries|shopping|meal|cook|recipe|food)\b",
     "Food & meal planning"),
    (r"\b(club|team|society|basketball|soccer|practice|meeting)\b",
     "Club / group coordination"),
    (r"\b(research|paper|thesis|experiment|idea)\b", "Research"),
    (r"\b(travel|trip|flight|itinerary|vacation)\b", "Travel"),
    (r"\b(finance|budget|bill|expense|money|stock)\b", "Finance"),
)

_COURSE_RE = re.compile(r"\b([A-Za-z]{2,6}\s?\d{2,4}[A-Za-z]?)\b")


def _now() -> int:
    return int(time.time())


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
        return ""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------
@dataclass
class TopicProfile:
    id: int = 0
    chat_id: int = 0
    thread_id: int = 0
    name: str = ""
    purpose: str = ""
    description: str = ""
    status: str = PENDING_SETUP
    capabilities: dict[str, str] = field(default_factory=dict)
    created_at: int = 0
    updated_at: int = 0
    last_seen_at: int = 0
    pin_message_id: int = 0
    pin_message_version: int = 0
    pin_content_hash: str = ""
    template: str = ""
    # legacy per-topic settings (kept for backward compatibility)
    routing: str = ""
    push_on: int = 0
    push_time: str = "07:00"
    push_freq: str = "daily"

    def cap(self, capability: str) -> str:
        return self.capabilities.get(capability, "disabled")

    def enabled(self, capability: str) -> bool:
        return self.cap(capability) == "enabled"

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["capabilities"] = dict(self.capabilities)
        return d

    @classmethod
    def from_row(cls, row: Any) -> "TopicProfile":
        caps = _loads(row["capabilities"], None) if "capabilities" in row.keys() \
            else None
        return cls(
            id=int(row["id"]), chat_id=int(row["chat_id"]),
            thread_id=int(row["thread_id"] or 0), name=str(row["topic"] or ""),
            purpose=str(row["purpose"] or "") if "purpose" in row.keys() else "",
            description=str(row["description"] or "")
            if "description" in row.keys() else "",
            status=str(row["status"] or ACTIVE) if "status" in row.keys() else ACTIVE,
            capabilities=caps or dict(DEFAULT_CAPS),
            created_at=int(row["created_at"] or 0)
            if "created_at" in row.keys() else 0,
            updated_at=int(row["updated_ts"] or 0),
            last_seen_at=int(row["last_seen_at"] or 0)
            if "last_seen_at" in row.keys() else 0,
            pin_message_id=int(row["pin_message_id"] or 0)
            if "pin_message_id" in row.keys() else 0,
            pin_message_version=int(row["pin_message_version"] or 0)
            if "pin_message_version" in row.keys() else 0,
            pin_content_hash=str(row["pin_content_hash"] or "")
            if "pin_content_hash" in row.keys() else "",
            template=str(row["template"] or "") if "template" in row.keys() else "",
            routing=str(row["routing"] or ""),
            push_on=int(row["push_on"] or 0),
            push_time=str(row["push_time"] or "07:00"),
            push_freq=str(row["push_freq"] or "daily"),
        )


# ---------------------------------------------------------------------------
# interpretation
# ---------------------------------------------------------------------------
def suggest_capabilities(text: str) -> dict[str, str]:
    """Deterministic capability suggestion from a natural-language purpose."""
    low = _norm(text)
    caps = dict(DEFAULT_CAPS)
    for cap, words in _CAP_HINTS.items():
        if any(w in low for w in words):
            caps[cap] = "enabled"
    # A negative statement disables a capability explicitly.
    for cap, words in _CAP_HINTS.items():
        for w in words:
            if re.search(r"(don'?t|do not|no|stop|disable)\b[^.]{0,30}\b"
                         + re.escape(w), low):
                caps[cap] = "disabled"
    return caps


def purpose_label(text: str) -> str:
    low = _norm(text)
    for pat, label in _PURPOSE_HINTS:
        if re.search(pat, low):
            return label
    # fall back to the first short clause, title-cased
    first = re.split(r"[.!?\n]", (text or "").strip())[0].strip()
    if not first:
        return "General"
    words = first.split()
    return " ".join(words[:6])[:60]


def name_guess(text: str) -> str:
    m = _COURSE_RE.search(text or "")
    if m:
        return m.group(1).strip().upper().replace(" ", "")
    first = re.split(r"[.!?\n,]", (text or "").strip())[0].strip()
    words = first.split()
    return " ".join(words[:4])[:40] if words else ""


def interpret_purpose(text: str) -> dict[str, Any]:
    """Turn a natural-language description into a topic configuration."""
    text = (text or "").strip()
    return {
        "purpose": purpose_label(text),
        "description": text,
        "name_guess": name_guess(text),
        "capabilities": suggest_capabilities(text),
    }


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class TopicStore:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db = getattr(container, "db", None)
        self.audit = getattr(container, "audit", None)

    # ------------------------------------------------------------- lifecycle
    def get(self, chat_id: int, thread_id: int | None) -> TopicProfile | None:
        row = self.db.topic_setting(int(chat_id), int(thread_id or 0))
        return TopicProfile.from_row(row) if row else None

    def ensure(self, chat_id: int, thread_id: int | None,
               name: str = "") -> tuple[TopicProfile, bool]:
        """Return the topic profile, creating a pending one if unknown."""
        existing = self.get(chat_id, thread_id)
        if existing is not None:
            if name and not existing.name:
                self.update(existing, name=name)
                existing = self.get(chat_id, thread_id)
            return existing, False
        prof = TopicProfile(chat_id=int(chat_id), thread_id=int(thread_id or 0),
                            name=name or "", status=PENDING_SETUP,
                            capabilities=dict(DEFAULT_CAPS), created_at=_now(),
                            updated_at=_now(), last_seen_at=_now())
        self._insert(prof)
        self._audit("topic_create", prof, detail={"status": PENDING_SETUP})
        return self.get(chat_id, thread_id), True

    def _insert(self, prof: TopicProfile) -> None:
        self.db.upsert_topic_setting(
            prof.chat_id, prof.thread_id, topic=prof.name,
            routing=prof.routing)
        self.db.execute(
            "UPDATE topic_settings SET purpose=?, description=?, status=?, "
            "capabilities=?, created_at=?, last_seen_at=?, updated_ts=? "
            "WHERE chat_id=? AND thread_id=?",
            (prof.purpose, prof.description, prof.status,
             _dumps(prof.capabilities), prof.created_at, prof.last_seen_at,
             prof.updated_at, prof.chat_id, prof.thread_id))

    def update(self, prof: TopicProfile, **fields: Any) -> TopicProfile:
        allowed = {"name", "purpose", "description", "status", "capabilities",
                   "template", "last_seen_at", "pin_message_id",
                   "pin_message_version", "pin_content_hash", "routing",
                   "push_on", "push_time", "push_freq"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "name":
                sets.append("topic=?")
                params.append(v)
            elif k == "capabilities":
                sets.append("capabilities=?")
                params.append(_dumps(v))
            elif k == "updated_at":
                sets.append("updated_ts=?")
                params.append(v)
            elif k == "last_seen_at":
                sets.append("last_seen_at=?")
                params.append(v)
            else:
                sets.append(f"{k}=?")
                params.append(v)
        sets.append("updated_ts=?")
        params.append(_now())
        params += [prof.chat_id, prof.thread_id]
        self.db.execute(
            f"UPDATE topic_settings SET {', '.join(sets)} "
            "WHERE chat_id=? AND thread_id=?", tuple(params))
        self._audit("topic_update", prof, detail={"fields": sorted(fields)})
        return self.get(prof.chat_id, prof.thread_id)

    def set_capability(self, prof: TopicProfile, capability: str,
                       state: str) -> TopicProfile:
        if capability not in CAPABILITIES or state not in CAP_STATES:
            return prof
        caps = dict(prof.capabilities)
        caps[capability] = state
        return self.update(prof, capabilities=caps)

    def list(self, status: str = "") -> list[TopicProfile]:
        rows = self.db.topic_settings_all()
        out = [TopicProfile.from_row(r) for r in rows]
        if status:
            out = [p for p in out if p.status == status]
        return out

    # ------------------------------------------------------------- links
    def links(self, prof: TopicProfile) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM topic_links WHERE topic_profile_id=? ORDER BY id",
            (prof.id,))
        return [dict(r) for r in rows]

    def add_link(self, prof: TopicProfile, target_type: str, target_id: int = 0,
                 relation: str = "about", confidence: float = 1.0,
                 provenance: str = "user") -> dict[str, Any]:
        self.db.execute(
            "INSERT INTO topic_links(topic_profile_id,target_type,target_id,"
            "relation,confidence,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(topic_profile_id,target_type,"
            "target_id,relation) DO UPDATE SET confidence=excluded.confidence, "
            "provenance=excluded.provenance, updated_at=excluded.updated_at",
            (prof.id, str(target_type), int(target_id), str(relation),
             float(confidence), str(provenance), _now(), _now()))
        self._audit("topic_link", prof,
                    detail={"target_type": target_type, "target_id": target_id,
                            "relation": relation})
        return {"ok": True, "target_type": target_type, "target_id": target_id,
                "relation": relation}

    def remove_link(self, prof: TopicProfile, link_id: int) -> dict[str, Any]:
        self.db.execute("DELETE FROM topic_links WHERE id=? AND topic_profile_id=?",
                        (int(link_id), prof.id))
        return {"ok": True, "removed": int(link_id)}

    # ------------------------------------------------------------- matching
    def resolve_links(self, name: str, purpose: str = ""
                      ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Deterministically match existing data. Returns (links, ambiguous)."""
        text = _norm(f"{name} {purpose}")
        links: list[dict[str, Any]] = []
        ambiguous: list[dict[str, Any]] = []

        # courses by code (exact token) or name
        try:
            courses = [dict(r) for r in self.db.courses()]
        except Exception:  # noqa: BLE001
            courses = []
        for c in courses:
            code = str(c.get("code") or "")
            cname = str(c.get("name") or "")
            if not code:
                continue
            if re.search(r"\b" + re.escape(code.lower()) + r"\b", text) \
                    or (cname and _norm(cname) in text):
                links.append({"target_type": "course", "target_id": int(c["id"]),
                              "relation": "about", "confidence": 0.95,
                              "provenance": "name_match"})

        # projects by exact/normalized name (ambiguous if several)
        try:
            projects = list(self.container.projects.list_projects())
        except Exception:  # noqa: BLE001
            projects = []
        matches = []
        for p in projects:
            pname = _norm(str(p.get("name") or ""))
            if not pname:
                continue
            if pname == _norm(name) or pname in text or _norm(name) in pname:
                matches.append(p)
        if len(matches) == 1:
            links.append({"target_type": "project",
                          "target_id": int(matches[0]["id"]),
                          "relation": "about", "confidence": 0.85,
                          "provenance": "name_match"})
        elif len(matches) > 1:
            ambiguous.append({"target_type": "project",
                              "candidates": [{"id": int(m["id"]),
                                              "name": m["name"]}
                                             for m in matches[:5]]})

        # food / pantry / meal plan are contextual references (shared data)
        if re.search(r"\b(food|pantry|grocery|groceries|shopping|meals?|cook|"
                     r"recipe)\b", text):
            links.append({"target_type": "food", "target_id": 0,
                          "relation": "uses_pantry", "confidence": 0.7,
                          "provenance": "keyword_match"})
            if re.search(r"\b(meals?|recipe|cook)\b", text):
                links.append({"target_type": "meal_plan", "target_id": 0,
                              "relation": "uses_meal_plan", "confidence": 0.7,
                              "provenance": "keyword_match"})
        return links, ambiguous

    # ------------------------------------------------------------- data view
    def linked_data(self, prof: TopicProfile) -> dict[str, Any]:
        """Resolve the referenced domain data (read-only) for context/panels."""
        data: dict[str, Any] = {"courses": [], "projects": [], "tasks": [],
                                "food": None, "counts": {}, "storage": ""}
        for link in self.links(prof):
            tt = str(link["target_type"])
            tid = int(link["target_id"] or 0)
            if tt == "course":
                row = self.db.course_by_id(tid) if tid else None
                if row is None:
                    continue
                code = str(row["code"])
                docs = self.db.course_documents(tid)
                data["courses"].append({
                    "id": tid, "code": code, "name": str(row["name"] or ""),
                    "documents": len(docs)})
                data["counts"]["documents"] = data["counts"].get("documents", 0) \
                    + len(docs)
                try:
                    cprojs = [p for p in self.container.projects.list_projects()
                              if int(p.get("course_id") or 0) == tid]
                except Exception:  # noqa: BLE001
                    cprojs = []
                data["counts"]["projects"] = data["counts"].get("projects", 0) \
                    + len(cprojs)
                try:
                    ctasks = [dict(t) for t in self.db.tasks("active")
                              if code.lower() in str(t["title"]).lower()]
                    data["tasks"] += ctasks
                    data["counts"]["assignments"] = \
                        data["counts"].get("assignments", 0) + len(ctasks)
                except Exception:  # noqa: BLE001
                    pass
                data["storage"] = self._course_storage(code)
            elif tt == "project":
                got = self.container.projects.get_project(tid) if tid else None
                if got is None:
                    continue
                data["projects"].append({
                    "id": tid, "name": got["name"],
                    "remaining_minutes": got["remaining_minutes"],
                    "risk_level": got["risk_level"]})
            elif tt == "food":
                food = getattr(self.container, "food", None)
                if food is not None and hasattr(food, "all"):
                    try:
                        items = food.all()
                        data["food"] = {"items": len(items),
                                        "expiring": len(food.expiring(3))}
                    except Exception:  # noqa: BLE001
                        pass
            elif tt == "meal_plan":
                data["counts"]["meal_plan"] = 1
        return data

    def _course_storage(self, code: str) -> str:
        cfg = self.cfg
        base = getattr(cfg, "course_dir", "") if cfg is not None else ""
        return f"{base}/{code}" if base and code else ""

    # ------------------------------------------------------------- rendering
    def render_panel(self, prof: TopicProfile) -> tuple[str, str]:
        data = self.linked_data(prof)
        lines = [f"📚 {prof.name or 'Topic'}",
                 "━━━━━━━━━━━━━━━━", "Purpose",
                 prof.purpose or prof.description or "(not set)"]
        if prof.description and prof.purpose \
                and prof.description.strip() != prof.purpose.strip():
            lines += ["", prof.description.strip()[:300]]
        lines += ["", "Butler"]
        for cap in CAPABILITIES:
            state = prof.cap(cap)
            if state == "not_applicable":
                continue
            icon = CAP_ICONS.get(state, "•")
            lines.append(f"{icon} {CAP_LABELS[cap]}")
        connected = self._connected_lines(prof, data)
        if connected:
            lines += ["", "Connected"] + connected
        current = self._current_lines(data)
        if current:
            lines += ["", "Current"] + current
        if data.get("storage"):
            lines += ["", "Storage", f"📁 {data['storage']}"]
        lines += ["", "Last updated",
                  datetime.fromtimestamp(prof.updated_at or _now())
                  .strftime("%d %b · %H:%M")]
        text = "\n".join(lines)
        return text, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

    def _connected_lines(self, prof: TopicProfile, data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for c in data.get("courses", []):
            out.append(f"• {c['code']} (course)")
        for p in data.get("projects", []):
            out.append(f"• {p['name']} (project)")
        counts = data.get("counts", {})
        if counts.get("assignments"):
            out.append(f"• {counts['assignments']} assignment(s)")
        if counts.get("documents"):
            out.append(f"• {counts['documents']} document(s)")
        if data.get("food"):
            out.append(f"• pantry ({data['food']['items']} item(s))")
        if counts.get("meal_plan"):
            out.append("• meal plan")
        return out

    def _current_lines(self, data: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for t in data.get("tasks", [])[:3]:
            dl = int(t["deadline"] or 0)
            when = f" (due {datetime.fromtimestamp(dl).strftime('%a %H:%M')})" \
                if dl else ""
            out.append(f"• {t['title']}{when}")
        for p in data.get("projects", [])[:2]:
            out.append(f"• {p['name']}: {p['remaining_minutes']}m remaining "
                       f"({p.get('risk_level') or 'unknown'} risk)")
        return out

    def connections_text(self, prof: TopicProfile) -> str:
        data = self.linked_data(prof)
        lines = ["🔗 Connections", ""]
        for c in data.get("courses", []):
            lines.append(f"{c['code']}")
            lines.append("• Course")
            if data.get("counts", {}).get("assignments"):
                lines.append(f"• {data['counts']['assignments']} assignment(s)")
            if data.get("counts", {}).get("documents"):
                lines.append(f"• {data['counts']['documents']} document(s)")
        for p in data.get("projects", []):
            lines.append(f"• Project: {p['name']}")
        if data.get("food"):
            lines.append("• Pantry (shared food data)")
        if data.get("counts", {}).get("meal_plan"):
            lines.append("• Meal plan (shared)")
        if len(lines) == 2:
            lines.append("Nothing connected yet.")
        return "\n".join(lines)

    def details_text(self, prof: TopicProfile) -> str:
        lines = [f"📋 {prof.name or 'Topic'} — details", "",
                 f"Purpose: {prof.purpose or '(not set)'}",
                 f"Status: {prof.status}",
                 f"Created: "
                 f"{datetime.fromtimestamp(prof.created_at or _now()).strftime('%Y-%m-%d')}",
                 f"Updated: "
                 f"{datetime.fromtimestamp(prof.updated_at or _now()).strftime('%Y-%m-%d %H:%M')}",
                 "", "Capabilities:"]
        for cap in CAPABILITIES:
            state = prof.cap(cap)
            lines.append(f"  {CAP_ICONS.get(state, '•')} {CAP_LABELS[cap]}: "
                         f"{state}")
        lines += ["", "Provenance:",
                  f"  • {self.why_enabled(prof, 'tracking')}"]
        return "\n".join(lines)

    def why_enabled(self, prof: TopicProfile, capability: str) -> str:
        if prof.enabled(capability):
            if prof.description:
                return (f"{CAP_LABELS.get(capability, capability)} is enabled "
                        f"because you described this topic as: "
                        f"\"{prof.description[:80]}\".")
            return (f"{CAP_LABELS.get(capability, capability)} is enabled by "
                    f"default for this topic.")
        return f"{CAP_LABELS.get(capability, capability)} is disabled for this topic."

    def settings_text(self, prof: TopicProfile) -> str:
        lines = [f"⚙️ Settings — {prof.name or 'Topic'}", "",
                 f"Purpose: {prof.purpose or '(not set)'}", "",
                 "Capabilities (tap to toggle):"]
        for cap in CAPABILITIES:
            lines.append(f"  {CAP_ICONS.get(prof.cap(cap), '•')} "
                         f"{CAP_LABELS[cap]}")
        lines += ["", "Notifications: "
                  f"{'on' if prof.push_on else 'off'} · {prof.push_time} · "
                  f"{prof.push_freq}",
                  "Memory scope: this topic + global"]
        return "\n".join(lines)

    def storage_text(self, prof: TopicProfile) -> str:
        data = self.linked_data(prof)
        path = data.get("storage")
        if not path:
            return "📁 No storage is connected to this topic yet."
        files = data.get("counts", {}).get("documents", 0)
        return f"📁 {path}\n{files} connected file(s)."

    # ------------------------------------------------------------- templates
    def to_template(self, prof: TopicProfile) -> dict[str, Any]:
        return {"purpose": prof.purpose, "capabilities": dict(prof.capabilities),
                "push_on": prof.push_on, "push_time": prof.push_time,
                "push_freq": prof.push_freq, "template": prof.name}

    def apply_template(self, prof: TopicProfile,
                       template: dict[str, Any]) -> TopicProfile:
        return self.update(
            prof,
            capabilities=dict(template.get("capabilities") or DEFAULT_CAPS),
            push_on=int(template.get("push_on") or 0),
            push_time=str(template.get("push_time") or "07:00"),
            push_freq=str(template.get("push_freq") or "daily"),
            template=str(template.get("template") or ""))

    # ------------------------------------------------------------- context
    def context_text(self, prof: TopicProfile) -> str:
        """A compact, bounded summary injected into chat as relevance context."""
        if prof.status != ACTIVE:
            return ""
        data = self.linked_data(prof)
        bits = [f"Current Telegram topic: {prof.name or 'topic'}",
                f"Topic purpose: {prof.purpose or 'general'}"]
        caps = [CAP_LABELS[c] for c in CAPABILITIES if prof.enabled(c)]
        if caps:
            bits.append("Enabled here: " + ", ".join(caps))
        for c in data.get("courses", [])[:3]:
            bits.append(f"Linked course: {c['code']}")
        for p in data.get("projects", [])[:3]:
            bits.append(f"Linked project: {p['name']}")
        if data.get("food"):
            bits.append("Linked: pantry/food")
        bits.append("Topic context is a relevance hint, not a hard boundary; "
                    "answer global questions across all data when asked.")
        return "\n".join(bits)

    # ------------------------------------------------------------- audit
    def _audit(self, action: str, prof: TopicProfile,
               detail: Any = None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action, actor="topics",
                              target=f"topic:{prof.chat_id}:{prof.thread_id}",
                              decision="allowed", outcome="ok",
                              detail=detail or {})
        except Exception:  # noqa: BLE001
            log.debug("topic audit failed", exc_info=True)
