"""Phase 4.3 acceptance tests for Butler's context timeline & history.

Run:  .venv/bin/python tests/run_acceptance_p43.py

Proves the durable, SQLite-backed zone-level timeline:

   1. a zone transition is recorded on change
   2. an unchanged zone is not duplicated (idempotent)
   3. an unknown HA state is handled without an event
   4. an HA outage never corrupts the timeline (real transitions still recorded)
   5. a task completion is recorded
   6. calendar start/end events are recorded
   7. events are queryable by time range
   8. current vs historical context are distinct
   9. no raw GPS coordinate is persisted
  10. no HA token leaks into the DB
  11. Telegram/CLI timeline queries resolve to a readable reply
  12. retention prunes old events when configured
  13. repeated polling (presence / calendar sync) is idempotent

Run everything:
  .venv/bin/python tests/test_schedule.py
  .venv/bin/python tests/run_acceptance.py
  .venv/bin/python tests/run_acceptance_p3.py
  .venv/bin/python tests/run_acceptance_p35.py
  .venv/bin/python tests/run_acceptance_p36.py
  .venv/bin/python tests/run_acceptance_p37.py
  .venv/bin/python tests/run_acceptance_p41.py
  .venv/bin/python tests/run_acceptance_p42.py
  .venv/bin/python tests/run_acceptance_p43.py
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.house import HAOutage, HomeAssistant  # noqa: E402

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


def _tracker(eid: str, state: str, **attrs) -> dict:
    return {"entity_id": eid, "state": state, "attributes": attrs}


class FakeHA:
    def __init__(self, states=None, status: int = 200):
        self._states = states if states is not None else []
        self._status = status

    def get(self, url: str, headers=None) -> tuple[int, object]:
        return self._status, self._states


class RaisingHA(HomeAssistant):
    def states(self):
        raise HAOutage("backend down")


def _cfg(base: str, enabled: bool = True, token: str = "tl-secret-token") -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.home_assistant_enabled = enabled
    cfg.home_assistant_url = "http://ha.local:8123"
    cfg.home_assistant_token = token
    cfg.ensure_dirs()
    return cfg


def _ha_for(cfg: Config, zone: str) -> HomeAssistant:
    state = zone if zone else "unknown"
    return HomeAssistant(cfg, http=FakeHA(states=[
        _tracker("device_tracker.phone", state, location_name=state or "Unknown",
                 battery=72, source_type="gps")]))


def _container(base: str, zone: str = "") -> Container:
    sub = tempfile.mkdtemp(prefix="c-", dir=base)
    cfg = _cfg(sub)
    c = Container(cfg)
    c.ha = _ha_for(cfg, zone)
    return c


def _pres(zone: str, known: bool = True, status: str = "away", available: bool = True) -> dict:
    return {"known": known, "zone": zone, "status": status,
            "battery": 72, "available": available, "source": "home_assistant"}


def _ts(hour: int, minute: int = 0) -> int:
    d = datetime.date.today()
    return int(datetime.datetime(d.year, d.month, d.day, hour, minute).timestamp())


def _db_bytes(container: Container) -> bytes:
    with open(container.cfg.db_path(), "rb") as fh:
        return fh.read()


def _gps_in(text: str | bytes) -> bool:
    if isinstance(text, bytes):
        text = text.decode(errors="ignore")
    return bool(re.search(r"[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?", text))


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p43-")
    try:
        # ============ 4.3.1 zone transition recorded (integration) ==========
        print("\n=== 4.3.1 Zone transition is recorded ===")
        c = _container(base, zone="Library")
        c.context.snapshot()                       # records initial Library
        c.ha = _ha_for(c.cfg, "Gym")
        c.context.snapshot()                       # Library -> Gym
        zone_events = [e for e in c.timeline.get_recent()
                       if e["type"] == "zone_change"]
        check("a zone transition is recorded",
              len(zone_events) == 2
              and zone_events[0]["zone_to"] == "Gym"
              and zone_events[0]["zone_from"] == "Library",
              f"{[(e['zone_from'], e['zone_to']) for e in zone_events]}")

        # ============ 4.3.2 unchanged zone not duplicated ============
        print("\n=== 4.3.2 Unchanged zone is not duplicated ===")
        before = len(c.timeline.get_recent())
        again = c.timeline.observe_presence(_pres("Gym"))
        c.context.snapshot()
        check("an unchanged zone re-poll produces no new event",
              again is None and len(c.timeline.get_recent()) == before,
              f"added={again is not None}")

        # ============ 4.3.3 unknown HA state ============
        print("\n=== 4.3.3 Unknown HA state is handled ===")
        unk = c.timeline.observe_presence(_pres("", known=False, status="unknown",
                                               available=False))
        check("unknown presence produces no event",
              unk is None and c.timeline.current_context()["zone"] == "Gym",
              f"current={c.timeline.current_context()}")

        # ============ 4.3.4 HA outage does not corrupt ============
        print("\n=== 4.3.4 HA outage never corrupts the timeline ===")
        before = len(c.timeline.get_recent())
        c.ha = RaisingHA(c.cfg)
        c.context.snapshot()  # degrades to unknown, must not touch last zone
        check("outage leaves the last-known zone intact",
              len(c.timeline.get_recent()) == before
              and c.timeline.current_context()["zone"] == "Gym",
              f"current={c.timeline.current_context()}")
        c.ha = _ha_for(c.cfg, "Cafe")
        c.context.snapshot()
        check("a real transition is still recorded after an outage",
              c.timeline.current_context()["zone"] == "Cafe")

        # ============ 4.3.5 task completion recorded ============
        print("\n=== 4.3.5 Task completion is recorded ===")
        tid = c.planner.add_task("Finish essay", est_minutes=45)
        c.planner.start(tid)
        c.planner.done(tid)
        types = [e["type"] for e in c.timeline.get_recent()]
        check("task start + completion are recorded",
              "task_started" in types and "task_completed" in types, str(types))

        # ============ 4.3.6 calendar events recorded ============
        print("\n=== 4.3.6 Calendar start/end events are recorded ===")
        cal = [{"external_id": "ev-42", "title": "CS168 Lecture",
                "start_ts": _ts(9, 0), "end_ts": _ts(10, 0), "source": "google"}]
        added = c.timeline.record_calendar_events(cal)
        cal_types = {e["type"] for e in c.timeline.get_recent()}
        check("calendar start and end are recorded",
              added == 2 and "calendar_start" in cal_types
              and "calendar_end" in cal_types, f"added={added}")

        # ============ 4.3.7 query by time range ============
        print("\n=== 4.3.7 Query by time range ===")
        c2 = _container(base, zone="Dorm")
        c2.timeline.establish_zone("Dorm", ts=_ts(8, 0))
        c2.timeline.establish_zone("Library", ts=_ts(15, 0))
        c2.timeline.establish_zone("Gym", ts=_ts(17, 0))
        window = c2.timeline.get_between(_ts(14, 30), _ts(15, 30))
        check("events are queryable by time range",
              len(window) == 1 and window[0]["type"] == "zone_change"
              and window[0]["zone_to"] == "Library",
              f"window={[e['zone_to'] for e in window]}")

        # ============ 4.3.8 current vs historical distinct ============
        print("\n=== 4.3.8 Current vs historical context are distinct ===")
        past = c2.timeline.get_between(_ts(8, 0), _ts(8, 1))
        cur = c2.timeline.current_context()
        check("historical window vs live zone are distinct",
              len(past) == 1 and past[0]["zone_to"] == "Dorm"
              and cur["zone"] == "Gym",
              f"past={[e['zone_to'] for e in past]} cur={cur['zone']}")

        # ============ 4.3.9 no raw GPS persisted ============
        print("\n=== 4.3.9 No raw GPS coordinate is persisted ===")
        c.timeline.observe_presence(_pres("Library"))
        check("no raw GPS coordinate appears in the timeline store",
              not any(_gps_in(str(e["zone_to"])) or _gps_in(str(e["zone_from"]))
                      for e in c.timeline.get_recent()))
        check("no raw GPS coordinate appears anywhere in the DB",
              not _gps_in(_db_bytes(c)),
              f"db_size={len(_db_bytes(c))}")

        # ============ 4.3.10 no token leakage ============
        print("\n=== 4.3.10 No HA token leaks into the DB ===")
        token = c.cfg.home_assistant_token
        check("the HA access token is never persisted",
              token and token.encode() not in _db_bytes(c),
              f"token={token}")

        # ============ 4.3.11 timeline queries work (Telegram/CLI) ============
        print("\n=== 4.3.11 Timeline queries resolve to a readable reply ===")
        intents = ("where have i been", "show my context today",
                   "what have i been doing today", "/timeline")
        all_ok = True
        replies = []
        for q in intents:
            intent = c.decider.parse(q)
            result = c.decider.resolve(intent, "user")
            tok = result.get("kind") if isinstance(result, dict) else ""
            text = result.get("text", "") if isinstance(result, dict) else ""
            if tok != "timeline" or not text:
                all_ok = False
            replies.append((q, tok, text.split("\n")[0]))
        check("timeline intents resolve to kind 'timeline' with readable text",
              all_ok, str(replies))
        check("current-location line is present in the reply",
              any("currently at" in r[2] for r in replies))

        # ============ 4.3.12 retention ============
        print("\n=== 4.3.12 Retention prunes old events ===")
        c3 = _container(base, zone="Office")
        c3.timeline.establish_zone("Office", ts=_ts(7, 0))
        c3.timeline.establish_zone("Lab", ts=_ts(7, 5))
        n = c3.timeline.prune(retention_days=0)
        check("retention_days=0 keeps everything", n == 0, f"removed={n}")
        removed = c3.timeline.prune(retention_days=1)  # today's events kept
        check("recent events survive a 1-day retention", removed == 0, f"removed={removed}")
        old_ts = _ts(7, 0) - 10 * 86400
        c3.timeline.establish_zone("Old", ts=old_ts)
        c3.timeline.record_user_declaration("went on a walk", ts=old_ts)
        before = len(c3.timeline.get_recent())
        removed = c3.timeline.prune(retention_days=1)
        check("events older than the retention window are pruned",
              removed >= 1 and len(c3.timeline.get_recent()) < before,
              f"removed={removed}")

        # ============ 4.3.13 duplicate polling idempotent ============
        print("\n=== 4.3.13 Repeated polling is idempotent ===")
        c4 = _container(base, zone="Library")
        p = _pres("Library")
        c4.timeline.observe_presence(p)
        count1 = len(c4.timeline.get_recent())
        for _ in range(5):
            c4.timeline.observe_presence(p)
        count2 = len(c4.timeline.get_recent())
        cal2 = [{"external_id": "ev-99", "title": "Repeat", "start_ts": _ts(12, 0),
                 "end_ts": _ts(12, 30)}]
        c4.timeline.record_calendar_events(cal2)
        n1 = len(c4.timeline.get_recent())
        c4.timeline.record_calendar_events(cal2)
        n2 = len(c4.timeline.get_recent())
        tid2 = c4.planner.add_task("idempotent", est_minutes=10)
        c4.planner.done(tid2)
        m1 = sum(1 for e in c4.timeline.get_recent()
                 if e["type"] == "task_completed" and e["external_id"] == f"task:{tid2}:task_completed")
        c4.planner.done(tid2)
        m2 = sum(1 for e in c4.timeline.get_recent()
                 if e["type"] == "task_completed" and e["external_id"] == f"task:{tid2}:task_completed")
        check("repeated presence polling is idempotent",
              count1 == count2, f"{count1} vs {count2}")
        check("repeated calendar sync is idempotent", n1 == n2, f"{n1} vs {n2}")
        check("completing an already-done task is idempotent",
              m1 == 1 and m2 == 1, f"task_completed={m1} vs {m2}")

        c.db.close()
        c2.db.close()
        c3.db.close()
        c4.db.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\nP43: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
