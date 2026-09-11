"""Phase 7 / M2: deterministic temporal resolution.

A user phrase like "tonight", "before class" or "in two hours" must become a
typed :class:`~butler.agent.semantic.TemporalRange` **without guessing**. This
module resolves the phrases we can resolve safely, marks best-effort anchors as
``inferred`` (soft), and leaves everything else ``unresolved`` so the caller asks
instead of inventing a time.

Timezone handling is explicit and DST-safe: wall-clock boundaries are converted
through the configured ``zoneinfo`` timezone (or the host local zone when unset),
never by adding a flat 86 400 seconds across a DST transition.

No LLM is involved. An LLM may later *propose* a phrase, but the arithmetic here
is the deterministic authority.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

from .semantic import Scope, ScopeKind, TemporalRange, TemporalResolution

# ---------------------------------------------------------------------------
# number words (bounded; anything larger falls back to digits)
# ---------------------------------------------------------------------------

_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30, "forty": 40, "fortyfive": 45, "fifty": 50,
    "sixty": 60, "an": 1, "a": 1,
}

_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")
_CLOCK_RE = re.compile(
    r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)


def _number(token: str) -> float | None:
    token = token.strip().lower().replace("-", "")
    if not token:
        return None
    if token in _WORDS:
        return float(_WORDS[token])
    try:
        return float(token)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# clock
# ---------------------------------------------------------------------------


class Clock:
    """A timezone-aware clock that can be frozen for tests."""

    def __init__(self, tz: Any = None, now_ts: int | None = None):
        self._tz = tz
        self._fixed = now_ts

    @classmethod
    def from_config(cls, cfg: Any, now_ts: int | None = None) -> "Clock":
        tz = cfg.tz() if hasattr(cfg, "tz") else None
        return cls(tz=tz, now_ts=now_ts)

    @property
    def tz(self) -> Any:
        return self._tz

    def now_ts(self) -> int:
        return int(self._fixed) if self._fixed is not None else int(time.time())

    def now(self) -> datetime:
        if self._tz is None:
            return datetime.fromtimestamp(self.now_ts())
        return datetime.fromtimestamp(self.now_ts(), self._tz)

    def local(self, ts: int) -> datetime:
        if self._tz is None:
            return datetime.fromtimestamp(ts)
        return datetime.fromtimestamp(ts, self._tz)

    def midnight(self, ts: int) -> int:
        dt = self.local(ts)
        return self.wall(dt.year, dt.month, dt.day)

    def wall(self, year: int, month: int, day: int,
             hour: int = 0, minute: int = 0) -> int:
        """Local wall-clock -> epoch (DST-safe)."""
        naive = datetime(year, month, day, hour, minute)
        if self._tz is None:
            return int(naive.timestamp())
        return int(naive.replace(tzinfo=self._tz).timestamp())

    def add_days(self, ts: int, days: int) -> int:
        """Calendar-aware day shift (never a flat 86 400 s)."""
        dt = self.local(self.midnight(ts)) + timedelta(days=int(days))
        return self.wall(dt.year, dt.month, dt.day)

    def at_minute(self, day_ts: int, minute_of_day: int) -> int:
        dt = self.local(self.midnight(day_ts))
        return self.wall(dt.year, dt.month, dt.day,
                         hour=int(minute_of_day) // 60,
                         minute=int(minute_of_day) % 60)

    def minute_of_day(self, ts: int) -> int:
        dt = self.local(ts)
        return dt.hour * 60 + dt.minute

    def weekday(self, ts: int) -> int:
        return self.local(ts).weekday()  # Monday == 0


# ---------------------------------------------------------------------------
# resolver
# ---------------------------------------------------------------------------


class TemporalResolver:
    def __init__(self, clock: Clock, *, sleep_start: int = 23 * 60,
                 sleep_end: int = 7 * 60,
                 dinner_start: int = 18 * 60,
                 dinner_end: int = 19 * 60,
                 lunch_start: int = 12 * 60,
                 lunch_end: int = 13 * 60,
                 class_windows: list[tuple[int, int, str]] | None = None,
                 meeting_windows: list[tuple[int, int, str]] | None = None):
        self.clock = clock
        self.sleep_start = int(sleep_start)
        self.sleep_end = int(sleep_end)
        self.dinner_start = int(dinner_start)
        self.dinner_end = int(dinner_end)
        self.lunch_start = int(lunch_start)
        self.lunch_end = int(lunch_end)
        self.class_windows = list(class_windows or [])
        self.meeting_windows = list(meeting_windows or [])

    # ------------------------------------------------------------- helpers
    def _range(self, phrase: str, start: int, end: int,
               resolution: TemporalResolution, confidence: float,
               *, all_day: bool = False) -> TemporalRange:
        return TemporalRange(
            phrase=phrase, start=int(start), end=int(end),
            timezone=str(getattr(self.clock.tz, "key", "") or ""),
            resolution=resolution, confidence=confidence, all_day=all_day,
            source="temporal_resolver")

    def _unresolved(self, phrase: str, reason: str = "") -> TemporalRange:
        return TemporalRange(
            phrase=phrase, timezone=str(getattr(self.clock.tz, "key", "") or ""),
            resolution=TemporalResolution.UNRESOLVED, confidence=0.0,
            source="temporal_resolver")

    def _parse_clock(self, text: str) -> tuple[int, int] | None:
        m = _CLOCK_RE.search(text)
        if not m:
            return None
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = (m.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return hour, minute

    # -------------------------------------------------------------- public
    def resolve(self, phrase: str, *, now_ts: int | None = None) -> TemporalRange:
        phrase = (phrase or "").strip().lower()
        now = int(now_ts) if now_ts is not None else self.clock.now_ts()
        if not phrase:
            return self._unresolved("")
        day = self.clock.midnight(now)
        tomorrow = self.clock.add_days(day, 1)
        nom = self.clock.minute_of_day(now)

        # ---- in <n> <unit> ------------------------------------------------
        m = re.search(r"\bin\s+([a-z0-9\-]+)\s*(hour|hr|h|minute|min|m|day|d|week|w)s?\b",
                      phrase)
        if m:
            n = _number(m.group(1))
            if n is not None:
                unit = m.group(2)
                if unit in ("hour", "hr", "h"):
                    delta = int(n * 3600)
                elif unit in ("minute", "min", "m"):
                    delta = int(n * 60)
                elif unit in ("day", "d"):
                    delta = int(n * 86400)
                else:
                    delta = int(n * 7 * 86400)
                return self._range(phrase, now, now + delta,
                                   TemporalResolution.RESOLVED, 0.95)

        # ---- now ----------------------------------------------------------
        if phrase in ("now", "right now", "currently"):
            return self._range(phrase, now, now, TemporalResolution.EXPLICIT, 1.0)

        # ---- tonight ------------------------------------------------------
        if ("tonight" in phrase or "this evening" in phrase) \
                and "later tonight" not in phrase:
            start = max(now, self.clock.at_minute(day, 18 * 60))
            end = self.clock.at_minute(day, self.sleep_start)
            if start >= end:
                return self._unresolved(phrase, "evening already past")
            return self._range(phrase, start, end,
                               TemporalResolution.RESOLVED, 0.85)

        # ---- this morning / afternoon ------------------------------------
        if "this morning" in phrase:
            start = max(now, self.clock.at_minute(day, 8 * 60))
            end = self.clock.at_minute(day, 12 * 60)
            if start >= end:
                return self._unresolved(phrase, "morning already past")
            return self._range(phrase, start, end,
                               TemporalResolution.RESOLVED, 0.85)
        if "this afternoon" in phrase:
            start = max(now, self.clock.at_minute(day, 12 * 60))
            end = self.clock.at_minute(day, 17 * 60)
            if start >= end:
                return self._unresolved(phrase, "afternoon already past")
            return self._range(phrase, start, end,
                               TemporalResolution.RESOLVED, 0.85)

        # ---- tomorrow -----------------------------------------------------
        if "tomorrow" in phrase and not any(
                d in phrase for d in ("morning", "afternoon", "evening", "night")):
            return self._range(phrase, tomorrow,
                               self.clock.add_days(tomorrow, 1),
                               TemporalResolution.RESOLVED, 0.95, all_day=True)

        # ---- today --------------------------------------------------------
        if phrase in ("today", "rest of today", "rest of the day"):
            return self._range(phrase, now, self.clock.add_days(day, 1),
                               TemporalResolution.RESOLVED, 0.95)

        # ---- next week / this week ---------------------------------------
        if "next week" in phrase and "weekend" not in phrase:
            start = self.clock.add_days(day, 7 - self.clock.weekday(day))
            return self._range(phrase, start, self.clock.add_days(start, 7),
                               TemporalResolution.RESOLVED, 0.9, all_day=True)
        if "this week" in phrase or "rest of the week" in phrase:
            end = self.clock.add_days(day, 7 - self.clock.weekday(day))
            return self._range(phrase, now, end,
                               TemporalResolution.RESOLVED, 0.9)

        # ---- after dinner -------------------------------------------------
        if "after dinner" in phrase or "after supper" in phrase:
            start = max(now, self.clock.at_minute(day, self.dinner_end))
            end = self.clock.at_minute(day, self.sleep_start)
            if start >= end:
                return self._unresolved(phrase, "after bedtime")
            return self._range(phrase, start, end,
                               TemporalResolution.RESOLVED, 0.5)

        # ---- after / before lunch -----------------------------------------
        if "after lunch" in phrase or "after my lunch" in phrase:
            start = max(now, self.clock.at_minute(day, self.lunch_end))
            end = self.clock.at_minute(day, self.sleep_start)
            if start >= end:
                return self._unresolved(phrase, "after bedtime")
            return self._range(phrase, start, end,
                               TemporalResolution.RESOLVED, 0.6)
        if "before lunch" in phrase:
            end = self.clock.at_minute(day, self.lunch_start)
            if end <= now:
                return self._unresolved(phrase, "lunch already past")
            return self._range(phrase, now, end,
                               TemporalResolution.RESOLVED, 0.6)

        # ---- before dinner ------------------------------------------------
        if "before dinner" in phrase or "before supper" in phrase:
            end = self.clock.at_minute(day, self.dinner_start)
            if end <= now:
                end = self.clock.at_minute(tomorrow, self.dinner_start)
            return self._range(phrase, now, end,
                               TemporalResolution.RESOLVED, 0.6)

        # ---- later tonight ------------------------------------------------
        if "later tonight" in phrase:
            start = max(now, self.clock.at_minute(day, 20 * 60))
            end = self.clock.at_minute(day, self.sleep_start)
            if start >= end:
                return self._unresolved(phrase, "too late tonight")
            return self._range(phrase, start, end,
                               TemporalResolution.RESOLVED, 0.6)

        # ---- tomorrow dayparts --------------------------------------------
        if "tomorrow morning" in phrase:
            return self._range(phrase, self.clock.at_minute(tomorrow, 8 * 60),
                               self.clock.at_minute(tomorrow, 12 * 60),
                               TemporalResolution.RESOLVED, 0.9)
        if "tomorrow afternoon" in phrase:
            return self._range(phrase, self.clock.at_minute(tomorrow, 12 * 60),
                               self.clock.at_minute(tomorrow, 17 * 60),
                               TemporalResolution.RESOLVED, 0.9)
        if "tomorrow evening" in phrase or "tomorrow night" in phrase:
            return self._range(phrase, self.clock.at_minute(tomorrow, 18 * 60),
                               self.clock.at_minute(tomorrow, self.sleep_start),
                               TemporalResolution.RESOLVED, 0.9)

        # ---- next / this <weekday> ----------------------------------------
        wd = self._weekday_in(phrase)
        if wd is not None:
            offset = (wd - self.clock.weekday(day)) % 7
            if offset == 0 and "next" in phrase:
                offset = 7  # "next Tuesday" is strictly after today
            target = self.clock.add_days(day, offset)
            return self._range(phrase, target, self.clock.add_days(target, 1),
                               TemporalResolution.RESOLVED, 0.85, all_day=True)

        # ---- next / this weekend ------------------------------------------
        if "weekend" in phrase:
            days_to_sat = (5 - self.clock.weekday(day)) % 7
            if "next" in phrase and days_to_sat == 0:
                days_to_sat = 7
            sat = self.clock.add_days(day, days_to_sat)
            return self._range(phrase, sat, self.clock.add_days(sat, 2),
                               TemporalResolution.RESOLVED, 0.8, all_day=True)

        # ---- before / after my meeting ------------------------------------
        if "meeting" in phrase and ("before" in phrase or "after" in phrase):
            nxt = self._next_window(self.meeting_windows, now)
            if nxt is None:
                return self._unresolved(phrase, "no known upcoming meeting")
            if "before" in phrase:
                return self._range(phrase, now, nxt[0],
                                   TemporalResolution.RESOLVED, 0.75)
            return self._range(
                phrase, nxt[1],
                self.clock.at_minute(day, self.sleep_start),
                TemporalResolution.RESOLVED, 0.7)

        # ---- after my lecture / class -------------------------------------
        if ("lecture" in phrase or "class" in phrase) and "after" in phrase:
            nxt = self._next_class(now)
            if nxt is None:
                return self._unresolved(phrase, "no known upcoming class")
            return self._range(
                phrase, nxt[1],
                self.clock.at_minute(day, self.sleep_start),
                TemporalResolution.RESOLVED, 0.75)

        # ---- before class -------------------------------------------------
        if "before class" in phrase or "before my class" in phrase:
            nxt = self._next_class(now)
            if nxt is None:
                return self._unresolved(phrase, "no known upcoming class")
            return self._range(phrase, now, nxt[0],
                               TemporalResolution.RESOLVED, 0.8)

        # ---- before/after <clock> ----------------------------------------
        if phrase.startswith("before") or "before " in phrase:
            hm = self._parse_clock(phrase)
            if hm is not None:
                end = self.clock.at_minute(day, hm[0] * 60 + hm[1])
                if end <= now:
                    end = self.clock.at_minute(tomorrow, hm[0] * 60 + hm[1])
                return self._range(phrase, now, end,
                                   TemporalResolution.RESOLVED, 0.8)
        if phrase.startswith("after") or "after " in phrase:
            hm = self._parse_clock(phrase)
            if hm is not None:
                start = self.clock.at_minute(day, hm[0] * 60 + hm[1])
                if start < now:
                    start = now
                return self._range(phrase, start,
                                   self.clock.at_minute(day, self.sleep_start),
                                   TemporalResolution.RESOLVED, 0.75)

        # ---- bare clock ("5pm", "17:30") ---------------------------------
        hm = self._parse_clock(phrase)
        if hm is not None:
            start = self.clock.at_minute(day, hm[0] * 60 + hm[1])
            if start < now:
                start = self.clock.at_minute(tomorrow, hm[0] * 60 + hm[1])
            return self._range(phrase, start, start,
                               TemporalResolution.EXPLICIT, 0.9)

        # ---- next hour ----------------------------------------------------
        if "next hour" in phrase or "in an hour" in phrase:
            return self._range(phrase, now, now + 3600,
                               TemporalResolution.RESOLVED, 0.9)

        return self._unresolved(phrase, "phrase not safely resolvable")

    _WEEKDAYS = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }

    def _weekday_in(self, phrase: str) -> int | None:
        for name, idx in self._WEEKDAYS.items():
            if name in phrase:
                return idx
        return None

    def _next_window(self, windows: list[tuple[int, int, str]],
                     now: int) -> tuple[int, int, str] | None:
        upcoming = [w for w in windows if w[1] > now]
        if not upcoming:
            return None
        return min(upcoming, key=lambda w: w[0])

    def _next_class(self, now: int) -> tuple[int, int, str] | None:
        upcoming = [w for w in self.class_windows if w[1] > now]
        if not upcoming:
            return None
        return min(upcoming, key=lambda w: w[0])

    # -------------------------------------------------------------- scope
    def scope(self, phrase: str, temporal: TemporalRange) -> Scope:
        phrase = (phrase or "").strip().lower()
        kind = ScopeKind.UNKNOWN
        if ("tonight" in phrase or "this evening" in phrase) \
                and "later tonight" not in phrase:
            kind = ScopeKind.TONIGHT
        elif "tomorrow" in phrase:
            kind = ScopeKind.TOMORROW
        elif "next week" in phrase:
            kind = ScopeKind.NEXT_WEEK
        elif "this week" in phrase or "rest of the week" in phrase:
            kind = ScopeKind.THIS_WEEK
        elif phrase in ("now", "right now") or "right now" in phrase:
            kind = ScopeKind.NOW
        elif "today" in phrase:
            kind = ScopeKind.TODAY
        elif temporal.is_resolved():
            kind = ScopeKind.RANGE
        return Scope(kind=kind, start=temporal.start, end=temporal.end,
                     timezone=temporal.timezone)
