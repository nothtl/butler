"""N3: universal creation, linking & organization.

Natural language is the interface; the existing domain services remain the
source of truth. This module decides *what the user wants created, linked or
organized* and then calls the appropriate existing API (courses, projects,
tasks, food, grocery, memory, topics, trackers, files).

It is deliberately **not** a second domain model and **not** a giant ontology:
resolution is against live domain data, aliases are a small bridge table, and
anything without a specialized model is preserved as a note (M6 memory) or an
unresolved reference.

Pipeline:

    user text -> intent/operation -> topic/recent context -> resolution
              -> proposal -> safety/confirmation -> existing domain API
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger("butler.creation")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------
OP_CREATE = "create"
OP_UPDATE = "update"
OP_LINK = "link"
OP_ORGANIZE = "organize"
OP_SCHEDULE = "schedule"
OP_TRACK = "track"
OP_MEMORY = "memory"
OP_RESOLVE = "resolve"

#: Small, human-readable relationship set (no dozens of types).
RELATIONS: tuple[str, ...] = (
    "about", "belongs_to", "part_of", "related_to", "requires", "uses",
    "stored_in", "scheduled_for", "replenishes", "created_for", "derived_from",
)

TARGET_TYPES: tuple[str, ...] = (
    "course", "project", "task", "food", "grocery", "recipe", "event",
    "note", "topic", "memory", "file",
)

STATUS_RESOLVED = "resolved"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNRESOLVED = "unresolved"
STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_LINKED = "linked"
STATUS_PREVIEW = "preview"
STATUS_NEEDS_CONFIRMATION = "needs_confirmation"

_COURSE_RE = re.compile(r"\b([A-Za-z]{2,6})\s?(\d{2,4}[A-Za-z]?)\b")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b", re.I)
_MINS_RE = re.compile(r"(\d+)\s*(?:minutes?|mins?|m)\b", re.I)
_QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:portions?|units?|packs?|bottles?|"
                     r"cans?|kg|g|lb|lbs)?\b", re.I)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")

# Deterministic fallback vocabulary (documented; not an endless regex pile).
_CREATE_WORDS = ("add ", "create ", "new ", "save ", "make ", "start ",
                 "set up ", "put ")
_LINK_WORDS = ("link ", "connect ", "attach ", "associate ", "put this under ",
               "file this under ", "assign ")
_UPDATE_WORDS = ("change ", "update ", "rename ", "edit ", "make it ",
                 "actually ", "set the ", "move ")
_ORGANIZE_WORDS = ("organize", "organise", "sort ", "file these", "archive ",
                   "tidy ")
_SCHEDULE_WORDS = ("schedule ", "block ", "put ", "plan ")
_MEMORY_WORDS = ("remember that", "remember i", "remember this", "remember",
                 "note that i", "i prefer", "keep in mind")


def _now() -> int:
    return int(time.time())


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _norm(text)) if len(t) > 1]


@dataclass
class Reference:
    type: str
    id: int = 0
    name: str = ""
    display: str = ""
    confidence: float = 0.0
    provenance: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Proposal:
    operation: str = OP_CREATE
    target_type: str = ""
    name: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    links: list[dict[str, Any]] = field(default_factory=list)
    resolution: dict[str, Any] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    confidence: float = 0.0
    requires_confirmation: bool = False
    preview: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
class CreationService:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db = getattr(container, "db", None)
        self.audit = getattr(container, "audit", None)

    # ------------------------------------------------------------- resolution
    def _candidates(self, types: list[str] | None = None) -> list[Reference]:
        types = types or list(TARGET_TYPES)
        out: list[Reference] = []

        def add(t: str, i: int, name: str, display: str = "") -> None:
            out.append(Reference(t, int(i), name, display or name))

        if "course" in types:
            try:
                for r in self.db.courses():
                    code = str(r["code"])
                    add("course", r["id"], code,
                        f"{code} {str(r['name'] or '')}".strip())
            except Exception:  # noqa: BLE001
                pass
        if "project" in types:
            try:
                for p in self.container.projects.list_projects():
                    add("project", p["id"], str(p["name"]), str(p["name"]))
            except Exception:  # noqa: BLE001
                pass
        if "task" in types:
            try:
                for r in self.db.tasks("active"):
                    add("task", r["id"], str(r["title"]), str(r["title"]))
            except Exception:  # noqa: BLE001
                pass
        if "food" in types:
            try:
                for r in self.db.food():
                    add("food", r["id"], str(r["name"]), str(r["name"]))
            except Exception:  # noqa: BLE001
                pass
        if "grocery" in types:
            try:
                for r in self.db.shopping(0):
                    add("grocery", r["id"], str(r["name"]), str(r["name"]))
            except Exception:  # noqa: BLE001
                pass
        if "recipe" in types:
            try:
                for r in self.db.recipes():
                    add("recipe", r["id"], str(r["name"]), str(r["name"]))
            except Exception:  # noqa: BLE001
                pass
        # aliases enrich candidates
        try:
            aliases = self.db.query("SELECT * FROM entity_aliases")
            for a in aliases:
                out.append(Reference(str(a["target_type"]), int(a["target_id"]),
                                     str(a["alias"]), str(a["canonical"] or ""),
                                     float(a["confidence"] or 0.85),
                                     "alias"))
        except Exception:  # noqa: BLE001
            pass
        return out

    def _context_boost(self, ref: Reference,
                       context: dict[str, Any] | None) -> float:
        if not context:
            return 0.0
        boost = 0.0
        linked = context.get("linked") or []
        for l in linked:
            if str(l.get("target_type")) == ref.type and int(l.get("target_id") or 0) == ref.id:
                boost += 0.15
        focus = context.get("focus") or {}
        if focus.get("type") == ref.type and int(focus.get("id") or 0) == ref.id:
            boost += 0.15
        topic_name = _norm(str(context.get("topic_name") or ""))
        if topic_name and topic_name in _norm(ref.name):
            boost += 0.05
        return min(0.2, boost)

    def _score(self, query: str, ref: Reference,
               context: dict[str, Any] | None) -> float:
        qn = _norm(query)
        if not qn:
            return 0.0
        cn = _norm(ref.name)
        dn = _norm(ref.display)
        score = 0.0
        if qn == cn or qn == dn:
            score = 1.0
        elif cn and (qn in cn or cn in qn):
            score = 0.7
        elif dn and (qn in dn or dn in qn):
            score = 0.6
        else:
            qt, ct = set(_tokens(qn)), set(_tokens(f"{cn} {dn}"))
            if qt and ct:
                ratio = len(qt & ct) / len(qt)
                if ratio:
                    score = 0.4 * ratio
        if ref.provenance == "alias" and qn == _norm(ref.name):
            score = max(score, 0.9)
        # Context is a tie-breaker only: never let it overturn an exact match.
        if 0 < score < 0.95:
            score = min(1.0, score + self._context_boost(ref, context))
        return round(score, 4)

    def resolve(self, query: str, *, types: list[str] | None = None,
                context: dict[str, Any] | None = None,
                limit: int = 8) -> dict[str, Any]:
        scored: list[Reference] = []
        for ref in self._candidates(types):
            s = self._score(query, ref, context)
            if s >= 0.4:
                ref.score = s
                scored.append(ref)
        # de-duplicate (type,id) keeping the best-scoring name
        best: dict[tuple[str, int], Reference] = {}
        for ref in scored:
            key = (ref.type, ref.id)
            if key not in best or ref.score > best[key].score:
                best[key] = ref
        ranked = sorted(best.values(), key=lambda r: (-r.score, r.type, r.id))
        ranked = ranked[:limit]
        if not ranked:
            return {"status": STATUS_UNRESOLVED, "query": query,
                    "candidates": [], "refs": []}
        top = ranked[0]
        second = ranked[1].score if len(ranked) > 1 else 0.0
        if top.score >= 0.95 and second < 0.95:
            return {"status": STATUS_RESOLVED, "query": query,
                    "ref": top.to_dict(),
                    "candidates": [r.to_dict() for r in ranked]}
        if top.score >= 0.95 and second >= 0.95:
            return {"status": STATUS_AMBIGUOUS, "query": query,
                    "candidates": [r.to_dict() for r in ranked[:5]]}
        if top.score >= 0.85 and (len(ranked) == 1 or top.score - second >= 0.15):
            return {"status": STATUS_RESOLVED, "query": query,
                    "ref": top.to_dict(), "candidates": [r.to_dict() for r in ranked]}
        if len(ranked) > 1 and top.score - ranked[1].score < 0.15:
            return {"status": STATUS_AMBIGUOUS, "query": query,
                    "candidates": [r.to_dict() for r in ranked[:5]]}
        if top.score >= 0.6:
            return {"status": STATUS_RESOLVED, "query": query,
                    "ref": top.to_dict(), "candidates": [r.to_dict() for r in ranked]}
        return {"status": STATUS_UNRESOLVED, "query": query,
                "candidates": [r.to_dict() for r in ranked]}

    # ------------------------------------------------------------- parsing
    def parse(self, text: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = (text or "").strip()
        low = _norm(raw)
        context = context or {}
        prop = Proposal(context=context)

        # operation
        if _match(low, _MEMORY_WORDS) and not _match(low, ("add ", "create ")):
            prop.operation = OP_MEMORY
        elif re.search(r"\b(add|save|put)\s+this\s+to\b", low) or \
                _match(low, _LINK_WORDS) or \
                (re.search(r"\bunder\b", low) and "add" not in low):
            prop.operation = OP_LINK
        elif _match(low, _ORGANIZE_WORDS):
            prop.operation = OP_ORGANIZE
        elif _match(low, _UPDATE_WORDS):
            prop.operation = OP_UPDATE
        elif _match(low, ("track ", "watch ", "tell me when")) or \
                low.startswith("track"):
            prop.operation = OP_TRACK
        elif _match(low, _SCHEDULE_WORDS) and re.search(
                r"\b(hour|hours|h|block|this)\b", low):
            prop.operation = OP_SCHEDULE
        elif _match(low, _CREATE_WORDS):
            prop.operation = OP_CREATE
        else:
            prop.operation = OP_CREATE

        # target type
        prop.target_type = self._target_type(low)
        # "this"/contextual reference is only meaningful for link/update/organize
        if prop.operation in (OP_LINK, OP_UPDATE, OP_ORGANIZE):
            prop.fields["reference"] = self._contextual_reference(low, context)

        # name
        prop.name = self._extract_name(raw, prop)

        # fields
        if prop.target_type == "food":
            prop.fields["quantity"] = self._quantity(low)
        if prop.target_type == "grocery":
            prop.fields["quantity"] = self._quantity(low)
        if prop.target_type == "task":
            prop.fields["est_minutes"] = self._effort(low) or 60
        if prop.target_type == "project":
            prop.fields["estimated_total_minutes"] = self._effort(low) or 0
        if prop.target_type == "course":
            prop.fields["url"] = _URL_RE.search(raw).group(0) if _URL_RE.search(raw) else ""
        prop.fields["deadline"] = self._deadline(low)
        prop.fields["priority"] = self._priority(low)
        if prop.operation == OP_SCHEDULE:
            prop.fields["minutes"] = self._effort(low) or 60
            prop.fields["when"] = self._when(low)
        if prop.target_type == "note":
            prop.fields["text"] = raw

        if prop.operation == OP_UPDATE:
            m = re.search(r"\bto\s+(.+)$", raw, flags=re.I)
            if m and re.search(r"\b(rename|call)\b", low):
                prop.fields["new_name"] = m.group(1).strip()[:60]

        # resolution against existing data
        if prop.name and prop.target_type not in ("note", "memory", "file"):
            types = None if prop.operation == OP_UPDATE else (
                [prop.target_type] if prop.target_type in TARGET_TYPES else None)
            prop.resolution = self.resolve(prop.name, types=types, context=context)
            if prop.resolution.get("status") == STATUS_RESOLVED:
                ref = prop.resolution["ref"]
                if prop.operation == OP_CREATE:
                    prop.operation = OP_UPDATE
                    prop.fields["_existing"] = ref
                if prop.operation == OP_UPDATE:
                    prop.target_type = ref["type"]
        elif prop.fields.get("reference"):
            ref = prop.fields["reference"]
            prop.resolution = {"status": STATUS_RESOLVED, "ref": ref}
        else:
            prop.resolution = {"status": STATUS_UNRESOLVED, "candidates": []}

        # confidence / questions / confirmation
        prop.confidence = self._confidence(prop)
        prop.questions = self._questions(prop)
        prop.requires_confirmation = prop.operation in (OP_SCHEDULE, OP_ORGANIZE) \
            or bool(prop.questions)
        self._build_preview(prop)
        return prop.to_dict()

    def _target_type(self, low: str) -> str:
        if re.search(r"\bremember\b|\bi prefer\b", low):
            return "memory"
        if re.search(r"\bproject\b", low):
            return "project"
        if re.search(r"\b(task|todo|to-?do|finish|report|essay)\b", low) \
                and "project" not in low:
            return "task"
        if re.search(r"\b(pantry|food|fridge|freezer|inventory)\b", low):
            return "food"
        if re.search(r"\b(grocery|groceries|grocer|shopping|buy|pick up)\b", low):
            return "grocery"
        if re.search(r"\b(recipe)\b", low):
            return "recipe"
        if re.search(r"\b(calendar|event|appointment|meeting|reminder)\b", low):
            return "event"
        if re.search(r"\b(note|save this|document)\b", low):
            return "note"
        if re.search(r"\btopic\b", low):
            return "topic"
        if _COURSE_RE.search(low) or re.search(r"\b(course|class)\b", low):
            return "course"
        if re.search(r"\b(file|folder|document)\b", low):
            return "file"
        return "task"

    def _contextual_reference(self, low: str,
                              context: dict[str, Any]) -> dict[str, Any] | None:
        if not re.search(r"\b(this|that|it|these|those)\b", low):
            return None
        focus = context.get("focus")
        if isinstance(focus, dict) and focus.get("type"):
            return focus
        recent = context.get("recent") or []
        if recent:
            return recent[0]
        return {"type": "unknown", "id": 0, "name": "this"}

    def _extract_name(self, raw: str, prop: Proposal) -> str:
        t = raw.strip()
        # strip a leading trigger phrase
        t = re.sub(r"^(please\s+)?(add|create|new|save|make|start|set up|link|"
                   r"connect|attach|associate|assign|change|update|rename|"
                   r"organize|organise|sort|archive|schedule|block|track|"
                   r"watch|remember|note)\b\s*", "", t, flags=re.I)
        t = re.sub(r"^(a|an|the|my|this|that|these|those)\s+", "", t, flags=re.I)
        # cut at field markers
        cut = re.split(r"\s+(?:due|by|that|which|with|around|about|for|to|under|"
                       r"at|on|in|every|until)\b|,", t, maxsplit=1, flags=re.I)[0]
        cut = cut.strip(" .,-")
        if prop.target_type == "course" and prop.operation != OP_UPDATE:
            m = _COURSE_RE.search(cut) or _COURSE_RE.search(raw)
            if m:
                return (m.group(1) + m.group(2)).upper().replace(" ", "")
        if prop.target_type in ("food", "grocery"):
            m = re.search(r"\b([a-z][a-z ]{1,30}?)\s+(?:to|into|in)\b", _norm(t))
            if m:
                cut = m.group(1).strip()
        if prop.target_type in ("project", "task", "course"):
            cut = re.sub(r"\s+(project|task|course)$", "", cut, flags=re.I)
        if prop.operation == OP_UPDATE:
            cut = re.sub(r"\s+(deadline|name|effort|priority|date|time)\b.*$",
                         "", cut, flags=re.I)
        if len(cut.split()) > 8:
            cut = " ".join(cut.split()[:8])
        return cut

    def _quantity(self, low: str) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:portions?|units?|packs?|bottles?|"
                      r"cans?|kg|g|lb|lbs)?\b", low)
        if m:
            return float(m.group(1))
        return 1.0

    def _effort(self, low: str) -> int:
        m = _HOURS_RE.search(low)
        if m:
            return int(round(float(m.group(1)) * 60))
        m = _MINS_RE.search(low)
        if m:
            return int(m.group(1))
        return 0

    def _priority(self, low: str) -> int:
        if re.search(r"\b(critical|urgent|asap)", low):
            return 5
        if re.search(r"\b(important|high priority)", low):
            return 4
        if re.search(r"\b(low priority|whenever)", low):
            return 2
        return 3

    def _deadline(self, low: str) -> int:
        now = datetime.now()
        base = datetime(now.year, now.month, now.day, 23, 59)
        for i, wd in enumerate(_WEEKDAYS):
            if re.search(rf"\b(?:due\s+|by\s+|on\s+|next\s+)?{wd}\b", low):
                delta = (i - now.weekday()) % 7
                if delta == 0:
                    delta = 7
                return int((base + timedelta(days=delta)).timestamp())
        if "tomorrow" in low:
            return int((base + timedelta(days=1)).timestamp())
        if "tonight" in low or "today" in low:
            return int(base.timestamp())
        m = re.search(r"\bin\s+(\d+)\s+(day|week)s?\b", low)
        if m:
            n = int(m.group(1)) * (7 if m.group(2) == "week" else 1)
            return int((base + timedelta(days=n)).timestamp())
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
        if m:
            try:
                return int(datetime(int(m.group(1)), int(m.group(2)),
                                    int(m.group(3)), 23, 59).timestamp())
            except ValueError:
                return 0
        return 0

    def _when(self, low: str) -> str:
        for w in ("tonight", "tomorrow", "today", "afternoon", "morning",
                  "evening"):
            if w in low:
                return w
        return ""

    def _confidence(self, prop: Proposal) -> float:
        if prop.questions:
            return 0.5
        if prop.resolution.get("status") == STATUS_RESOLVED:
            return 0.9
        if prop.operation in (OP_CREATE, OP_MEMORY, OP_TRACK):
            return 0.85 if prop.name else 0.4
        return 0.7

    def _questions(self, prop: Proposal) -> list[str]:
        q: list[str] = []
        if prop.resolution.get("status") == STATUS_AMBIGUOUS:
            names = ", ".join(c["display"] for c in prop.resolution["candidates"][:4])
            q.append(f"Which one — {names}?")
        if prop.operation == OP_LINK and not prop.name:
            q.append("What should I link it to?")
        if prop.operation in (OP_CREATE, OP_UPDATE) and not prop.name \
                and prop.target_type not in ("note", "memory"):
            q.append("What should I call it?")
        return q

    def _build_preview(self, prop: Proposal) -> None:
        lines = [f"{prop.operation.title()} {prop.target_type}: {prop.name or '(this)'}"]
        f = prop.fields
        if f.get("deadline"):
            lines.append("• deadline: " + datetime.fromtimestamp(
                int(f["deadline"])).strftime("%a %H:%M"))
        if f.get("est_minutes"):
            lines.append(f"• effort: {int(f['est_minutes'])}m")
        if f.get("estimated_total_minutes"):
            lines.append(f"• effort: {int(f['estimated_total_minutes'])}m")
        if f.get("quantity") and prop.target_type in ("food", "grocery"):
            lines.append(f"• quantity: {f['quantity']}")
        if f.get("priority") and f["priority"] != 3:
            lines.append(f"• priority: {f['priority']}")
        if f.get("url"):
            lines.append(f"• url: {f['url']}")
        if f.get("minutes"):
            lines.append(f"• {f['minutes']}m" + (f" {f['when']}" if f.get("when") else ""))
        if prop.links:
            lines.append("• links: " + ", ".join(
                str(l.get("target_type")) for l in prop.links))
        prop.preview = lines

    # ------------------------------------------------------------- execution
    def execute(self, prop: dict[str, Any], *, confirmed: bool = False
                ) -> dict[str, Any]:
        op = prop.get("operation")
        if prop.get("questions"):
            return {"ok": False, "status": STATUS_AMBIGUOUS,
                    "questions": prop["questions"], "proposal": prop}
        if op == OP_MEMORY:
            return self._do_memory(prop)
        if op == OP_TRACK:
            return self._do_track(prop)
        if op == OP_LINK:
            return self._do_link(prop)
        if op == OP_UPDATE:
            return self._do_update(prop)
        if op == OP_ORGANIZE:
            if not confirmed:
                return {"ok": True, "status": STATUS_NEEDS_CONFIRMATION,
                        "proposal": prop, "preview": prop.get("preview")}
            return self._do_organize(prop, confirmed=True)
        if op == OP_SCHEDULE:
            return {"ok": True, "status": STATUS_NEEDS_CONFIRMATION,
                    "proposal": prop,
                    "message": "Prepared a scheduling request (confirmation "
                               "required before any calendar change)."}
        if op == OP_CREATE:
            return self._do_create(prop)
        return {"ok": False, "error": f"unknown operation {op!r}"}

    def _do_create(self, prop: dict[str, Any]) -> dict[str, Any]:
        tt = prop.get("target_type")
        name = prop.get("name") or ""
        f = prop.get("fields") or {}
        ctx = prop.get("context") or {}
        created: dict[str, Any] = {}
        links: list[dict[str, Any]] = []
        if tt == "course":
            code = name or "COURSE"
            res = self.container.courses.add_course(code, url=f.get("url", ""))
            created = {"type": "course", "id": res.get("course_id"),
                       "name": code}
        elif tt == "project":
            res = self.container.projects.create_project(
                name or "New Project",
                deadline=int(f.get("deadline") or 0),
                estimated_total_minutes=int(f.get("estimated_total_minutes") or 0),
                priority=int(f.get("priority") or 3))
            created = {"type": "project", "id": res["project"]["id"],
                       "name": name}
        elif tt == "task":
            tid = self.db.add_task(
                name or "New task", est_minutes=int(f.get("est_minutes") or 60),
                deadline=int(f.get("deadline") or 0),
                priority=int(f.get("priority") or 3))
            created = {"type": "task", "id": tid, "name": name}
        elif tt == "food":
            existing = self.container.food.get(name) if name else None
            if existing:
                self.db.update_food(int(existing["id"]),
                                    quantity=float(existing["quantity"]) +
                                    float(f.get("quantity") or 1))
                created = {"type": "food", "id": int(existing["id"]),
                           "name": existing["name"], "deduped": True}
            else:
                res = self.container.food.add(name, quantity=float(
                    f.get("quantity") or 1))
                created = {"type": "food", "id": res.get("food_id"),
                           "name": res.get("name")}
        elif tt == "grocery":
            sid = self.db.add_shopping(name, quantity=float(f.get("quantity") or 1))
            created = {"type": "grocery", "id": sid, "name": name}
        elif tt == "note":
            res = self.container.memory.remember(
                type="core_fact", subject=str(ctx.get("topic_name") or ""),
                key="note", value=(f.get("text") or name)[:2000],
                provenance="explicit_user", confidence=0.9,
                tags=["note"] + ([f"topic:{ctx['topic_name']}"]
                                 if ctx.get("topic_name") else []))
            created = {"type": "note", "id": (res.get("stored") or {}).get("id"),
                       "name": name, "value": f.get("text") or name}
        else:
            # generic fallback: preserve the user's wording as a note
            res = self.container.memory.remember(
                type="core_fact", subject=str(ctx.get("topic_name") or ""),
                key="note", value=(f.get("text") or name or prop.get("name") or "")[:2000],
                provenance="explicit_user", confidence=0.8, tags=["note"])
            created = {"type": "note", "id": (res.get("stored") or {}).get("id"),
                       "name": name}

        # link to the current topic if there is one
        if ctx.get("topic_id") and created.get("id") is not None:
            try:
                self.container.topics.add_link(
                    self.container.topics.get(ctx["chat_id"], ctx.get("thread_id", 0)),
                    created["type"], int(created["id"]), "about", 0.9, "creation")
                links.append({"topic_id": ctx["topic_id"],
                              "target_type": created["type"],
                              "target_id": created["id"]})
            except Exception:  # noqa: BLE001
                pass
        self._audit("create", created)
        return {"ok": True, "status": STATUS_CREATED, "created": created,
                "links": links,
                "message": self._created_message(created, links, f)}

    def _created_message(self, created: dict[str, Any], links: list[dict],
                         fields: dict[str, Any]) -> str:
        lines = [f"Added {created.get('name') or created.get('value', 'item')}"
                 f" ({created.get('type')})."]
        if links:
            lines.append("Linked to: " + ", ".join(
                f"topic #{l['topic_id']}" for l in links))
        if fields.get("deadline"):
            lines.append("Deadline: " + datetime.fromtimestamp(
                int(fields["deadline"])).strftime("%A"))
        if fields.get("est_minutes"):
            lines.append(f"Effort: {int(fields['est_minutes'])}m")
        return "\n".join(lines)

    def _do_update(self, prop: dict[str, Any]) -> dict[str, Any]:
        ref = (prop.get("fields") or {}).get("_existing") \
            or (prop.get("resolution") or {}).get("ref")
        if not ref:
            res = self.resolve(prop.get("name") or "", context=prop.get("context"))
            if res.get("status") != STATUS_RESOLVED:
                return {"ok": False, "status": res.get("status", STATUS_UNRESOLVED),
                        "questions": ["Which item should I update?"]}
            ref = res["ref"]
        tt, tid = ref["type"], int(ref["id"])
        f = prop.get("fields") or {}
        new_name = f.get("new_name") or prop.get("name")
        if tt == "project":
            fields = {}
            if f.get("deadline"):
                fields["deadline"] = int(f["deadline"])
            if f.get("estimated_total_minutes"):
                fields["estimated_total_minutes"] = int(f["estimated_total_minutes"])
            if new_name:
                fields["name"] = new_name
            self.container.projects.update_project(tid, **fields)
        elif tt == "task":
            fields = {}
            if f.get("deadline"):
                fields["deadline"] = int(f["deadline"])
            if f.get("est_minutes"):
                fields["est_minutes"] = int(f["est_minutes"])
            if f.get("priority"):
                fields["priority"] = int(f["priority"])
            if new_name:
                fields["title"] = new_name
            self.db.update_task(tid, **fields)
        elif tt == "food":
            fields = {}
            if f.get("quantity"):
                fields["quantity"] = float(f["quantity"])
            self.db.update_food(tid, **fields)
        elif tt == "course":
            fields = {}
            if f.get("url"):
                fields["url"] = f["url"]
            self.db.update_course(tid, **fields)
        else:
            return {"ok": False, "error": f"cannot update {tt}"}
        self._audit("update", {"type": tt, "id": tid})
        return {"ok": True, "status": STATUS_UPDATED,
                "updated": {"type": tt, "id": tid, "name": ref.get("name")},
                "message": f"Updated {ref.get('display') or ref.get('name')}."}

    def _do_link(self, prop: dict[str, Any]) -> dict[str, Any]:
        ctx = prop.get("context") or {}
        source = prop.get("fields", {}).get("reference")
        target_name = prop.get("name") or ""
        target = self.resolve(target_name, context=ctx)
        if target.get("status") == STATUS_AMBIGUOUS:
            return {"ok": False, "status": STATUS_AMBIGUOUS,
                    "questions": [f"Which one — " + ", ".join(
                        c["display"] for c in target["candidates"][:4]) + "?"],
                    "candidates": target["candidates"]}
        if target.get("status") != STATUS_RESOLVED:
            return {"ok": False, "status": STATUS_UNRESOLVED,
                    "questions": [f"I couldn't find {target_name!r}."]}
        ref = target["ref"]
        topic = None
        if ctx.get("chat_id") is not None and ctx.get("topic_id"):
            topic = self.container.topics.get(ctx["chat_id"], ctx.get("thread_id", 0))
        if topic is not None:
            self.container.topics.add_link(topic, ref["type"], int(ref["id"]),
                                           prop.get("fields", {}).get("relation",
                                                                      "related_to"),
                                           0.9, "creation")
            self._audit("link", ref)
            return {"ok": True, "status": STATUS_LINKED, "linked": ref,
                    "message": f"Linked this topic to {ref.get('display')}."}
        # link two resolved items by a shared reference is not supported without
        # a topic; treat as informational.
        return {"ok": True, "status": STATUS_LINKED, "linked": ref,
                "message": f"Noted the connection to {ref.get('display')}."}

    def _do_memory(self, prop: dict[str, Any]) -> dict[str, Any]:
        res = self.container.memory.remember_explicit(
            (prop.get("fields") or {}).get("text") or prop.get("name") or "")
        if not res.get("ok"):
            return {"ok": False, "status": "rejected",
                    "message": res.get("reason", "could not remember that")}
        return {"ok": True, "status": STATUS_CREATED,
                "memory": res.get("stored"),
                "message": "Remembered as an explicit preference."}

    def _do_track(self, prop: dict[str, Any]) -> dict[str, Any]:
        ctx = prop.get("context") or {}
        res = self.container.trackers.parse_request(
            (prop.get("fields") or {}).get("text") or prop.get("name") or "",
            context=ctx)
        if res.get("questions"):
            return {"ok": False, "status": STATUS_AMBIGUOUS,
                    "questions": res["questions"], "proposal": res}
        t = res["tracker"]
        tr = self.container.trackers.create(
            name=t["name"], source=t["source"], target_type=t["target_type"],
            target_id=t["target_id"], target_ref=t["target_ref"],
            condition=t["condition"], action=t["action"], cadence=t["cadence"],
            scope=t["scope"], priority=t["priority"],
            destination=res.get("destination") or {},
            one_shot=t["one_shot"], expires_at=t["expires_at"])
        return {"ok": True, "status": STATUS_CREATED, "tracker": tr.to_dict(),
                "message": "Tracking created."}

    def _do_organize(self, prop: dict[str, Any], *, confirmed: bool) -> dict[str, Any]:
        ctx = prop.get("context") or {}
        path = prop.get("name") or (prop.get("fields") or {}).get("path") or ""
        organizer = getattr(self.container, "organizer", None)
        if organizer is None:
            return {"ok": False, "error": "organizer unavailable"}
        # path safety: only paths inside configured roots are accepted
        try:
            engine = self.container.engine
            if path:
                engine.require_inside(path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "rejected", "error": str(exc),
                    "message": "That path is outside Butler's managed folders."}
        try:
            plan = organizer.plan_organize(path or "~/Downloads")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        items = [{"action": i.action, "src": i.src, "dest": i.dest}
                 for i in getattr(plan, "items", [])]
        self._audit("organize", {"path": path, "items": len(items)})
        return {"ok": True, "status": STATUS_PREVIEW, "plan": {
            "plan_id": getattr(plan, "plan_id", ""),
            "title": getattr(plan, "title", ""),
            "summary": getattr(plan, "summary", ""), "items": items[:100]},
            "requires_confirmation": True,
            "message": (f"Proposed organizing {len(items)} file(s). "
                        "Confirmation required before anything moves.")}

    # ------------------------------------------------------------- helpers
    def alias(self, target_type: str, target_id: int, alias: str,
              *, canonical: str = "", source: str = "user",
              confidence: float = 1.0) -> dict[str, Any]:
        alias = (alias or "").strip()
        if not alias:
            return {"ok": False, "error": "alias required"}
        self.db.execute(
            "INSERT INTO entity_aliases(target_type,target_id,alias,canonical,"
            "source,confidence,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(target_type,target_id,alias) DO UPDATE SET "
            "canonical=excluded.canonical, source=excluded.source, "
            "confidence=excluded.confidence, updated_at=excluded.updated_at",
            (target_type, int(target_id), _norm(alias), canonical, source,
             float(confidence), _now(), _now()))
        return {"ok": True, "alias": alias, "target_type": target_type,
                "target_id": int(target_id)}

    def aliases_for(self, target_type: str, target_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM entity_aliases WHERE target_type=? AND target_id=?",
            (target_type, int(target_id)))]

    def _audit(self, action: str, detail: Any = None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(f"creation_{action}", actor="creation",
                              target="", decision="allowed", outcome="ok",
                              detail=detail or {})
        except Exception:  # noqa: BLE001
            log.debug("creation audit failed", exc_info=True)


def _match(text: str, table: tuple[str, ...]) -> bool:
    return any(t in text for t in table)
