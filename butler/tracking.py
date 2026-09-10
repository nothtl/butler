"""N2: universal tracking / trigger engine.

ONE generic engine for every "when X changes, do Y" request. It is deliberately
not a workflow framework and not a graph database: a compact :class:`Tracker`
row (with JSON ``condition``/``action``) is evaluated by a deterministic rule
engine over a *normalized source snapshot*.

    tracker -> source provider -> normalized state -> diff -> events
            -> condition evaluation -> action proposal
            -> M7 ProactiveCandidate -> existing notification policy -> Telegram

Design ideas adopted (and why):

* **Home Assistant trigger/condition/action** — separate the *event source*, the
  *condition* and the *action*; keep the condition vocabulary small and explicit.
* **n8n / Temporal durable workflow** — persist enough state (last snapshot,
  hash, next check, failure count) that a restart is safe and never re-fires an
  unchanged observation.
* **Airflow/Dagster scheduling** — cadence + ``next_check_at`` rather than an
  unbounded loop; only due trackers are evaluated.
* **Butler M7** — reuse the proactive candidate + notification policy (ranking,
  budget, quiet hours, snooze, dedup) instead of a second notifier.
* **Butler M4** — reuse the web knowledge stack; never a second HTTP client.
* **Butler Phase 6** — reuse retry/circuit-breaker, idempotency and audit.

No external workflow dependency is introduced.
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

log = logging.getLogger("butler.tracking")

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------
STATE_PENDING = "pending"
STATE_ACTIVE = "active"
STATE_PAUSED = "paused"
STATE_DEGRADED = "degraded"
STATE_ERROR = "error"
STATE_DISABLED = "disabled"
STATE_ARCHIVED = "archived"
STATES = (STATE_PENDING, STATE_ACTIVE, STATE_PAUSED, STATE_DEGRADED,
          STATE_ERROR, STATE_DISABLED, STATE_ARCHIVED)

SCOPE_GLOBAL = "global"
SCOPE_TOPIC = "topic"
SCOPE_OBJECT = "object"
SCOPES = (SCOPE_GLOBAL, SCOPE_TOPIC, SCOPE_OBJECT)

PRIORITIES = ("critical", "high", "medium", "low")

EV_NEW_ITEM = "NEW_ITEM"
EV_ITEM_REMOVED = "ITEM_REMOVED"
EV_DATA_CHANGED = "DATA_CHANGED"
EV_THRESHOLD_CROSSED = "THRESHOLD_CROSSED"
EV_DEADLINE_CHANGED = "DEADLINE_CHANGED"
EV_RISK_CHANGED = "RISK_CHANGED"
EV_STATUS_CHANGED = "STATUS_CHANGED"
EV_SCHEDULE_CONFLICT = "SCHEDULE_CONFLICT"
EV_TIME_REACHED = "TIME_REACHED"
EV_EXTERNAL_UPDATED = "EXTERNAL_SOURCE_UPDATED"

CONDITION_TYPES = (
    "new_item", "item_removed", "field_changed", "deadline_changed",
    "status_changed", "risk_crossed_above", "threshold_below", "threshold_above",
    "no_activity", "time_reached", "state_changed", "and", "or",
)
ACTION_TYPES = (
    "NOTIFY_TOPIC", "CREATE_SUGGESTION", "UPDATE_INTERNAL_STATE",
    "CREATE_REMINDER", "PROPOSE_CALENDAR_ACTION",
)
#: Actions that would be consequential if executed; they are only ever proposed.
CONSEQUENTIAL_ACTIONS = frozenset({"PROPOSE_CALENDAR_ACTION"})

MAX_CONDITION_LEAVES = 4
MAX_SNAPSHOT_CHARS = 20000

_CADENCE_DEFAULTS = {
    "food": 3600, "project_risk": 3600, "task": 3600, "calendar": 900,
    "course": 21600, "web": 43200, "github": 43200, "file": 3600,
    "snapshot": 3600, "global": 3600,
}


def _now() -> int:
    return int(time.time())


def _dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _hash(obj: Any) -> str:
    return hashlib.sha1(_dumps(obj).encode("utf-8")).hexdigest()[:16]


def _cadence_seconds(spec: Any, default: int = 21600) -> int:
    if isinstance(spec, (int, float)):
        return max(60, int(spec))
    s = str(spec or "").strip().lower()
    if not s:
        return default
    if s in ("hourly", "1h"):
        return 3600
    if s in ("daily", "day", "1d"):
        return 86400
    if s in ("weekly", "1w"):
        return 7 * 86400
    m = re.match(r"(\d+)\s*(m|min|minute|minutes|h|hr|hour|hours|d|day|days)?$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2) or "m"
        if unit.startswith("h"):
            return max(60, n * 3600)
        if unit.startswith("d"):
            return max(60, n * 86400)
        return max(60, n * 60)
    return default


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------
@dataclass
class Event:
    event_type: str
    target_type: str = ""
    target_id: int = 0
    summary: str = ""
    before: Any = None
    after: Any = None
    field_name: str = ""
    identity: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ActionProposal:
    action_type: str
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class EvaluationResult:
    tracker_id: int
    fired: bool
    events: list[Event] = field(default_factory=list)
    proposals: list[ActionProposal] = field(default_factory=list)
    reason: str = ""
    state: str = STATE_ACTIVE
    dry_run: bool = False
    snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"tracker_id": self.tracker_id, "fired": self.fired,
                "events": [e.to_dict() for e in self.events],
                "proposals": [p.to_dict() for p in self.proposals],
                "reason": self.reason, "state": self.state,
                "dry_run": self.dry_run}


@dataclass
class Tracker:
    id: int = 0
    name: str = ""
    target_type: str = ""
    target_id: int = 0
    target_ref: str = ""
    source: str = ""
    condition: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] = field(default_factory=dict)
    cadence_seconds: int = 21600
    scope: str = SCOPE_OBJECT
    priority: str = "medium"
    destination_chat_id: int = 0
    destination_thread_id: int = 0
    destination_topic_id: int = 0
    enabled: bool = True
    state: str = STATE_PENDING
    last_checked_at: int = 0
    next_check_at: int = 0
    last_state_hash: str = ""
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    last_event: str = ""
    last_event_at: int = 0
    failure_count: int = 0
    cooldown_until: int = 0
    one_shot: bool = False
    completed: bool = False
    expires_at: int = 0
    provenance: str = ""
    created_at: int = 0
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_row(cls, row: Any) -> "Tracker":
        return cls(
            id=int(row["id"]), name=str(row["name"] or ""),
            target_type=str(row["target_type"] or ""),
            target_id=int(row["target_id"] or 0),
            target_ref=str(row["target_ref"] or ""),
            source=str(row["source"] or ""),
            condition=_loads(row["condition"], {}),
            action=_loads(row["action"], {}),
            cadence_seconds=int(row["cadence_seconds"] or 21600),
            scope=str(row["scope"] or SCOPE_OBJECT),
            priority=str(row["priority"] or "medium"),
            destination_chat_id=int(row["destination_chat_id"] or 0),
            destination_thread_id=int(row["destination_thread_id"] or 0),
            destination_topic_id=int(row["destination_topic_id"] or 0),
            enabled=bool(row["enabled"]),
            state=str(row["state"] or STATE_PENDING),
            last_checked_at=int(row["last_checked_at"] or 0),
            next_check_at=int(row["next_check_at"] or 0),
            last_state_hash=str(row["last_state_hash"] or ""),
            last_snapshot=_loads(row["last_snapshot"], {}),
            last_event=str(row["last_event"] or ""),
            last_event_at=int(row["last_event_at"] or 0),
            failure_count=int(row["failure_count"] or 0),
            cooldown_until=int(row["cooldown_until"] or 0),
            one_shot=bool(row["one_shot"]),
            completed=bool(row["completed"]),
            expires_at=int(row["expires_at"] or 0),
            provenance=str(row["provenance"] or ""),
            created_at=int(row["created_at"] or 0),
            updated_at=int(row["updated_at"] or 0),
        )


# ---------------------------------------------------------------------------
# source providers
# ---------------------------------------------------------------------------
class SourceUnavailable(RuntimeError):
    pass


class SourceProvider:
    name = "base"
    kind = "local"          # local | external
    auto_refresh = False

    def __init__(self, container: Any):
        self.container = container

    def refresh(self, tracker: Tracker, now: int) -> None:
        """Optionally update upstream state (e.g. sync a course page)."""
        return None

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        raise NotImplementedError


def _item(label: str, **fields: Any) -> dict[str, Any]:
    return {"label": label, "fields": fields}


class FoodProvider(SourceProvider):
    name = "food"
    kind = "local"

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        food = getattr(self.container, "food", None)
        if food is None or not hasattr(food, "all"):
            raise SourceUnavailable("food inventory unavailable")
        items = {}
        for it in food.all():
            name = str(it.get("name") or "")
            if not name:
                continue
            items[_norm(name)] = _item(name, quantity=float(it.get("quantity") or 0))
        return {"items": items}


class ProjectRiskProvider(SourceProvider):
    name = "project_risk"
    kind = "local"

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        projects = getattr(self.container, "projects", None)
        if projects is None:
            raise SourceUnavailable("projects unavailable")
        if tracker.target_id:
            got = projects.get_project(tracker.target_id)
        else:
            got = None
            for p in projects.list_projects():
                if _norm(p["name"]) == _norm(tracker.target_ref):
                    got = p
                    break
        if got is None:
            raise SourceUnavailable("project not found")
        return {"fields": {"risk": float(got.get("risk") or 0.0),
                           "level": str(got.get("risk_level") or "low"),
                           "remaining_minutes": int(got.get("remaining_minutes") or 0)}}


class TaskProvider(SourceProvider):
    name = "task"
    kind = "local"

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        db = self.container.db
        items = {}
        for r in db.tasks("active"):
            items[str(r["id"])] = _item(str(r["title"]),
                                        status=str(r["status"]),
                                        deadline=int(r["deadline"] or 0))
        return {"items": items}


class CalendarProvider(SourceProvider):
    name = "calendar"
    kind = "local"

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        db = self.container.db
        items = {}
        for r in db.events():
            items[str(r["id"])] = _item(str(r["title"]),
                                        start=int(r["start_ts"]),
                                        end=int(r["end_ts"]))
        return {"items": items}


class CourseProvider(SourceProvider):
    """Watches the *normalized local state* the course subsystem maintains."""
    name = "course"
    kind = "external"

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        db = self.container.db
        code = (tracker.target_ref or "").strip().upper()
        course = db.course_by_code(code) if code else None
        if course is None and tracker.target_id:
            course = db.course_by_id(int(tracker.target_id))
        if course is None:
            raise SourceUnavailable(f"course {code or tracker.target_id} not found")
        items = {}
        for d in db.course_documents(int(course["id"])):
            title = str(d["title"] or d["url"] or "")
            items[_norm(title)] = _item(title, deadline=int(d["deadline"] or 0),
                                        doc_type=str(d["doc_type"] or ""))
        for r in db.tasks("active"):
            if code and code.lower() in str(r["title"]).lower():
                items[_norm(str(r["title"]))] = _item(
                    str(r["title"]), deadline=int(r["deadline"] or 0),
                    status=str(r["status"]))
        return {"items": items}


class WebProvider(SourceProvider):
    name = "web"
    kind = "external"

    def __init__(self, container: Any, fetcher: Any = None):
        super().__init__(container)
        self.fetcher = fetcher

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        url = tracker.target_ref
        if not url:
            raise SourceUnavailable("no URL to watch")
        if self.fetcher is not None:
            res = self.fetcher(url)
            if not res or not res.get("ok", True):
                raise SourceUnavailable(str((res or {}).get("error") or "fetch failed"))
            text = str(res.get("text") or "")
        else:
            web = getattr(self.container, "web", None)
            if web is None:
                raise SourceUnavailable("web unavailable")
            fr = web.fetch(url)
            if not fr.ok:
                raise SourceUnavailable(fr.error or "fetch failed")
            text = str(fr.text or "")
        norm = re.sub(r"\s+", " ", text).strip()[:MAX_SNAPSHOT_CHARS]
        return {"fields": {"text": norm, "hash": _hash(norm)}}


class GitHubProvider(SourceProvider):
    name = "github"
    kind = "external"

    def __init__(self, container: Any, fetcher: Any = None):
        super().__init__(container)
        self.fetcher = fetcher

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        repo = (tracker.target_ref or "").strip()
        if not repo:
            raise SourceUnavailable("no repository to watch")
        if self.fetcher is not None:
            data = self.fetcher(repo)
            if data is None:
                raise SourceUnavailable("github fetch failed")
        else:
            web = getattr(self.container, "web", None)
            if web is None:
                raise SourceUnavailable("github unavailable")
            data = None
            try:
                fr = web.fetch(f"https://api.github.com/repos/{repo}")
                if fr.ok and fr.text:
                    data = json.loads(fr.text)
            except Exception as exc:  # noqa: BLE001
                raise SourceUnavailable(str(exc)) from exc
            if not isinstance(data, dict):
                raise SourceUnavailable("github not configured")
        return {"fields": {
            "pushed_at": str(data.get("pushed_at") or ""),
            "open_issues": int(data.get("open_issues_count") or 0),
            "default_branch": str(data.get("default_branch") or ""),
            "stars": int(data.get("stargazers_count") or 0),
            "latest_release": str((data.get("latest_release") or {}).get("tag_name")
                                  if isinstance(data.get("latest_release"), dict)
                                  else data.get("latest_release") or ""),
        }}


class FileProvider(SourceProvider):
    name = "file"
    kind = "local"

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        import os
        root = tracker.target_ref
        if not root or not os.path.isdir(root):
            raise SourceUnavailable("directory not found")
        items = {}
        try:
            for name in sorted(os.listdir(root))[:500]:
                p = os.path.join(root, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                items[name] = _item(name, mtime=int(st.st_mtime),
                                    size=int(st.st_size))
        except OSError as exc:
            raise SourceUnavailable(str(exc)) from exc
        return {"items": items}


class SnapshotProvider(SourceProvider):
    """Injectable provider for tests / sandbox scenarios."""
    name = "snapshot"
    kind = "local"

    def __init__(self, container: Any, snapshot: dict[str, Any] | None = None,
                 fail: bool = False):
        super().__init__(container)
        self._snapshot = snapshot or {}
        self._fail = fail

    def set_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot
        self._fail = False

    def snapshot(self, tracker: Tracker, now: int) -> dict[str, Any]:
        if self._fail:
            raise SourceUnavailable("synthetic failure")
        return self._snapshot


def default_providers(container: Any) -> dict[str, SourceProvider]:
    provs: list[SourceProvider] = [
        FoodProvider(container), ProjectRiskProvider(container),
        TaskProvider(container), CalendarProvider(container),
        CourseProvider(container), WebProvider(container),
        GitHubProvider(container), FileProvider(container),
    ]
    return {p.name: p for p in provs}


# ---------------------------------------------------------------------------
# diffing + condition evaluation
# ---------------------------------------------------------------------------
def _fields_of(entry: Any) -> dict[str, Any]:
    if isinstance(entry, dict):
        return dict(entry.get("fields") or {})
    return {}


def _label_of(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("label") or "")
    return str(entry)


def diff_snapshots(prev: dict[str, Any], curr: dict[str, Any]) -> list[Event]:
    """Meaningful, normalized differences between two snapshots."""
    events: list[Event] = []
    p_items = dict(prev.get("items") or {})
    c_items = dict(curr.get("items") or {})
    for key, entry in c_items.items():
        if key not in p_items:
            events.append(Event(EV_NEW_ITEM, summary=f"new item: {_label_of(entry)}",
                                after=entry, identity=str(key)))
            continue
        pf, cf = _fields_of(p_items[key]), _fields_of(entry)
        for fname, cval in cf.items():
            if fname in pf and pf[fname] != cval:
                etype = EV_DATA_CHANGED
                if fname == "deadline":
                    etype = EV_DEADLINE_CHANGED
                elif fname == "status":
                    etype = EV_STATUS_CHANGED
                events.append(Event(etype, field_name=fname,
                                    summary=f"{_label_of(entry)} {fname} changed",
                                    before=pf[fname], after=cval,
                                    identity=f"{key}:{fname}"))
    for key, entry in p_items.items():
        if key not in c_items:
            events.append(Event(EV_ITEM_REMOVED,
                                summary=f"removed: {_label_of(entry)}",
                                before=entry, identity=str(key)))
    pf = dict(prev.get("fields") or {})
    cf = dict(curr.get("fields") or {})
    for fname, cval in cf.items():
        if fname in pf and pf[fname] != cval:
            etype = EV_DATA_CHANGED
            if fname == "risk":
                etype = EV_RISK_CHANGED
            elif fname == "deadline":
                etype = EV_DEADLINE_CHANGED
            elif fname == "conflict":
                etype = EV_SCHEDULE_CONFLICT
            events.append(Event(etype, field_name=fname,
                                summary=f"{fname} changed",
                                before=pf[fname], after=cval,
                                identity=f"field:{fname}"))
    return events


def _find_item(snapshot: dict[str, Any], needle: str) -> dict[str, Any] | None:
    if not needle:
        return None
    needle = _norm(needle)
    items = snapshot.get("items") or {}
    if needle in items:
        return _fields_of(items[needle])
    for key, entry in items.items():
        if needle in _norm(key) or needle in _norm(_label_of(entry)):
            return _fields_of(entry)
    return None


def _crossed_below(prev_val: Any, curr_val: Any, threshold: float) -> bool:
    try:
        c = float(curr_val)
    except (TypeError, ValueError):
        return False
    if c >= threshold:
        return False
    try:
        p = float(prev_val)
    except (TypeError, ValueError):
        return True  # first observation already below threshold
    return p >= threshold


def _crossed_above(prev_val: Any, curr_val: Any, threshold: float) -> bool:
    try:
        c = float(curr_val)
    except (TypeError, ValueError):
        return False
    if c <= threshold:
        return False
    try:
        p = float(prev_val)
    except (TypeError, ValueError):
        return True
    return p <= threshold


def evaluate_condition(condition: dict[str, Any], events: list[Event],
                       prev: dict[str, Any], curr: dict[str, Any],
                       now: int, last_checked_at: int = 0) -> tuple[bool, str]:
    """Deterministic condition evaluation. Returns (fired, reason)."""
    if not condition:
        return bool(events), "any change"
    ctype = str(condition.get("type") or "").strip()

    def any_event(*types: str, field_name: str = "") -> Event | None:
        for e in events:
            if e.event_type in types and (not field_name or e.field_name == field_name):
                return e
        return None

    if ctype == "new_item":
        e = any_event(EV_NEW_ITEM)
        return (bool(e), f"new item: {e.summary}" if e else "no new item")
    if ctype == "item_removed":
        e = any_event(EV_ITEM_REMOVED)
        return (bool(e), f"removed: {e.summary}" if e else "no removal")
    if ctype == "field_changed":
        f = str(condition.get("field") or "")
        e = any_event(EV_DATA_CHANGED, EV_DEADLINE_CHANGED, EV_STATUS_CHANGED,
                      field_name=f) or any_event(EV_DATA_CHANGED, field_name=f)
        if not e and f:
            e = next((x for x in events if x.field_name == f), None)
        return (bool(e), f"{f} changed" if e else f"{f} unchanged")
    if ctype == "deadline_changed":
        e = any_event(EV_DEADLINE_CHANGED)
        return (bool(e), "deadline changed" if e else "deadline unchanged")
    if ctype == "status_changed":
        e = any_event(EV_STATUS_CHANGED)
        return (bool(e), "status changed" if e else "status unchanged")
    if ctype == "risk_crossed_above":
        thr = float(condition.get("value") or 0.8)
        pv = (prev.get("fields") or {}).get("risk")
        cv = (curr.get("fields") or {}).get("risk")
        fired = _crossed_above(pv, cv, thr)
        return (fired, f"risk {pv} -> {cv} (threshold {thr})")
    if ctype in ("threshold_below", "threshold_above"):
        thr = float(condition.get("value") or 0)
        item = str(condition.get("item") or "")
        field_name = str(condition.get("field") or "quantity")
        if item:
            pf = _find_item(prev, item) or {}
            cf = _find_item(curr, item) or {}
            pv, cv = pf.get(field_name), cf.get(field_name)
        else:
            pv = (prev.get("fields") or {}).get(field_name)
            cv = (curr.get("fields") or {}).get(field_name)
        if ctype == "threshold_below":
            fired = _crossed_below(pv, cv, thr)
            return (fired, f"{field_name} {pv} -> {cv} (< {thr})")
        fired = _crossed_above(pv, cv, thr)
        return (fired, f"{field_name} {pv} -> {cv} (> {thr})")
    if ctype == "no_activity":
        days = float(condition.get("days") or 5)
        last = curr.get("last_activity") or (curr.get("fields") or {}).get("last_activity")
        prev_last = prev.get("last_activity") or (prev.get("fields") or {}).get("last_activity")
        if not last:
            return (False, "no activity timestamp")
        age = (now - int(last)) / 86400.0
        prev_age = (last_checked_at - int(prev_last)) / 86400.0 if prev_last else None
        fired = age >= days and (prev_age is None or prev_age < days)
        return (fired, f"inactive {age:.1f}d (>= {days}d)")
    if ctype == "time_reached":
        at = int(condition.get("at") or 0)
        fired = bool(at) and now >= at and last_checked_at < at
        return (fired, f"time reached ({at})" if fired else "time not reached")
    if ctype == "state_changed":
        f = str(condition.get("field") or "")
        e = next((x for x in events if not f or x.field_name == f), None)
        return (bool(e), "state changed" if e else "state unchanged")
    if ctype in ("and", "or"):
        subs = list(condition.get("conditions") or [])[:MAX_CONDITION_LEAVES]
        results = [evaluate_condition(c, events, prev, curr, now, last_checked_at)
                   for c in subs if isinstance(c, dict)]
        if not results:
            return (False, "empty group")
        if ctype == "and":
            fired = all(r[0] for r in results)
        else:
            fired = any(r[0] for r in results)
        return (fired, "; ".join(r[1] for r in results))
    return (bool(events), "any change")


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------
class TrackerEngine:
    def __init__(self, container: Any, providers: dict[str, SourceProvider] | None = None):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db = getattr(container, "db", None)
        self.audit = getattr(container, "audit", None)
        self.retry = getattr(container, "retry", None)
        self.providers = providers or default_providers(container)
        self.max_per_cycle = int(getattr(self.cfg, "tracker_max_per_cycle", 50))
        self.default_cooldown = int(getattr(self.cfg, "tracker_cooldown_minutes",
                                            360)) * 60
        self.max_failures = int(getattr(self.cfg, "tracker_max_failures", 3))

    # ------------------------------------------------------------- registry
    def register_provider(self, provider: SourceProvider) -> None:
        self.providers[provider.name] = provider

    def provider_for(self, source: str) -> SourceProvider | None:
        return self.providers.get(str(source or ""))

    # ------------------------------------------------------------- CRUD
    def create(self, *, name: str, source: str, target_type: str = "",
               target_id: int = 0, target_ref: str = "",
               condition: dict[str, Any] | None = None,
               action: dict[str, Any] | None = None,
               cadence: Any = None, scope: str = SCOPE_OBJECT,
               priority: str = "medium", destination: dict[str, Any] | None = None,
               one_shot: bool = False, expires_at: int = 0,
               provenance: str = "user", state: str = STATE_ACTIVE,
               now: int | None = None) -> Tracker:
        now = int(now if now is not None else _now())
        dest = destination or {}
        cad = _cadence_seconds(cadence, _CADENCE_DEFAULTS.get(source, 21600))
        cur = self.db.execute(
            "INSERT INTO trackers(name,target_type,target_id,target_ref,source,"
            "condition,action,cadence_seconds,scope,priority,destination_chat_id,"
            "destination_thread_id,destination_topic_id,enabled,state,next_check_at,"
            "one_shot,expires_at,provenance,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, target_type, int(target_id), target_ref, source,
             _dumps(condition or {}), _dumps(action or {}), cad, scope, priority,
             int(dest.get("chat_id") or 0), int(dest.get("thread_id") or 0),
             int(dest.get("topic_id") or 0), 1, state, now, 1 if one_shot else 0,
             int(expires_at or 0), provenance, now, now))
        tracker = self.get(int(cur.lastrowid))
        self._audit("tracker_create", tracker, detail={"source": source,
                                                       "name": name})
        return tracker

    def get(self, tracker_id: int) -> Tracker | None:
        row = self.db.one("SELECT * FROM trackers WHERE id=?", (int(tracker_id),))
        return Tracker.from_row(row) if row else None

    def list(self, *, state: str = "", enabled: bool | None = None,
             destination_topic_id: int = 0, limit: int = 100) -> list[Tracker]:
        sql = "SELECT * FROM trackers WHERE 1=1"
        params: list[Any] = []
        if state:
            sql += " AND state=?"
            params.append(state)
        if enabled is not None:
            sql += " AND enabled=?"
            params.append(1 if enabled else 0)
        if destination_topic_id:
            sql += " AND destination_topic_id=?"
            params.append(int(destination_topic_id))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [Tracker.from_row(r) for r in self.db.query(sql, tuple(params))]

    def by_destination(self, chat_id: int, thread_id: int) -> list[Tracker]:
        rows = self.db.query(
            "SELECT * FROM trackers WHERE destination_chat_id=? AND "
            "destination_thread_id=? ORDER BY id", (int(chat_id), int(thread_id)))
        return [Tracker.from_row(r) for r in rows]

    def update(self, tracker: Tracker, **fields: Any) -> Tracker:
        allowed = {"name", "condition", "action", "cadence_seconds", "scope",
                   "priority", "destination_chat_id", "destination_thread_id",
                   "destination_topic_id", "enabled", "state", "next_check_at",
                   "last_checked_at", "last_state_hash", "last_snapshot",
                   "last_event", "last_event_at", "failure_count",
                   "cooldown_until", "one_shot", "completed", "expires_at"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k in ("condition", "action", "last_snapshot"):
                v = _dumps(v)
            elif k == "enabled":
                v = 1 if v else 0
            elif k in ("one_shot", "completed"):
                v = 1 if v else 0
            sets.append(f"{k}=?")
            params.append(v)
        if not sets:
            return tracker
        sets.append("updated_at=?")
        params.append(_now())
        params.append(tracker.id)
        self.db.execute(f"UPDATE trackers SET {', '.join(sets)} WHERE id=?",
                        tuple(params))
        return self.get(tracker.id)

    def control(self, tracker_id: int, action: str, *,
                now: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else _now())
        t = self.get(tracker_id)
        if t is None:
            return {"ok": False, "error": "tracker not found"}
        action = str(action or "").lower()
        mapping = {"pause": STATE_PAUSED, "resume": STATE_ACTIVE,
                   "enable": STATE_ACTIVE, "disable": STATE_DISABLED,
                   "archive": STATE_ARCHIVED, "stop": STATE_ARCHIVED}
        if action not in mapping:
            return {"ok": False, "error": f"unknown action {action!r}"}
        state = mapping[action]
        enabled = state == STATE_ACTIVE
        t = self.update(t, state=state, enabled=enabled,
                        next_check_at=now if enabled else 0)
        self._audit(f"tracker_{action}", t, detail={"state": state})
        return {"ok": True, "tracker": t.to_dict()}

    # ------------------------------------------------------------- evaluate
    def evaluate(self, tracker: Tracker, *, now: int | None = None,
                 dry_run: bool = False, snapshot: dict[str, Any] | None = None
                 ) -> EvaluationResult:
        now = int(now if now is not None else _now())
        provider = self.provider_for(tracker.source)
        if provider is None and snapshot is None:
            return EvaluationResult(tracker.id, False, state=STATE_ERROR,
                                    reason=f"unknown source {tracker.source!r}")
        prev = dict(tracker.last_snapshot or {})
        try:
            if snapshot is not None:
                curr = snapshot
            else:
                if provider.auto_refresh and not dry_run:
                    provider.refresh(tracker, now)
                curr = provider.snapshot(tracker, now)
        except SourceUnavailable as exc:
            return EvaluationResult(tracker.id, False, state=STATE_DEGRADED,
                                    reason=f"source unavailable: {exc}")
        except Exception as exc:  # noqa: BLE001
            return EvaluationResult(tracker.id, False, state=STATE_ERROR,
                                    reason=f"provider error: {exc}")

        events = diff_snapshots(prev, curr)
        fired, reason = evaluate_condition(tracker.condition, events, prev, curr,
                                           now, tracker.last_checked_at)
        # time-based conditions have no diff events but may still fire
        proposals: list[ActionProposal] = []
        if fired:
            proposals = self._proposals_for(tracker, events, curr, reason)
        return EvaluationResult(tracker.id, fired, events=events,
                                proposals=proposals, reason=reason,
                                state=STATE_ACTIVE, dry_run=dry_run,
                                snapshot=curr)

    def _proposals_for(self, tracker: Tracker, events: list[Event],
                       curr: dict[str, Any], reason: str) -> list[ActionProposal]:
        action = dict(tracker.action or {})
        atype = str(action.get("type") or "NOTIFY_TOPIC")
        params = dict(action.get("params") or {})
        # derive a useful default message
        params.setdefault("tracker", tracker.name)
        params.setdefault("reason", reason)
        if atype == "CREATE_SUGGESTION" and "items" not in params:
            # e.g. grocery shortfall from a threshold crossing
            item = (tracker.condition or {}).get("item")
            params["items"] = [item] if item else []
        return [ActionProposal(atype, params, reason=reason)]

    # ------------------------------------------------------------- run cycle
    def run_due(self, *, now: int | None = None, deliver: bool = True,
                limit: int | None = None) -> dict[str, Any]:
        now = int(now if now is not None else _now())
        limit = int(limit or self.max_per_cycle)
        rows = self.db.query(
            "SELECT * FROM trackers WHERE enabled=1 AND completed=0 AND "
            "state IN (?,?,?) AND (next_check_at=0 OR next_check_at<=?) AND "
            "(expires_at=0 OR expires_at>?) ORDER BY next_check_at, id LIMIT ?",
            (STATE_ACTIVE, STATE_DEGRADED, STATE_ERROR, now, now, limit))
        evaluated = fired = 0
        results: list[dict[str, Any]] = []
        for row in rows:
            t = Tracker.from_row(row)
            res = self.evaluate(t, now=now)
            evaluated += 1
            self._after_evaluate(t, res, now=now, deliver=deliver)
            if res.fired:
                fired += 1
            results.append({"tracker_id": t.id, "fired": res.fired,
                            "reason": res.reason, "state": res.state})
        # expired trackers -> archived
        self._expire(now)
        return {"ok": True, "evaluated": evaluated, "fired": fired,
                "results": results}

    def _after_evaluate(self, t: Tracker, res: EvaluationResult, *, now: int,
                        deliver: bool) -> None:
        if res.state in (STATE_DEGRADED, STATE_ERROR):
            fc = t.failure_count + 1
            state = STATE_ERROR if fc >= self.max_failures else STATE_DEGRADED
            backoff = min(3600 * (2 ** min(fc, 5)), 6 * 3600)
            self.update(t, state=state, failure_count=fc,
                        last_checked_at=now, next_check_at=now + backoff)
            self._audit("tracker_evaluation_failed", t,
                        detail={"reason": res.reason, "failure_count": fc})
            return
        fields: dict[str, Any] = {
            "state": STATE_ACTIVE, "failure_count": 0,
            "last_checked_at": now, "next_check_at": now + t.cadence_seconds,
            "last_state_hash": _hash(res.snapshot),
            "last_snapshot": res.snapshot,
        }
        if res.fired:
            fields["last_event"] = res.reason[:300]
            fields["last_event_at"] = now
            fields["cooldown_until"] = now + self.default_cooldown
        self.update(t, **fields)
        # persist meaningful events (idempotent) and bridge to M7
        for ev in res.events:
            self._persist_event(t, ev, now)
        self._audit("tracker_evaluation", t,
                    detail={"fired": res.fired, "reason": res.reason})
        if res.fired:
            on_cooldown = now < t.cooldown_until
            if deliver and not on_cooldown:
                cands = self._candidates_for(t, res, now)
                if cands:
                    self._bridge(cands, now)
        if t.one_shot and res.fired:
            self.update(self.get(t.id), completed=True, state=STATE_ARCHIVED,
                        enabled=False)

    def _expire(self, now: int) -> int:
        cur = self.db.execute(
            "UPDATE trackers SET state=?, enabled=0, updated_at=? "
            "WHERE expires_at>0 AND expires_at<=? AND state NOT IN (?,?)",
            (STATE_ARCHIVED, now, now, STATE_ARCHIVED, STATE_DISABLED))
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------- events
    def _event_key(self, tracker: Tracker, ev: Event) -> str:
        identity = ev.identity or f"{ev.event_type}:{ev.field_name}"
        version = _hash(ev.after)
        return hashlib.sha1(
            f"{tracker.id}:{ev.event_type}:{identity}:{version}".encode()
        ).hexdigest()[:20]

    def _persist_event(self, tracker: Tracker, ev: Event, now: int) -> bool:
        key = self._event_key(tracker, ev)
        exists = self.db.one("SELECT id FROM tracker_events WHERE event_key=?",
                             (key,))
        if exists:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO tracker_events(tracker_id,event_key,event_type,"
            "target_type,target_id,summary,evidence,before_state,after_state,"
            "observed_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tracker.id, key, ev.event_type, tracker.target_type,
             tracker.target_id, ev.summary[:300], _dumps(ev.evidence),
             _dumps(ev.before), _dumps(ev.after), now, now))
        self._audit("tracker_event", tracker,
                    detail={"event_type": ev.event_type, "summary": ev.summary[:120]})
        return True

    # ------------------------------------------------------------- bridge
    def _candidates_for(self, tracker: Tracker, res: EvaluationResult,
                        now: int) -> list[Any]:
        from .proactive_engine import ProactiveCandidate
        out = []
        for prop in res.proposals:
            atype = prop.action_type
            if atype == "UPDATE_INTERNAL_STATE":
                continue
            key = f"tracker:{tracker.id}:{_hash(res.reason)[:10]}"
            impact = {"critical": 1.0, "high": 0.8, "medium": 0.5,
                      "low": 0.3}.get(tracker.priority, 0.5)
            summary = (f"{tracker.name}: {res.reason}" if tracker.name
                       else res.reason)
            out.append(ProactiveCandidate(
                key=key, category="tracker",
                title=f"🔎 {tracker.name or 'Tracker'}",
                summary=summary,
                confidence=0.85, detected_at=now, urgency=impact,
                impact=impact,
                evidence=[{"kind": "tracker", "value": tracker.name},
                          {"kind": "source", "value": tracker.source},
                          {"kind": "reason", "value": res.reason}],
                proposed_action={"action": atype, **prop.params},
                requires_confirmation=(atype in CONSEQUENTIAL_ACTIONS),
                destination={"chat_id": tracker.destination_chat_id,
                             "thread_id": tracker.destination_thread_id},
                explanation=self.explain_tracker(tracker, res)))
        return out

    def _bridge(self, candidates: list[Any], now: int) -> list[dict[str, Any]]:
        proactive = getattr(self.container, "proactive_engine", None)
        if proactive is None or not hasattr(proactive, "ingest"):
            return []
        return proactive.ingest(candidates, now=now, deliver=True)

    # ------------------------------------------------------------- explain
    def explain_tracker(self, tracker: Tracker,
                        res: EvaluationResult | None = None) -> str:
        src = tracker.source or "local"
        bits = [f"Watching {tracker.target_ref or tracker.target_type} "
                f"via {src}."]
        cond = tracker.condition or {}
        if cond.get("type"):
            bits.append(f"Condition: {cond['type']}.")
        if res is not None:
            bits.append(f"Result: {res.reason}.")
        return " ".join(bits)

    def why(self, tracker_id: int) -> dict[str, Any]:
        t = self.get(tracker_id)
        if t is None:
            return {"ok": False, "error": "tracker not found"}
        last = self.db.one(
            "SELECT * FROM tracker_events WHERE tracker_id=? ORDER BY id DESC "
            "LIMIT 1", (t.id,))
        return {"ok": True, "tracker": t.to_dict(),
                "last_event": dict(last) if last else None,
                "explanation": self.explain_tracker(t)}

    # ------------------------------------------------------------- audit
    def _audit(self, action: str, tracker: Tracker | None,
               detail: Any = None) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action, actor="tracker",
                              target=f"tracker:{tracker.id if tracker else 0}",
                              decision="allowed", outcome="ok",
                              detail=detail or {})
        except Exception:  # noqa: BLE001
            log.debug("tracker audit failed", exc_info=True)

    # ------------------------------------------------------------- NL parse
    def parse_request(self, text: str, *, context: dict[str, Any] | None = None,
                      now: int | None = None) -> dict[str, Any]:
        """Deterministically turn a natural-language request into a tracker."""
        now = int(now if now is not None else _now())
        raw = (text or "").strip()
        low = _norm(raw)
        context = context or {}
        questions: list[str] = []
        tracker: dict[str, Any] = {
            "source": "", "target_type": "", "target_id": 0, "target_ref": "",
            "condition": {}, "action": {"type": "NOTIFY_TOPIC", "params": {}},
            "cadence": 0, "scope": SCOPE_OBJECT, "priority": "medium",
            "one_shot": False, "expires_at": 0,
        }

        # destination from the current topic
        dest = {}
        if context.get("chat_id") is not None:
            dest = {"chat_id": int(context.get("chat_id") or 0),
                    "thread_id": int(context.get("thread_id") or 0),
                    "topic_id": int(context.get("topic_id") or 0)}
            tracker["destination"] = dest
            tracker["scope"] = SCOPE_TOPIC

        # priority
        if re.search(r"\b(critical|urgent|asap)", low):
            tracker["priority"] = "critical"
        elif re.search(r"\b(important|high priority)", low):
            tracker["priority"] = "high"

        # expiry
        exp = _parse_expiry(low, now)
        if exp:
            tracker["expires_at"] = exp

        # one-shot
        if re.search(r"\bonce\b|\bjust once\b|is published|becomes available|"
                     r"is released", low):
            tracker["one_shot"] = True

        # too broad?
        if re.search(r"\btrack everything\b|\btrack all\b|everything about|"
                     r"my university\b|\bthe world\b", low):
            questions.append("What specifically should I track?")

        # ---- target detection ----
        m = re.search(r"\b([A-Za-z]{2,6})\s?(\d{2,4})\b", raw)
        proj_matches = self._project_matches(raw)
        food_item, food_thr = _parse_food(low)

        if m and not re.search(r"github", low):
            code = (m.group(1) + m.group(2)).upper()
            tracker.update(source="course", target_type="course",
                           target_ref=code)
        elif re.search(r"\bgithub|repository|repo\b", low):
            repo = _parse_github_repo(raw)
            tracker.update(source="github", target_type="github_repo",
                           target_ref=repo or "")
            if not repo:
                questions.append("Which GitHub repository (owner/name)?")
        elif food_item:
            tracker.update(source="food", target_type="food_item",
                           target_ref=food_item)
            tracker["condition"] = {"type": "threshold_below", "item": food_item,
                                    "field": "quantity",
                                    "value": food_thr if food_thr is not None else 2}
            tracker["action"] = {"type": "CREATE_SUGGESTION",
                                 "params": {"items": [food_item]}}
        elif len(proj_matches) == 1:
            p = proj_matches[0]
            tracker.update(source="project_risk", target_type="project",
                           target_id=int(p["id"]), target_ref=str(p["name"]))
        elif len(proj_matches) > 1:
            questions.append("Which project — " + ", ".join(
                p["name"] for p in proj_matches[:4]) + "?")
            tracker.update(source="project_risk", target_type="project")
        elif re.search(r"\bflight|calendar|event|meeting|appointment\b", low):
            tracker.update(source="calendar", target_type="event",
                           target_ref=raw)
        elif re.search(r"\bannouncement|club|page|website|news\b", low):
            tracker.update(source="web", target_type="web_page")
            if not _extract_url(raw):
                questions.append("Which page or site should I watch?")
            else:
                tracker["target_ref"] = _extract_url(raw)
        elif re.search(r"\bassignment|deadline|course|class\b", low):
            tracker.update(source="course", target_type="course")
            questions.append("Which course?")
        else:
            questions.append("What should I track?")

        # ---- condition ----
        cond = tracker.get("condition") or {}
        if not cond:
            if re.search(r"\bdeadline", low):
                cond = {"type": "deadline_changed"}
            elif re.search(r"\bhigh risk|at risk|risk\b", low):
                cond = {"type": "risk_crossed_above", "value": 0.8}
            elif re.search(r"\bflight|change", low):
                cond = {"type": "field_changed", "field": "start"}
            elif re.search(r"\binactiv|no activity|quiet|stale", low):
                days = _parse_days(low) or 5
                cond = {"type": "no_activity", "days": days}
            elif re.search(r"\bannouncement|new|assignment|project\b", low):
                cond = {"type": "new_item"}
            else:
                cond = {"type": "new_item"}
            tracker["condition"] = cond

        # ---- cadence ----
        tracker["cadence"] = _parse_cadence(low) or _CADENCE_DEFAULTS.get(
            tracker["source"], 21600)

        # ---- scope ----
        if re.search(r"\bglobal|everywhere|all topics\b", low):
            tracker["scope"] = SCOPE_GLOBAL
        elif re.search(r"\bhere|this topic\b", low) and dest:
            tracker["scope"] = SCOPE_TOPIC

        tracker["name"] = self._name_for(tracker, raw)
        confidence = 0.9 if not questions else 0.5
        return {"ok": True, "tracker": tracker, "questions": questions,
                "confidence": confidence,
                "summary": self.proposal_summary(tracker, questions),
                "destination": dest}

    def _project_matches(self, raw: str) -> list[dict[str, Any]]:
        projects = getattr(self.container, "projects", None)
        if projects is None or not hasattr(projects, "list_projects"):
            return []
        text = _norm(raw)
        out = []
        for p in projects.list_projects():
            name = _norm(str(p.get("name") or ""))
            if name and (name in text or text in name):
                out.append(p)
        return out

    def _name_for(self, tracker: dict[str, Any], raw: str) -> str:
        ref = tracker.get("target_ref") or tracker.get("target_type") or "Tracker"
        cond = (tracker.get("condition") or {}).get("type", "changes")
        return f"{ref} {cond}".strip()[:60]

    def proposal_summary(self, tracker: dict[str, Any],
                         questions: list[str] | None = None) -> str:
        dest = tracker.get("destination") or {}
        dest_s = f"topic {dest.get('thread_id')}" if dest else "your main chat"
        cad = tracker.get("cadence") or 0
        cad_s = f"every {cad // 3600}h" if cad >= 3600 else f"every {cad // 60}m"
        lines = [f"Track: {tracker.get('target_ref') or tracker.get('target_type')}",
                 f"Source: {tracker.get('source')}",
                 f"Watch: {(tracker.get('condition') or {}).get('type')}",
                 f"Check: {cad_s}",
                 f"Notify: {dest_s}"]
        if questions:
            lines.append("Needs: " + "; ".join(questions))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# NL helpers
# ---------------------------------------------------------------------------
_COURSE_RE = re.compile(r"\b([A-Za-z]{2,6})\s?(\d{2,4})\b")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_GH_RE = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\b")


def _extract_url(text: str) -> str:
    m = _URL_RE.search(text or "")
    return m.group(0) if m else ""


def _parse_github_repo(text: str) -> str:
    m = _GH_RE.search(text or "")
    if m and "/" in m.group(1):
        return m.group(1)
    return ""


def _parse_food(low: str) -> tuple[str, float | None]:
    """Extract a food item + optional threshold from 'tell me when X is low'."""
    thr = None
    m = re.search(r"(?:below|under|less than)\s+(\d+(?:\.\d+)?)", low)
    if m:
        thr = float(m.group(1))
    m2 = re.search(
        r"(?:when|if|tell me when|notify me when|warn me if)?\s*(?:my\s+)?"
        r"([a-z][a-z ]{1,30}?)\s+(?:gets?|is|are|runs?|falls?|drops?)\s+"
        r"(?:low|below|under|out|short|empty)", low)
    item = m2.group(1).strip() if m2 else ""
    if not item:
        m3 = re.search(r"(?:low on|out of|running out of)\s+([a-z][a-z ]{1,30})",
                       low)
        item = m3.group(1).strip() if m3 else ""
    if not item and ("low stock" in low or "stock" in low):
        m4 = re.search(r"([a-z][a-z ]{1,30}?)\s+(?:low stock|stock)", low)
        item = m4.group(1).strip() if m4 else ""
    # only treat as food when there is an explicit low/stock/threshold signal
    if not (thr is not None or item):
        return "", None
    return item, thr


def _parse_cadence(low: str) -> int:
    if re.search(r"\bhourly\b", low):
        return 3600
    if re.search(r"\bdaily\b", low):
        return 86400
    if re.search(r"\bweekly\b", low):
        return 7 * 86400
    m = re.search(r"every\s+(\d+)\s*(hour|hr|h|minute|min|m|day|d)", low)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            return n * 3600
        if unit.startswith("d"):
            return n * 86400
        return max(60, n * 60)
    return 0


def _parse_days(low: str) -> int | None:
    m = re.search(r"(\d+)\s*days?", low)
    return int(m.group(1)) if m else None


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")


def _parse_expiry(low: str, now: int) -> int:
    m = re.search(r"until\s+(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        try:
            return int(datetime(int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), 23, 59).timestamp())
        except ValueError:
            return 0
    m = re.search(r"until\s+([a-z]+)", low)
    if m and m.group(1) in _WEEKDAYS:
        target = _WEEKDAYS.index(m.group(1))
        d = datetime.fromtimestamp(now)
        delta = (target - d.weekday()) % 7
        if delta == 0:
            delta = 7
        end = datetime(d.year, d.month, d.day, 23, 59)
        return int(end.timestamp()) + delta * 86400
    if "until tomorrow" in low:
        d = datetime.fromtimestamp(now)
        end = datetime(d.year, d.month, d.day, 23, 59)
        return int(end.timestamp()) + 86400
    return 0
