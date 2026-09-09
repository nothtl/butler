"""Phase 4.3: a lightweight, durable context timeline.

Records *meaningful* events deterministically so later phases can learn
routines (Phase 4.4) without any history scraping. Five families of events
are recorded, and only when they actually happen:

  * ``zone_change``       — the zone the user is in changed (HA presence);
  * ``calendar_start``    — a hard commitment begins;
  * ``calendar_end``      — a hard commitment ends;
  * ``task_started`` / ``task_completed`` — scheduler status transitions;
  * ``schedule_change``   — an explicit reschedule;
  * ``user_context``      — something the user told Butler (e.g. "I'm at X").

Design constraints that make this reliable and safe:

  * **DB is the single source of truth.** Everything is reproducible from
    HA/calendar/task state. DeepSeek is never asked to build the timeline.
  * **Zone-only.** Presence is reduced to a *zone name* before it ever reaches
    the store. Raw GPS coordinates are forbidden and never persisted.
  * **Nothing sensitive.** No HA tokens, credentials, or full API responses
    are written — only the resolved ``zone``, a ``title``, and a short note.
  * **Passive.** Recording never mutates the scheduler, calendar, or presence;
    it is an append-only audit trail.
  * **Idempotent.** Re-observing an unchanged state produces no duplicate
    event; a calendar sync seen twice is merged once; ``done`` on an already
    finished task is a no-op.
  * **Retention.** ``prune()`` drops events older than a configurable window
    and is safe to call repeatedly (and cheap, so it can run opportunistically).

The timeline exposes a small, human-readable view via ``timeline_text()`` and
a machine view via ``get_recent()`` / ``get_between()`` / ``current_context()``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

log = logging.getLogger("butler.timeline")

# Known event types.
ZONE_CHANGE = "zone_change"
CAL_START = "calendar_start"
CAL_END = "calendar_end"
TASK_STARTED = "task_started"
TASK_COMPLETED = "task_completed"
SCHEDULE_CHANGE = "schedule_change"
USER_CONTEXT = "user_context"

_LAST_ZONE_KEY = "last_zone"
_LAST_PRUNE_KEY = "last_prune"

# State names never taken as a real zone (HA already maps these to "").
_UNKNOWN = {"", "unknown", "unavailable", "none", "not_home", "nothome"}


def _now_ts() -> int:
    return int(datetime.now().timestamp())


class Timeline:
    """SQLite-backed context event store.

    Created by the ``Container`` so every subsystem shares one instance. All
    methods are safe under the DB write lock (``db.execute``/``db.query``).
    """

    def __init__(self, cfg: Any, db: Any):
        self.cfg = cfg
        self.db = db

    # ------------------------------------------------------------------ gating
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "timeline_enabled", True))

    # ---------------------------------------------------------------- presence
    def establish_zone(self, zone: str, ts: int | None = None,
                       source: str = "home_assistant") -> dict[str, Any] | None:
        """Record a known zone, returning the event only if it actually changed.

        This is the low-level recorder behind :meth:`observe_presence`. It
        compares against the *last known* zone (kept in ``tl_state``) and emits
        a ``zone_change`` only on a transition. It never stores GPS coordinates
        and never records an unknown/empty zone.
        """
        if not self.enabled():
            return None
        zone = (zone or "").strip()
        if zone.lower() in _UNKNOWN:
            return None
        last = self._get_state(_LAST_ZONE_KEY) or ""
        if (last or "").strip().lower() == zone.lower():
            return None
        ts = ts or _now_ts()
        event = self._insert(
            ts=ts, type=ZONE_CHANGE,
            zone_from=(last or "").strip() or "",
            zone_to=zone, source=source,
        )
        self._set_state(_LAST_ZONE_KEY, zone)
        return event

    def observe_presence(self, presence: dict[str, Any]) -> dict[str, Any] | None:
        """Turn an HA ``presence`` dict into a durable zone change if warranted.

        Presence with no usable zone (unknown/unavailable/HA outage) is ignored
        — it never corrupts the last-known-zone state, so a later recovery does
        not look like a spurious jump home. Idempotent: re-observing the same
        zone produces nothing.
        """
        if not self.enabled():
            return None
        zone = (presence.get("zone") or "").strip()
        known = bool(presence.get("known"))
        status = (presence.get("status") or "unknown").strip()
        if not known or not zone or zone.lower() in _UNKNOWN:
            return None
        return self.establish_zone(zone, source=presence.get("source", "home_assistant"))

    # ------------------------------------------------------------------- tasks
    def record_task_started(self, task_id: int, title: str, ts: int | None = None) -> dict[str, Any]:
        return self._record_task(task_id, title, TASK_STARTED, ts)

    def record_task_completed(self, task_id: int, title: str, ts: int | None = None) -> dict[str, Any]:
        return self._record_task(task_id, title, TASK_COMPLETED, ts)

    def _record_task(self, task_id: int, title: str, kind: str,
                     ts: int | None = None) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "error": "timeline disabled"}
        ext = f"task:{task_id}:{kind}"
        existing = self.db.one("SELECT id FROM timeline_events WHERE external_id=?", (ext,))
        if existing:
            return {"ok": False, "duplicate": True}
        ev = self._insert(ts=ts or _now_ts(), type=kind, external_id=ext,
                          title=title or "", source="scheduler")
        return {"ok": True, "event": ev}

    # ---------------------------------------------------------------- calendar
    def record_calendar_events(self, events: list[dict[str, Any]],
                               source: str = "google") -> int:
        """Record ``calendar_start``/``calendar_end`` for a batch of events.

        Each dict is ``{id, title, start_ts, end_ts, source}``. Events are
        merged idempotently by ``external_id`` (``cal:<id>:start`` /
        ``cal:<id>:end``), so resyncing the same window never duplicates rows.
        Returns the number of newly-inserted rows.
        """
        if not self.enabled():
            return 0
        added = 0
        for ev in events:
            eid = ev.get("external_id") or ev.get("id")
            title = str(ev.get("title") or "")
            src = ev.get("source") or source
            start = int(ev.get("start_ts") or 0)
            end = int(ev.get("end_ts") or 0)
            for kind, when in ((CAL_START, start), (CAL_END, end)):
                if not when:
                    continue
                ext = f"cal:{eid}:{kind}"
                if self.db.one("SELECT id FROM timeline_events WHERE external_id=?",
                               (ext,)):
                    continue
                self._insert(ts=when, type=kind, external_id=ext,
                             title=title, source=src, note="")
                added += 1
        return added

    # -------------------------------------------------------------- the rest
    def record_schedule_change(self, note: str = "", ts: int | None = None) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "error": "timeline disabled"}
        ev = self._insert(ts=ts or _now_ts(), type=SCHEDULE_CHANGE,
                          note=note or "", source="scheduler")
        return {"ok": True, "event": ev}

    def record_user_declaration(self, note: str, zone: str = "",
                                title: str | None = None, ts: int | None = None) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "error": "timeline disabled"}
        ev = self._insert(ts=ts or _now_ts(), type=USER_CONTEXT,
                          zone_to=(zone or "").strip() or None,
                          title=title or None, note=note or "", source="user")
        return {"ok": True, "event": ev}

    # ----------------------------------------------------------------- queries
    def get_recent(self, limit: int = 20, since: int | None = None) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM timeline_events "
            "WHERE (? IS NULL OR ts >= ?) ORDER BY ts DESC, id DESC LIMIT ?",
            (since, since, max(0, int(limit))))
        return [self._to_event(r) for r in rows]

    def get_between(self, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM timeline_events WHERE ts >= ? AND ts <= ? "
            "ORDER BY ts ASC, id ASC", (int(start_ts), int(end_ts)))
        return [self._to_event(r) for r in rows]

    def events(self) -> list[dict[str, Any]]:
        return self.get_recent(limit=500)

    def current_context(self) -> dict[str, Any]:
        """The *live* view: where the user is now, distinct from the history.

        ``zone`` is the last known zone (from ``tl_state``); it is *not* a
        derivation from historical events, so an empty history and an unchanged
        presence produce a stable answer. ``since`` is when that zone was
        entered (the timestamp of the most recent ``zone_change`` into it).
        """
        zone = (self._get_state(_LAST_ZONE_KEY) or "").strip()
        last = self.db.one(
            "SELECT ts FROM timeline_events WHERE type=? "
            "ORDER BY ts DESC, id DESC LIMIT 1",
            (ZONE_CHANGE,))
        return {
            "known": bool(zone),
            "zone": zone,
            "since": int(last["ts"]) if last else None,
            "at": datetime.fromtimestamp(int(last["ts"])).strftime("%A %H:%M")
            if last else None,
        }

    # ---------------------------------------------------------------- retention
    def prune(self, retention_days: int | None = None) -> int:
        """Delete events older than the retention window. Idempotent.

        ``retention_days = 0`` keeps everything. Returns rows removed.
        """
        days = int(getattr(self.cfg, "timeline_retention_days", 30)
                   if retention_days is None else retention_days)
        if days <= 0:
            return 0
        cutoff = _now_ts() - days * 86400
        cur = self.db.one(
            "SELECT COUNT(*) AS removed FROM timeline_events WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM timeline_events WHERE ts < ?", (cutoff,))
        self._set_state(_LAST_PRUNE_KEY, str(_now_ts()))
        return int(cur["removed"]) if cur else 0

    # -------------------------------------------------------------- rendering
    def timeline_text(self, events: list[dict[str, Any]] | None = None,
                      limit: int = 25) -> str:
        """A compact, human-readable timeline (e.g. for Telegram/CLI)."""
        events = events if events is not None else self.get_recent(limit=limit)
        if not events:
            return "Nothing recorded yet."
        lines = []
        for ev in events:
            when = datetime.fromtimestamp(int(ev["ts"])).strftime("%a %H:%M")
            lines.append(f"{when}  {self._render_event(ev)}")
        return "\n".join(lines)

    def _render_event(self, ev: dict[str, Any]) -> str:
        kind = ev["type"]
        title = (ev.get("title") or "").strip()
        note = (ev.get("note") or "").strip()
        if kind == ZONE_CHANGE:
            frm = (ev.get("zone_from") or "").strip()
            to = (ev.get("zone_to") or "").strip()
            if frm and to:
                return f"moved {frm} → {to}"
            return f"at {to}"
        if kind == TASK_STARTED:
            return f"started work on \"{title or 'task'}\""
        if kind == TASK_COMPLETED:
            return f"finished \"{title or 'task'}\""
        if kind == CAL_START:
            return f"\"{title or 'event'}\" begins"
        if kind == CAL_END:
            return f"\"{title or 'event'}\" ends"
        if kind == SCHEDULE_CHANGE:
            return "schedule changed" + (f" ({note})" if note else "")
        if kind == USER_CONTEXT:
            return note or title or "user note"
        return kind

    # ------------------------------------------------------------- low-level
    def _insert(self, ts: int, type: str, zone_from: str | None = None,
                zone_to: str | None = None, source: str | None = None,
                external_id: str | None = None, title: str | None = None,
                note: str | None = None) -> dict[str, Any]:
        cur = self.db.execute(
            "INSERT INTO timeline_events(ts, type, zone_from, zone_to, source, "
            "external_id, title, note) VALUES(?,?,?,?,?,?,?,?)",
            (int(ts), type, zone_from, zone_to, source, external_id, title, note))
        return self._to_event(self.db.one(
            "SELECT * FROM timeline_events WHERE id=?", (cur.lastrowid,)))

    def _to_event(self, row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "ts": int(row["ts"]),
            "type": str(row["type"]),
            "zone_from": row["zone_from"],
            "zone_to": row["zone_to"],
            "source": str(row["source"] or ""),
            "external_id": str(row["external_id"] or ""),
            "title": str(row["title"] or ""),
            "note": str(row["note"] or ""),
        }

    def _get_state(self, key: str) -> str | None:
        row = self.db.one("SELECT value FROM tl_state WHERE key=?", (key,))
        return str(row["value"]) if row else None

    def _set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO tl_state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))
