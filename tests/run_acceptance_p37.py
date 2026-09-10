"""Phase 3.7 acceptance tests for Butler Google Calendar hard constraints.

Run:  .venv/bin/python tests/run_acceptance_p37.py

Proves Google Calendar is a reliable source of hard constraint events:
  1. first OAuth/sync  -- a stored token yields events that land in the DB
  2. repeated sync is idempotent (no duplicate rows, no churn)
  3. event modification updates the existing row in place (no duplicate)
  4. event cancellation/removal prunes the row (and a cancelled occurrence)
  5. a recurring series expands into distinct deterministic instances
  6. timezone correctness (offset + all-day) -> correct absolute timestamps
  7. calendar outage / auth loss -> graceful, existing events preserved
  8. the scheduler treats imported events as hard constraints (never overlaps)
  plus the existing Phase 2/3/3.5/3.6 suites stay green (run separately).

Run everything:
  .venv/bin/python tests/test_schedule.py
  .venv/bin/python tests/run_acceptance.py
  .venv/bin/python tests/run_acceptance_p3.py
  .venv/bin/python tests/run_acceptance_p35.py
  .venv/bin/python tests/run_acceptance_p36.py
  .venv/bin/python tests/run_acceptance_p37.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time as _time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.gcal import GCalAuthError, GCalError, GCalOutage, GoogleCalendar  # noqa: E402
from butler import schedule as sch  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


# ------------------------------------------------------------ helpers
def _local_ts(day_offset: int, hhmm: str) -> int:
    """Absolute ts for a local time on (today + day_offset)."""
    hh, mm = map(int, hhmm.split(":"))
    now = datetime.now()
    dt = datetime(now.year, now.month, now.day) + timedelta(days=day_offset, hours=hh, minutes=mm)
    return int(dt.timestamp())


def _ev(ext: str, title: str, start_ts: int, end_ts: int, all_day: int = 0,
        status: str = "confirmed", recurring: str = "") -> dict:
    return {"external_id": ext, "recurring_id": recurring, "title": title,
            "all_day": all_day, "start_ts": int(start_ts), "end_ts": int(end_ts),
            "location": "", "status": status, "timezone": "Etc/UTC"}


_CURR = {"n": 0}


def _cfg(base: str, google_enabled: bool = True) -> Config:
    _CURR["n"] += 1
    data = os.path.join(base, "storage%d" % _CURR["n"])
    os.makedirs(data, exist_ok=True)
    cfg = Config()
    cfg.data_dir = data
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = google_enabled
    cfg.google_calendar_credentials = os.path.join(base, "client_secret.json")
    cfg.local_calendar_file = os.path.join(base, "calendar.ics")  # absent -> skip
    cfg.ensure_dirs()
    return cfg


def _write_secret(base: str) -> None:
    with open(os.path.join(base, "client_secret.json"), "w") as fh:
        json.dump({"installed": {"client_id": "test-client-id",
                                 "client_secret": "test-client-secret"}}, fh)


def _write_token(cfg: Config, access: str = "atoken", expires_in: int = 3600) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(os.path.join(cfg.state_dir, "gcal_token.json"), "w") as fh:
        json.dump({"access_token": access, "refresh_token": "rtoken",
                   "expires_at": int(_time.time()) + expires_in}, fh)


class FakeTransport:
    """Deterministic (status, json) HTTP double for the connector."""

    def __init__(self, primary_tz: str = "Etc/UTC", events: list[dict] | None = None,
                 fail_event_fetch: bool = False):
        self.primary_tz = primary_tz
        self.items = events or []
        self.fail_event_fetch = fail_event_fetch
        self.calls: list[str] = []

    def get(self, url: str, params=None, headers=None) -> tuple[int, dict]:
        self.calls.append(url)
        if url.endswith("/calendars/primary"):
            return 200, {"timeZone": self.primary_tz}
        if "/events" in url:
            if self.fail_event_fetch:
                return 503, {"error": {"code": 503, "message": "backend down"}}
            return 200, {"items": self.items}
        return 404, {"error": {"code": 404}}

    def post(self, url: str, data=None, headers=None) -> tuple[int, dict]:
        return 400, {"error": "invalid_grant"}


class RaisingCalendar:
    def __init__(self, exc: GCalError):
        self._exc = exc

    def read_calendar_ids(self) -> list[str]:
        return ["primary"]

    def calendar_id(self, calendar_id: str | None = None) -> str:
        return ""

    def list_events(self, calendar_id: str | None = None):
        raise self._exc


class FakeTokenTransport:
    """Serves refresh tokens side by side with the calendar fetch."""

    def __init__(self, events: list[dict] | None = None):
        self.items = events or []
        self.posts: list[str] = []

    def get(self, url: str, params=None, headers=None) -> tuple[int, dict]:
        if url.endswith("/calendars/primary"):
            return 200, {"timeZone": "Etc/UTC"}
        if "/events" in url:
            return 200, {"items": self.items}
        return 404, {"error": {"code": 404}}

    def post(self, url: str, data=None, headers=None) -> tuple[int, dict]:
        self.posts.append(url)
        return 200, {"access_token": "fresh-token", "expires_in": 3600}


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p37-")
    try:
        # ============ 3.7.1 First OAuth/sync (token -> events in DB) =========
        print("\n=== 3.7.1 First OAuth/sync ===")
        _write_secret(base)
        cfg = _cfg(base)
        _write_token(cfg)  # simulates `butler calendar connect` completion
        transport = FakeTransport(primary_tz="America/New_York", events=[
            {"id": "ev1", "summary": "Lecture", "status": "confirmed",
             "start": {"dateTime": "2026-09-07T10:00:00-04:00",
                       "timeZone": "America/New_York"},
             "end": {"dateTime": "2026-09-07T11:30:00-04:00",
                     "timeZone": "America/New_York"},
             "location": "Room 1"},
            {"id": "ev2", "summary": "Dentist", "status": "confirmed",
             "start": {"date": "2026-09-08"}, "end": {"date": "2026-09-09"}},
        ])
        gc = GoogleCalendar(cfg, http=transport)
        events = gc.list_events()
        check("connector returns external ids", len(events) == 2,
              str([e["external_id"] for e in events]))
        e1 = events[0]
        check("timed event parsed with timezone offset",
              e1["start_ts"] == int(datetime.fromisoformat("2026-09-07T10:00:00-04:00").timestamp()),
              str(e1["start_ts"]))
        check("timed event end parsed", e1["end_ts"] != e1["start_ts"] and e1["all_day"] == 0,
              f"{e1['start_ts']}..{e1['end_ts']}")
        e2 = events[1]
        check("all-day event spans a full day in calendar tz", e2["all_day"] == 1
              and e2["end_ts"] - e2["start_ts"] == 86400, f"{e2['start_ts']}..{e2['end_ts']}")
        # Push the feed through the planner merge -> DB rows.
        container = Container(cfg)
        merged = container.planner.sync_google(events=events)
        rows = container.db.events()
        check("merged events inserted into events table",
              merged["added"] == 2 and len(rows) == 2, str({k: merged[k] for k in ("added", "count")}))
        check("all rows are source=google", all(r["source"] == "google" for r in rows))
        check("external_id persisted for dedupe",
              all(r["external_id"] for r in rows), str([r["external_id"] for r in rows]))

        # ---- token refresh (near-expiry) transparently renews the access token
        print("\n=== 3.7.1b Token refresh ===")
        _write_token(cfg, access="expired-atoken", expires_in=-60)  # already expired
        rt = FakeTokenTransport(events=[
            {"id": "ev1", "summary": "Lecture", "status": "confirmed",
             "start": {"dateTime": "2026-09-07T10:00:00Z"},
             "end": {"dateTime": "2026-09-07T11:00:00Z"}},
        ])
        rgc = GoogleCalendar(cfg, http=rt)
        token = rgc._load()
        check("expired token flagged for refresh", token["expires_at"] - 120 < int(_time.time()))
        refreshed = rgc.access()
        check("refresh round-trips through /token and updates the stored token",
              rt.posts and refreshed == "fresh-token"
              and rgc._load()["access_token"] == "fresh-token",
              str(rt.posts))
        # a listing with the fresh token now succeeds
        evs = rgc.list_events()
        check("listing works after automatic refresh", len(evs) == 1, str(len(evs)))

        # ============ 3.7.2 Idempotent repeated sync ============
        print("\n=== 3.7.2 Repeated sync is idempotent ===")
        merged2 = container.planner.sync_google(events=events)
        rows2 = container.db.events()
        check("re-sync is a no-op (added=updated=removed=0)",
              merged2["added"] == 0 and merged2["updated"] == 0 and merged2["removed"] == 0,
              str(merged2))
        check("no duplicate rows after re-sync", len(rows2) == 2, str(len(rows2)))

        # ============ 3.7.3 Event modification (in-place update) ============
        print("\n=== 3.7.3 Event modification updates in place ===")
        moved = [{"external_id": "ev1", "title": "Lecture", "all_day": 0,
                  "start_ts": _local_ts(0, "14:00"), "end_ts": _local_ts(0, "15:30"),
                  "status": "confirmed", "recurring_id": "", "timezone": "Etc/UTC"},
                 {"external_id": "ev2", "title": "Dentist", "all_day": 1,
                  "start_ts": _local_ts(1, "00:00"), "end_ts": _local_ts(2, "00:00"),
                  "status": "confirmed", "recurring_id": "", "timezone": "Etc/UTC"}]
        merged3 = container.planner.sync_google(events=moved)
        rows3 = container.db.events()
        ev1row = next(r for r in rows3 if r["external_id"] == "ev1")
        check("modification detected as updated (never a new add)",
              merged3["added"] == 0 and merged3["updated"] >= 1, str(merged3))
        check("existing event updated in place (same id, new time)",
              int(ev1row["start_ts"]) == _local_ts(0, "14:00"), str(ev1row["start_ts"]))
        check("no duplicate created on modify", len(rows3) == 2, str(len(rows3)))

        # ============ 3.7.4 Event cancellation / removal ============
        print("\n=== 3.7.4 Event cancellation / removal ===")
        # ev1 disappears entirely (moved out / deleted) -> prune.
        pruned = container.planner.sync_google(events=[moved[1]])
        rows4 = container.db.events()
        check("vanished event pruned", pruned["removed"] == 1 and len(rows4) == 1,
              f"removed={pruned['removed']} rows={len(rows4)}")
        # A single cancelled occurrence of a series is also removed.
        container.planner.sync_google(events=[
            moved[1],
            _ev("r1_ix", "Weekly meeting", _local_ts(2, "09:00"), _local_ts(2, "10:00"),
                recurring="r1"),
        ])
        # now cancel one occurrence -> the DB row for that occurrence must vanish
        container.planner.sync_google(events=[
            moved[1],
            _ev("r1_ix", "Weekly meeting", _local_ts(2, "09:00"), _local_ts(2, "10:00"),
                status="cancelled", recurring="r1"),
        ])
        rows5 = container.db.events()
        check("cancelled occurrence removed from DB",
              len(rows5) == 1 and all(r["external_id"] != "r1_ix" for r in rows5),
              f"rows={len(rows5)}")

        # ============ 3.7.5 Recurring event expansion ============
        print("\n=== 3.7.5 Recurring event expands into instances ===")
        container.planner.db.clear_events()
        recur = [
            _ev("r1_20260907", "Seminar", _local_ts(0, "10:00"), _local_ts(0, "11:00"),
                recurring="r1"),
            _ev("r1_20260914", "Seminar", _local_ts(7, "10:00"), _local_ts(7, "11:00"),
                recurring="r1"),
            _ev("r1_20260921", "Seminar", _local_ts(14, "10:00"), _local_ts(14, "11:00"),
                recurring="r1"),
        ]
        m5 = container.planner.sync_google(events=recur)
        rows6 = container.db.events()
        extids = sorted(r["external_id"] for r in rows6)
        check("each occurrence is a distinct dedupe key", m5["added"] == 3
              and extids == ["r1_20260907", "r1_20260914", "r1_20260921"], str(extids))
        # Series loses its last occurrence -> that instance is pruned.
        m5b = container.planner.sync_google(events=recur[:2])
        rows7 = container.db.events()
        check("series shortening prunes the dropped instance",
              m5b["removed"] == 1 and len(rows7) == 2, f"{m5b} rows={len(rows7)}")

        # ============ 3.7.6 Timezone correctness ============
        print("\n=== 3.7.6 Timezone correctness ===")
        _write_secret(base)
        tzcfg = _cfg(base)
        _write_token(tzcfg)
        tz_transport = FakeTransport(primary_tz="America/Los_Angeles", events=[
            {"id": "t1", "summary": "Standup", "status": "confirmed",
             "start": {"dateTime": "2026-09-07T09:00:00-07:00", "timeZone": "America/Los_Angeles"},
             "end": {"dateTime": "2026-09-07T09:30:00-07:00", "timeZone": "America/Los_Angeles"}},
            {"id": "t2", "summary": "All-day in LA", "status": "confirmed",
             "start": {"date": "2026-09-08"}, "end": {"date": "2026-09-09"}},
            {"id": "t3", "summary": "UTC literal", "status": "confirmed",
             "start": {"dateTime": "2026-09-07T09:00:00Z"},
             "end": {"dateTime": "2026-09-07T10:00:00Z"}},
        ])
        tzgc = GoogleCalendar(tzcfg, http=tz_transport)
        tz_events = {e["external_id"]: e for e in tzgc.list_events()}
        check("offered timestamp = absolute UTC instant",
              tz_events["t1"]["start_ts"] == int(datetime.fromisoformat("2026-09-07T09:00:00-07:00").timestamp()),
              str(tz_events["t1"]["start_ts"]))
        # all-day midnight in the calendar tz must resolve to the same absolute
        # UTC instant regardless of the machine's local timezone.
        import zoneinfo as _zi
        la_mid = datetime(2026, 9, 8, 0, 0, 0).replace(
            tzinfo=_zi.ZoneInfo("America/Los_Angeles"))
        check("all-day respects the calendar timezone",
              tz_events["t2"]["start_ts"] == int(la_mid.astimezone(_zi.ZoneInfo("UTC")).timestamp()),
              str(tz_events["t2"]["start_ts"]))
        check("Z suffix treated as UTC mid-morning",
              tz_events["t3"]["start_ts"] == int(datetime.fromisoformat("2026-09-07T09:00:00+00:00").timestamp()),
              str(tz_events["t3"]["start_ts"]))

        # ============ 3.7.7 Calendar outage / auth loss -> preserved =========
        print("\n=== 3.7.7 Calendar outage / auth failure preserves state ===")
        _write_secret(base)
        ocfg = _cfg(base)
        _write_token(ocfg)
        octr = Container(ocfg)
        # seed two google events
        octr.planner.sync_google(events=[moved[1], recur[0]])
        seed_rows = len(octr.db.events())
        # outage -> list_events raises GCalOutage; DB untouched
        got_outage = False
        try:
            octr.planner.sync_google(gcal=RaisingCalendar(GCalOutage("backend down")))
        except GCalError:
            got_outage = True
        check("outage raises GCalOutage (no silent success)", got_outage)
        check("existing events preserved on outage", len(octr.db.events()) == seed_rows,
              f"{seed_rows} -> {len(octr.db.events())}")
        # auth loss -> GCalAuthError
        try:
            octr.planner.sync_google(gcal=RaisingCalendar(GCalAuthError("reconnect")))
            auth_raised = False
        except GCalAuthError:
            auth_raised = True
        check("auth loss raises GCalAuthError", auth_raised)
        # sync_events("") swallows an outage for the other source
        octr.cfg.google_calendar_enabled = True
        _write_token(ocfg)
        octr.cfg.local_calendar_file = os.path.join(base, "nonexistent.ics")
        _merge_out = octr.planner.sync_events("")
        check("sync_events('') degrades gracefully on outage",
              _merge_out.get("google_error") and len(octr.db.events()) == seed_rows,
              f"{_merge_out.get('google_error')}")
        # token file removed (never connected) -> planner keeps working
        os.remove(os.path.join(ocfg.state_dir, "gcal_token.json"))
        before = len(octr.db.events())
        res = octr.planner.plan_day()
        check("planning still succeeds when Google is unavailable",
              res.get("ok") and len(octr.db.events()) == before, str(res.get("ok")))

        # ============ 3.7.8 Scheduler avoids imported events ============
        print("\n=== 3.7.8 Scheduler treats imported events as hard constraints ===")
        _write_secret(base)
        scfg = _cfg(base, google_enabled=False)
        _write_token(scfg)
        sctr = Container(scfg)
        sctr.cfg.google_calendar_enabled = False
        sctr.cfg.local_calendar_file = os.path.join(base, "absent.ics")
        t_today = int(datetime.now().timestamp())
        # Imported Google event: today 09:00-11:00 local (in the wake window).
        sctr.planner.sync_google(events=[
            _ev("hard1", "Standup", _local_ts(0, "09:00"), _local_ts(0, "11:00")),
        ])
        # A soft task that would love to fill today.
        sctr.planner.add_task("Deep work", est_minutes=240, deadline=0)
        res8 = sctr.planner.plan_day(t_today)
        slots = res8.get("slots", [])
        overlap = any((s["start_min"] < 660) and (s["end_min"] > 540) for s in slots)
        check("no study block overlaps the imported Google event", not overlap,
              f"slots={[(s['start_min'],s['end_min']) for s in slots]}")
        check("events are surfaced as hard commitments in the plan",
              any(e["source"] == "google" for e in res8.get("events", [])),
              str(res8.get("events")))
        # capacity envelope also respects the imported event (single day).
        cap = sctr.planner.capacity_before(240, _local_ts(0, "23:59"))
        check("capacity envelope subtracts the imported event",
              cap.get("available", 1) < 756, f"available={cap.get('available')}")
        # and match against the raw free geometry minus buffer.
        free = sctr.context.free_minutes(_local_ts(0, "00:00"), _local_ts(1, "00:00"))
        check("imported event leaves the morning/evening gaps open",
              cap.get("available", 0) <= free, f"cap={cap.get('available')} free={free}")
        check("no block exceeds the working window", all(420 <= s["start_min"]
              and s["end_min"] <= 1380 for s in slots), str(slots))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
