"""Phase 4.4: learned routines and habits.

Detects *recurring* behaviour deterministically from the context timeline
(Phase 4.3) and turns any confirmed pattern into a *soft* planning preference.

The whole point is restraint:

  * **Deterministic & explainable.** Pattern detection is pure Python over the
    timeline. DeepSeek is never used. Confidence is a transparent product of
    three terms (count, consistency, recency) you can reproduce by hand.
  * **Soft only.** A confirmed routine only nudges recommendation affinity
    (capped at ``routines_affinity_max``) so explicit user preferences (``+/-6``,
    ``+/-8``) always win. It never rewrites a committed plan and never overrides
    a hard constraint — Google Calendar / deadlines / sleep still win.
  * **Confirm before you trust.** Inferred patterns start as a ``candidate``.
    Only after the user confirms do they affect recommendation. A one-off
    observation never becomes a routine.
  * **Lifecycle.** candidate -> confirmed (or declined). The user can forget
    (disable) a routine; uncontacted ones decay to ``stale`` but their
    observations are kept so a returning habit can re-surface.

Privacy: routines are built from zone names and task titles only — never raw
GPS, never a raw timeline passed to the LLM.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from . import affinity
from .timeline import (
    ZONE_CHANGE,
    TASK_COMPLETED,
    USER_CONTEXT,
)

log = logging.getLogger("butler.routines")

# Routine kinds.
ACTIVITY = "activity"
SEQUENCE = "sequence"

# Routine states (lifecycle).
CANDIDATE = "candidate"
CONFIRMED = "confirmed"
DECLINED = "declined"
DISABLED = "disabled"
STALE = "stale"

_ACTIVE_STATES = (CONFIRMED,)
_ANY_WEEKDAY = -1

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
         "Saturday", "Sunday")

# Human labels for the coarse affinity categories.
_CAT_LABEL = {
    "study": "study",
    "exercise": "exercise",
    "cook": "cooking",
    "shop": "shopping",
    "rest": "rest",
    "work": "work",
}

_DAY_WORDS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Category words for explicit routine creation / disabling.
_CAT_WORDS = {
    "study": ("study", "studying", "library", "read", "homework", "lecture", "class"),
    "exercise": ("gym", "workout", "exercise", "run", "jog", "swim", "yoga", "basketball"),
    "cook": ("cook", "cooking", "dinner", "lunch", "meal", "bake"),
    "shop": ("shop", "shopping", "grocery", "groceries", "store", "market", "errand"),
    "rest": ("rest", "nap", "break", "relax", "chill", "sleep"),
    "work": ("work", "meet", "meeting", "desk", "office", "email", "lab"),
}


def _hm(minutes: int) -> str:
    minutes = int(minutes)
    h, m = divmod(minutes, 60)
    return f"{h:02d}:{m:02d}"


def _mode(seq: list[str]) -> str:
    if not seq:
        return ""
    counts: dict[str, int] = {}
    order: list[str] = []
    for x in seq:
        if x not in counts:
            order.append(x)
        counts[x] = counts.get(x, 0) + 1
    best = max(order, key=lambda x: (counts[x], -order.index(x)))
    return best


def _recency_factor(last_ts: int, now: int, stale_days: int) -> float:
    age = (int(now) - int(last_ts)) / 86400.0
    if age <= stale_days:
        return 1.0
    if age >= 2 * stale_days:
        return 0.2
    return 1.0 - 0.8 * (age - stale_days) / stale_days


def _confidence(count: int, spread: int, last_ts: int, now: int,
                max_obs: int, tol: int, stale_days: int) -> float:
    count_ratio = min(1.0, count / max(1, max_obs))          # how often seen
    consistency = 1.0 - (max(0, spread) / max(1, tol))        # how punctual
    consistency = max(0.0, min(1.0, consistency))
    recency = _recency_factor(last_ts, now, stale_days)       # how current
    return round(count_ratio * consistency * recency, 2)


class Routines:
    """Learned-routine store + deterministic detector.

    Constructed by the ``Container`` with access to the shared ``timeline`` and
    ``db``. All scan/confirm/reject/forget calls are idempotent and safe under
    the DB write lock.
    """

    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.timeline = getattr(container, "timeline", None)

    # ------------------------------------------------------------------ config
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "routines_enabled", True))

    def _min_obs(self) -> int:
        return int(getattr(self.cfg, "routines_min_observations", 3))

    def _confidence_min(self) -> float:
        return float(getattr(self.cfg, "routines_confidence_min", 0.5))

    def _tol(self) -> int:
        return int(getattr(self.cfg, "routines_time_tolerance", 90))

    def _gap(self) -> int:
        return int(getattr(self.cfg, "routines_gap_minutes", 90))

    def _scan_days(self) -> int:
        return int(getattr(self.cfg, "routines_scan_days", 90))

    def _stale_days(self) -> int:
        return int(getattr(self.cfg, "routines_stale_days", 21))

    def _max_obs(self) -> int:
        return int(getattr(self.cfg, "routines_max_observations", 5))

    def _affinity_max(self) -> int:
        return int(getattr(self.cfg, "routines_affinity_max", 4))

    # -------------------------------------------------------------- detection
    def _observe(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Reduce one timeline event to a scannable observation, or ``None``."""
        kind = event.get("type")
        ts = int(event.get("ts") or 0)
        if not ts:
            return None
        d = datetime.fromtimestamp(ts)
        cat = ""
        zone = ""
        title = (event.get("title") or "").strip()
        if kind == ZONE_CHANGE:
            zone = (event.get("zone_to") or "").strip()
            zw = affinity.zone_weights_for(zone)
            if not zw:
                return None
            cat = _mode(list(zw.keys()))
            if not cat:
                return None
        elif kind == TASK_COMPLETED:
            if not title:
                return None
            cats = affinity.classify(title)
            if not cats:
                return None
            cat = min(cats)
            if cat == "work" and not title:
                return None
            zone = ""  # task completion doesn't carry a zone
        elif kind == USER_CONTEXT:
            note = (event.get("note") or "").strip()
            if not zone:
                zone = (event.get("zone_to") or "").strip()
            if zone:
                zw = affinity.zone_weights_for(zone)
                cat = _mode(list(zw.keys())) if zw else ""
            elif title or note:
                cats = affinity.classify(title or note)
                if not cats:
                    return None
                cat = min(cats)
            else:
                return None
        else:
            return None
        if not cat:
            return None
        return {
            "ts": ts,
            "weekday": d.weekday(),
            "minute": d.hour * 60 + d.minute,
            "category": cat,
            "zone": zone,
            "week": d.isocalendar()[:2],
            "date": (d.year, d.month, d.day),
        }

    def detect(self, events: list[dict[str, Any]],
               now: int | None = None) -> list[dict[str, Any]]:
        """Detect candidate routines from a chronological list of events.

        Deterministic, no LLM. Returns candidate descriptors (``kind``,
        ``category``, ``zone``, ``weekday``, ``start_min``/``end_min``,
        ``confidence``, ``count``, ``weeks``, ``first_ts``, ``last_ts``,
        ``title``). A pattern must clear ``routines_min_observations`` and
        ``routines_confidence_min``; a single occurrence never qualifies.
        """
        if not self.enabled():
            return []
        now = now or int(datetime.now().timestamp())
        obs = [o for o in (self._observe(e) for e in events) if o]
        obs = _dedupe_obs(obs)
        cands = self._detect_single(obs, now)
        cands += self._detect_sequence(obs, now)
        return cands

    def _detect_single(self, obs: list[dict[str, Any]],
                       now: int) -> list[dict[str, Any]]:
        min_obs = self._min_obs()
        tol = self._tol()
        max_obs = self._max_obs()
        groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for o in obs:
            groups.setdefault((o["weekday"], o["category"]), []).append(o)

        out: list[dict[str, Any]] = []
        for (wd, cat), items in sorted(groups.items()):
            if len(items) < min_obs:
                continue
            minutes = sorted(o["minute"] for o in items)
            spread = minutes[-1] - minutes[0]
            if spread > tol:
                continue
            zones = [o["zone"] for o in items if o["zone"]]
            zone = _mode(zones) if zones else ""
            weeks = {o["week"] for o in items}
            confidence = _confidence(len(items), spread, max(o["ts"] for o in items),
                                     now, max_obs, tol, self._stale_days())
            if confidence < self._confidence_min():
                continue
            out.append({
                "kind": ACTIVITY,
                "category": cat,
                "zone": zone,
                "weekday": wd,
                "start_min": minutes[0],
                "end_min": minutes[-1],
                "title": zone or _CAT_LABEL.get(cat, cat),
                "count": len(items),
                "weeks": len(weeks),
                "n_of_m": min(len(items), max_obs),
                "confidence": confidence,
                "first_ts": min(o["ts"] for o in items),
                "last_ts": max(o["ts"] for o in items),
            })
        return out

    def _detect_sequence(self, obs: list[dict[str, Any]],
                         now: int) -> list[dict[str, Any]]:
        min_obs = self._min_obs()
        gap = self._gap()
        max_obs = self._max_obs()
        tol = self._tol()
        by_date: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
        for o in obs:
            by_date.setdefault(o["date"], []).append(o)

        pairs: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
        for items in by_date.values():
            items = sorted(items, key=lambda o: o["ts"])
            for i in range(len(items)):
                a = items[i]
                for j in range(i + 1, len(items)):
                    b = items[j]
                    d = b["minute"] - a["minute"]
                    if d <= 0 or d > gap:
                        continue
                    if b["category"] == a["category"]:
                        continue
                    key = (a["weekday"], a["category"], b["category"])
                    pairs.setdefault(key, []).append({
                        "a": a, "b": b, "gap": d, "week": a["week"],
                        "date": a["date"], "minute": a["minute"],
                    })

        out: list[dict[str, Any]] = []
        for (wd, acat, bcat), pair_obs in sorted(pairs.items()):
            if len(pair_obs) < min_obs:
                continue
            weeks = {p["week"] for p in pair_obs}
            conf = _confidence(len(pair_obs), 0, max(p["a"]["ts"] for p in pair_obs),
                               now, max_obs, max(1, tol), self._stale_days())
            if conf < self._confidence_min():
                continue
            a_min = sorted(p["minute"] for p in pair_obs)
            b_zones = [p["b"]["zone"] for p in pair_obs if p["b"]["zone"]]
            out.append({
                "kind": SEQUENCE,
                "category": f"{acat}->{bcat}",
                "zone": _mode(b_zones) if b_zones else "",
                "weekday": wd,
                "start_min": a_min[0],
                "end_min": a_min[-1] + 60,
                "title": f"{_CAT_LABEL.get(acat, acat)} → {_CAT_LABEL.get(bcat, bcat)}",
                "count": len(pair_obs),
                "weeks": len(weeks),
                "n_of_m": min(len(pair_obs), max_obs),
                "confidence": conf,
                "first_ts": min(p["a"]["ts"] for p in pair_obs),
                "last_ts": max(p["b"]["ts"] for p in pair_obs),
            })
        return out

    # ----------------------------------------------------------------- scanning
    def scan(self) -> dict[str, Any]:
        """Scan the timeline, upserting candidates. Returns surfacings + counts."""
        if not self.enabled() or self.timeline is None:
            return {"candidates": [], "new": 0, "active": 0, "scanned": 0}
        now = int(datetime.now().timestamp())
        end = now
        start = now - self._scan_days() * 86400
        events = self.timeline.get_between(start, end)
        cands = self.detect(events, now)
        created = 0
        for c in cands:
            sig = self._sig(c)
            row = self.db.one(
                "SELECT * FROM routines WHERE signature=?", (sig,))
            if row is None:
                self._insert(c, sig)
                created += 1
            else:
                self._refresh(row, c)
        return {
            "candidates": [dict(r) for r in self.db.query(
                "SELECT * FROM routines WHERE state=? ORDER BY confidence DESC, id DESC",
                (CANDIDATE,))],
            "new": created,
            "active": self._count_state(CONFIRMED),
            "scanned": len(events),
        }

    def _sig(self, c: dict[str, Any]) -> str:
        bucket = int(c["start_min"] // 60)
        return f"{c['kind']}:{c['category']}:{c.get('zone', '')}:{c['weekday']}:{bucket}"

    def _insert(self, c: dict[str, Any], sig: str) -> None:
        now = int(datetime.now().timestamp())
        self.db.execute(
            "INSERT INTO routines(kind, category, zone, weekday, start_min, end_min, "
            "title, count, weeks, n_of_m, confidence, first_ts, last_ts, state, source, "
            "created_at, updated_at, signature) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["kind"], c["category"], c.get("zone", ""), c["weekday"], c["start_min"],
             c["end_min"], c.get("title", ""), c["count"], c["weeks"], c["n_of_m"],
             c["confidence"], c["first_ts"], c["last_ts"], CANDIDATE, "inferred",
             now, now, sig))

    def _refresh(self, row: Any, c: dict[str, Any]) -> None:
        """Rebase a known activity/sequence with the freshest observed stats.

        Declined/disabled routines stay declined/disabled (user intent wins);
        a confirmed one simply stays confirmed with updated numbers.
        """
        if row["state"] in (DECLINED, DISABLED):
            return
        new_state = CONFIRMED if row["state"] == CONFIRMED else CANDIDATE
        self.db.execute(
            "UPDATE routines SET count=?, weeks=?, n_of_m=?, confidence=?, "
            "first_ts=?, last_ts=?, end_min=?, state=?, updated_at=? WHERE id=?",
            (c["count"], c["weeks"], c["n_of_m"], c["confidence"], c["first_ts"],
             c["last_ts"], c["end_min"], new_state,
             int(datetime.now().timestamp()), row["id"]))

    # ----------------------------------------------------------------- queries
    def _count_state(self, state: str) -> int:
        row = self.db.one(
            "SELECT COUNT(*) AS n FROM routines WHERE state=?", (state,))
        return int(row["n"]) if row else 0

    def rows(self, state: str | None = None) -> list[dict[str, Any]]:
        if state is None:
            return [dict(r) for r in self.db.query(
                "SELECT * FROM routines ORDER BY state, confidence DESC, id DESC")]
        return [dict(r) for r in self.db.query(
            "SELECT * FROM routines WHERE state=? ORDER BY confidence DESC, id DESC",
            (state,))]

    def candidates(self) -> list[dict[str, Any]]:
        return self.rows(CANDIDATE)

    def confirmed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows(CONFIRMED)]

    def active(self) -> list[dict[str, Any]]:
        rows = self.confirmed()
        now = int(datetime.now().timestamp())
        return [_row(r) for r in rows if self._is_live(r, now)]

    # --------------------------------------------------------------- lifecycle
    def _target(self, routine_id: int | None, state: str) -> dict[str, Any] | None:
        if routine_id:
            return self.db.one(
                "SELECT * FROM routines WHERE id=? AND state=?", (int(routine_id), state))
        return self.db.one(
            "SELECT * FROM routines WHERE state=? ORDER BY id DESC LIMIT 1", (state,))

    def confirm(self, routine_id: int | None = None) -> dict[str, Any]:
        row = self._target(routine_id, CANDIDATE)
        if not row:
            return {"ok": False, "error": "no candidate routine to confirm"}
        self.db.execute(
            "UPDATE routines SET state=?, updated_at=? WHERE id=?",
            (CONFIRMED, int(datetime.now().timestamp()), row["id"]))
        return {"ok": True, "routine": _row(self.db.one(
            "SELECT * FROM routines WHERE id=?", (row["id"],)))}

    def reject(self, routine_id: int | None = None) -> dict[str, Any]:
        row = self._target(routine_id, CANDIDATE)
        if not row:
            return {"ok": False, "error": "no candidate routine to reject"}
        self.db.execute(
            "UPDATE routines SET state=?, updated_at=? WHERE id=?",
            (DECLINED, int(datetime.now().timestamp()), row["id"]))
        return {"ok": True, "routine": _row(self.db.one(
            "SELECT * FROM routines WHERE id=?", (row["id"],)))}

    def forget(self, routine_id: int | None = None) -> dict[str, Any]:
        """Disable an active routine (the user no longer wants it)."""
        row = self._target(routine_id, CONFIRMED)
        if not row:
            return {"ok": False, "error": "no active routine to forget"}
        self.db.execute(
            "UPDATE routines SET state=?, updated_at=? WHERE id=?",
            (DISABLED, int(datetime.now().timestamp()), row["id"]))
        return {"ok": True, "routine": _row(self.db.one(
            "SELECT * FROM routines WHERE id=?", (row["id"],)))}

    def decay(self) -> int:
        """Mark long-untouched confirmed routines as stale. Keeps observations."""
        now = int(datetime.now().timestamp())
        cutoff = now - self._stale_days() * 86400
        cur = self.db.execute(
            "UPDATE routines SET state=?, updated_at=? "
            "WHERE state=? AND last_ts < ?",
            (STALE, now, CONFIRMED, cutoff))
        return int(cur.rowcount or 0)

    def reactivate(self, routine_id: int | None = None) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM routines WHERE id=? AND state=?",
            (int(routine_id), STALE)) if routine_id else self.db.one(
            "SELECT * FROM routines WHERE state=? ORDER BY id DESC LIMIT 1", (STALE,))
        if not row:
            return {"ok": False, "error": "no stale routine to reactivate"}
        self.db.execute(
            "UPDATE routines SET state=?, updated_at=? WHERE id=?",
            (CONFIRMED, int(datetime.now().timestamp()), row["id"]))
        return {"ok": True, "routine": _row(self.db.one(
            "SELECT * FROM routines WHERE id=?", (row["id"],)))}

    def _is_live(self, row: dict[str, Any], now: int) -> bool:
        return (int(row["last_ts"]) + self._stale_days() * 86400) >= now

    # ------------------------------------------------------------- explicit
    def create_explicit(self, message: str) -> dict[str, Any]:
        """Turn an explicit user statement into a confirmed routine (soft).

        Supports "always <X> on <day>", "always <X> after <Y>", and negative
        statements like "I don't want to go to the gym on Mondays anymore"
        which instead disable the matching confirmed routine. Returns a result
        with ``action`` in {disable, create, none}.
        """
        text = (message or "").lower()
        day = self._find_day(text)
        cat = self._find_category(text)

        negative = bool(_match(r"(don'?t|no longer|not anymore|never|stop)\b", text))
        if negative:
            if cat:
                rm = self._disable_matching(cat, day)
                if rm:
                    return {"ok": True, "action": "disable", "routine": rm}
            return {"ok": False, "action": "none",
                    "error": "no matching routine to disable"}
        if not cat:
            return {"ok": False, "action": "none", "error": "could not parse the routine"}

        # "always X after Y" -> sequence (trigger -> target), else activity.
        after = self._after_cat(text)
        zone = ""
        if after and after != cat:
            kind = SEQUENCE
            cat_label = f"{after}->{cat}"
            title = f"{_CAT_LABEL.get(after, after)} → {_CAT_LABEL.get(cat, cat)}"
        else:
            kind = ACTIVITY
            cat_label = cat
            title = _CAT_LABEL.get(cat, cat)
        sig = f"{kind}:{cat_label}:{zone}:{day}:{17}"
        existing = self.db.one("SELECT * FROM routines WHERE signature=?", (sig,))
        if existing:
            self.db.execute(
                "UPDATE routines SET state=?, source=?, note=?, confidence=1.0, "
                "updated_at=? WHERE id=?",
                (CONFIRMED, "explicit", message, int(datetime.now().timestamp()),
                 existing["id"]))
            return {"ok": True, "action": "create", "routine": _row(self.db.one(
                "SELECT * FROM routines WHERE id=?", (existing["id"],)))}
        now = int(datetime.now().timestamp())
        self.db.execute(
            "INSERT INTO routines(kind, category, zone, weekday, start_min, end_min, "
            "title, count, weeks, n_of_m, confidence, first_ts, last_ts, state, source, "
            "note, created_at, updated_at, signature) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (kind, cat_label, zone, day, 17 * 60, 18 * 60, title, 1, 1, 1, 1.0, now, now,
             CONFIRMED, "explicit", message, now, now, sig))
        return {"ok": True, "action": "create", "routine": _row(self.db.one(
            "SELECT * FROM routines WHERE signature=?", (sig,)))}

    def _disable_matching(self, cat: str, day: int) -> dict[str, Any] | None:
        rows = self.confirmed()
        for r in rows:
            if r["kind"] != ACTIVITY:
                continue
            if r["category"] != cat:
                continue
            if day != _ANY_WEEKDAY and r["weekday"] != day:
                continue
            self.db.execute(
                "UPDATE routines SET state=?, updated_at=? WHERE id=?",
                (DISABLED, int(datetime.now().timestamp()), r["id"]))
            return _row(self.db.one("SELECT * FROM routines WHERE id=?", (r["id"],)))
        return None

    def _find_day(self, text: str) -> int:
        m = _match(r"\b(monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|"
                   r"friday|fri|saturday|sat|sunday|sun)\b", text)
        if m:
            return _DAY_WORDS[m.group(1)]
        if _match(r"\b(every day|daily|each day|all days)\b", text):
            return _ANY_WEEKDAY
        return _ANY_WEEKDAY

    def _find_category(self, text: str) -> str:
        for cat, words in _CAT_WORDS.items():
            if any(w in text for w in words):
                return cat
        return ""

    def _after_cat(self, text: str) -> str:
        m = _match(r"\bafter\s+(?:the\s+)?([a-z]+)", text)
        if not m:
            return ""
        return self._find_category(m.group(1))

    # ---------------------------------------------------------------- affinity
    def affinity_for(self, title: str = "", tags: str = "", day_ts: int = 0,
                     now_min: int = 0, zone: str = "",
                     recent: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """The soft boost a confirmed routine gives this task right now.

        Matches on weekday, time window, category and (when known) zone. Reserves
        the routine to be a *preference*: the returned ``score`` is capped at
        ``routines_affinity_max`` so explicit user weights (``+/-6``, ``+/-8``)
        always out-rank it. ``recent`` is a chronological slice of today's
        timeline used for sequence triggers.
        """
        empty = {"score": 0, "routine": None, "reason": ""}
        if not self.enabled():
            return empty
        if not day_ts:
            return empty
        wd = datetime.fromtimestamp(int(day_ts)).weekday()
        cats = affinity.classify(title, tags)
        tol = self._tol()
        best = None
        best_score = 0

        for r in self.confirmed():
            if not self._is_live(r, int(datetime.now().timestamp())):
                continue
            if r["kind"] == ACTIVITY:
                if r["weekday"] not in (_ANY_WEEKDAY, wd):
                    continue
                in_window = (int(r["start_min"]) - tol <= now_min
                             <= int(r["end_min"]) + tol)
                if not in_window:
                    continue
                if r["category"] not in cats:
                    continue
                if r["zone"] and zone and r["zone"].lower() not in (zone or "").lower():
                    continue
                score = self._boost(r["confidence"])
                if score > best_score:
                    best_score, best = score, r
            elif r["kind"] == SEQUENCE:
                acat, bcat = (r["category"] or "").split("->", 1)
                if bcat not in cats and not (r["zone"] and zone
                                             and r["zone"].lower() in (zone or "").lower()):
                    continue
                if r["weekday"] not in (_ANY_WEEKDAY, wd):
                    continue
                if recent and not self._trigger_present(recent, acat, wd, now_min):
                    continue
                score = self._boost(r["confidence"])
                if score > best_score:
                    best_score, best = score, r

        if not best:
            return empty
        cap = self._affinity_max()
        best_score = max(0, min(cap, best_score))
        return {"score": best_score, "routine": _row(best),
                "reason": self._reason(best, now_min)}

    def _boost(self, confidence: float) -> int:
        # map confidence 0..1 -> 1..affinity_max (rounded), so a mid-confidence
        # routine still nudges but a strong one nudges more.
        return max(1, min(self._affinity_max(), round(confidence * 5)))

    def _trigger_present(self, recent: list[dict[str, Any]], acat: str,
                         wd: int, now_min: int) -> bool:
        gap = self._gap()
        for ev in recent:
            obs = self._observe(ev)
            if not obs:
                continue
            if obs["weekday"] != wd:
                continue
            if obs["category"] != acat:
                continue
            d = now_min - obs["minute"]
            if 0 <= d <= gap:
                return True
        return False

    def _reason(self, r: dict[str, Any], now_min: int) -> str:
        if r["kind"] == SEQUENCE:
            acat, bcat = (r["category"] or "").split("->", 1)
            day = _DAYS[r["weekday"]] if r["weekday"] != _ANY_WEEKDAY else ""
            head = f"Because you usually {_CAT_LABEL.get(bcat, bcat)} after " \
                   f"{_CAT_LABEL.get(acat, acat)} around this time"
            return head + (f" on {day}s" if day else "")
        label = r["zone"] or _CAT_LABEL.get(r["category"], r["category"])
        where = f" at {r['zone']}" if r["zone"] else ""
        day = _DAYS[r["weekday"]] if r["weekday"] != _ANY_WEEKDAY else ""
        freq = (f" ({r['n_of_m']} of the last {r['count']} {day}s)" if day
                else f" ({r['count']} observation"
                      f"{'s' if r['count'] != 1 else ''})")
        return (f"Because you usually {label}{where} around {_hm(now_min)}{freq}")

    # --------------------------------------------------------------- rendering
    def show(self) -> str:
        rows = self.active()
        if not rows:
            return ("You don't have any active routines yet. I keep an eye on "
                    "your routine activity and suggest patterns once I'm sure.")
        lines = ["Your routines:"]
        for i, r in enumerate(rows, 1):
            day = _DAYS[r["weekday"]] if r["weekday"] != _ANY_WEEKDAY else "any day"
            lines.append(f"{i}. {r['title']} — {day}, ~{_hm(r['start_min'])} "
                         f"(confidence {int(r['confidence'] * 100)}%)")
        return "\n".join(lines)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Routines(enabled={self.enabled()})"


# --------------------------------------------------------------- helpers
def _dedupe_obs(obs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count each (date, category) once so a recurring pattern needs evidence
    spread across *distinct days*, not several events crammed into one day.

    A single busy morning (e.g. a zone change, a task completion and a context
    note all at the same minute for the same category) must not vote a
    routine. Otherwise ``routines_min_observations`` counts events, not days,
    and a one-day flurry produces ``count`` as high as the event volume while
    ``weeks`` stays 1.
    """
    seen: dict[tuple[tuple[int, int, int], str], dict[str, Any]] = {}
    for o in obs:
        key = (o["date"], o["category"])
        if key not in seen:
            seen[key] = o
    return list(seen.values())


def _match(pattern: str, text: str):
    import re
    return re.search(pattern, text)


def _row(r: Any) -> dict[str, Any]:
    return dict(r)
