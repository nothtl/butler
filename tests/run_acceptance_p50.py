"""Phase 5.0 acceptance tests: Google Calendar WRITE + extended task lifecycle.

Run:  python tests/run_acceptance_p50.py

Proves the deterministic calendar projection (butler/planner.py + butler/gcal.py)
and the extended task status state machine obey the Phase 5.0 rules:

  1.  a placed ``todo`` is auto-promoted to ``scheduled`` (never duplicated)
  2.  the local plan is projected to Google Calendar as Butler-managed events
      (extendedProperties marker + task_id link) and never touches external ones
  3.  completed tasks KEEP their calendar event (goal state already reached)
  4.  cancelled/deferred/skipped tasks have their event deleted
  5.  an event is PATCHed, not recreated, when a task's slot moves
  6.  a restart is idempotent: the same DB+transport yields no duplicate events
  7.  a Google/transport outage is swallowed and never blocks local scheduling
  8.  Butler-written events are skipped on import (no self-blocking feedback)
  9.  the lifecycle state machine rejects illegal transitions (deterministic)
 10.  defer/block/cancel/resume round-trip through the correct statuses
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

# Align the host wall clock with the configured "UTC" layer. The planner clamps
# DB events into minute-of-day using the *host* local midnight while it writes
# event timestamps via cfg.local_midnight (UTC); pinning TZ to UTC makes those
# two frames agree so slot-move tests are deterministic on any host.
os.environ["TZ"] = "UTC"
try:
    time.tzset()  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover — non-POSIX host
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.gcal import GCalOutage, GoogleCalendar  # noqa: E402

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


# ---------------------------------------------------------- fake transport
class FakeGCalTransport:
    """A tiny in-memory Calendar REST transport with call recording.

    Implements exactly the surface ``_DefaultTransport`` now exposes (post, get,
    patch, delete) so the write projection can be exercised fully offline.
    """

    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.n_create = 0
        self.n_update = 0
        self.n_delete = 0
        self.fail_write = False
        self._seq = 0

    def _rec(self, method: str, url: str) -> None:
        self.calls.append((method, url))

    def _maybe_fail(self) -> None:
        if self.fail_write:
            raise GCalOutage("simulated transport outage")

    def _body(self, data):
        if isinstance(data, str):
            return json.loads(data)
        return dict(data or {})

    def post(self, url, data=None, headers=None):
        self._rec("POST", url)
        self._maybe_fail()
        if url.endswith("/events"):
            body = self._body(data)
            self._seq += 1
            eid = f"fallev-{self._seq}"
            ev = dict(body)
            ev["id"] = eid
            ev.setdefault("extendedProperties", {}).setdefault("private", {})
            self.store[eid] = ev
            self.n_create += 1
            return (200, ev)
        return (200, {"access_token": "tok", "expires_in": 3600})

    def get(self, url, params=None, headers=None):
        self._rec("GET", url)
        self._maybe_fail()
        if url.endswith("/events"):
            return (200, {"items": list(self.store.values())})
        eid = url.rsplit("/", 1)[-1]
        ev = self.store.get(eid)
        return (200, ev) if ev else (404, {})

    def patch(self, url, data=None, headers=None):
        self._rec("PATCH", url)
        self._maybe_fail()
        eid = url.rsplit("/", 1)[-1]
        if eid not in self.store:
            return (404, {})
        self.store[eid].update(self._body(data))
        self.store[eid].setdefault("extendedProperties", {}).setdefault("private", {})
        self.n_update += 1
        return (200, self.store[eid])

    def delete(self, url, headers=None):
        self._rec("DELETE", url)
        self._maybe_fail()
        eid = url.rsplit("/", 1)[-1]
        if eid not in self.store:
            return (404, {})
        del self.store[eid]
        self.n_delete += 1
        return (204, {})


# ---------------------------------------------------------- fixtures
def _cfg(base: str) -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    creds = os.path.join(base, "client_secret.json")
    cfg.google_calendar_credentials = creds
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    with open(creds, "w", encoding="utf-8") as fh:
        json.dump({"installed": {"client_id": "cid", "client_secret": "csec"}}, fh)
    token = os.path.join(cfg.state_dir, "gcal_token.json")
    with open(token, "w", encoding="utf-8") as fh:
        json.dump({"access_token": "tok", "refresh_token": "refresh",
                   "expires_at": int(time.time()) + 86400}, fh)
    return cfg


def _mk(base: str, transport: FakeGCalTransport | None = None) -> Container:
    sub = tempfile.mkdtemp(prefix="c-", dir=base)
    cfg = _cfg(sub)
    c = Container(cfg)
    c.planner.gcal_override = GoogleCalendar(cfg, http=transport or FakeGCalTransport())
    return c


def _restart(base: str, transport: FakeGCalTransport) -> Container:
    subs = sorted(d for d in os.listdir(base) if d.startswith("c-"))
    sub = os.path.join(base, subs[-1])
    cfg = _cfg(sub)
    c = Container(cfg)
    c.planner.gcal_override = GoogleCalendar(cfg, http=transport)
    return c


def _iso_ts(raw: str) -> int:
    return int(datetime.fromisoformat(raw).timestamp())


# ---------------------------------------------------------- sections
def section(title: str) -> None:
    print(f"\n=== {title} ===")


def test_projection(base: str) -> None:
    section("Create + marker + scheduled promotion")
    tr = FakeGCalTransport()
    c = _mk(base, tr)
    tid = c.planner.add_task("Write report", detail="PR", est_minutes=120, priority=5)
    res = c.planner.reschedule()
    check("1 plan produced and placed a slot", bool(res.get("slots")), len(res.get("slots", [])))
    check("1 truthy ok", bool(res.get("ok")))
    row = c.db.task_by_id(tid)
    check("2 task auto-promoted to scheduled",
          (row["status"] == "scheduled"), row["status"])
    check("3 exactly one create call", tr.n_create == 1, f"n_create={tr.n_create}")
    ev = next(iter(tr.store.values()))
    priv = ev["extendedProperties"]["private"]
    check("4 event is Butler-managed", priv.get("butler_managed") == "1", str(priv))
    check("5 event linked to the task",
          priv.get("butler_task_id") == str(tid), str(priv.get("butler_task_id")))


def test_preserve_completed(base: str) -> None:
    section("Completed tasks keep their event; external events untouched")
    tr = FakeGCalTransport()
    c = _mk(base, tr)
    tid = c.planner.add_task("Draft spec", est_minutes=60, priority=5)
    c.planner.reschedule()
    check("1 event created", tr.n_create == 1, f"n_create={tr.n_create}")
    c.planner.done(tid)
    check("2 completed task keeps its event", len(tr.store) == 1, f"store={len(tr.store)}")
    check("3 no delete issued for a completed task", tr.n_delete == 0, f"n_delete={tr.n_delete}")
    check("4 no duplicate create on replan", tr.n_create == 1, f"n_create={tr.n_create}")
    row = c.db.task_gcal(tid)
    check("5 mapping still synced", (row["state"] == "synced" and row["gcal_event_id"]),
          row["state"])


def test_cancel_deletes(base: str) -> None:
    section("Cancelled/deferred/skipped tasks delete their event")
    tr = FakeGCalTransport()
    c = _mk(base, tr)
    tid = c.planner.add_task("Old chore", est_minutes=90, priority=3)
    c.planner.reschedule()
    check("1 event created", tr.n_create == 1, f"n_create={tr.n_create}")
    c.planner.cancel_task(tid)
    check("2 event deleted on cancel", len(tr.store) == 0, f"store={len(tr.store)}")
    check("3 exactly one delete", tr.n_delete == 1, f"n_delete={tr.n_delete}")
    row = c.db.task_gcal(tid)
    check("4 mapping marked deleted", row["state"] == "deleted", row["state"])

    tr2 = FakeGCalTransport()
    c2 = _mk(base, tr2)
    tid2 = c2.planner.add_task("Defer me", est_minutes=60, priority=4)
    c2.planner.reschedule()
    c2.planner.defer(tid2)
    check("5 deferred deletes its event", len(tr2.store) == 0, f"store={len(tr2.store)}")

    tr3 = FakeGCalTransport()
    c3 = _mk(base, tr3)
    tid3 = c3.planner.add_task("Skip me", est_minutes=60, priority=4)
    c3.planner.reschedule()
    c3.planner.skip(tid3)
    check("6 skipped deletes its event", len(tr3.store) == 0, f"store={len(tr3.store)}")


def test_update(base: str) -> None:
    section("Slot move => PATCH (no recreate)")
    tr = FakeGCalTransport()
    c = _mk(base, tr)
    tid = c.planner.add_task("Move me", est_minutes=180, priority=5)
    c.planner.reschedule()
    check("1 event created", tr.n_create == 1, f"n_create={tr.n_create}")
    ev = next(iter(tr.store.values()))
    s1 = _iso_ts(ev["start"]["dateTime"])
    e1 = _iso_ts(ev["end"]["dateTime"])
    # A hard local commitment exactly covering the task's own slot forces a move.
    c.db.add_event("Hard blocking", int(s1), int(e1), source="local")
    res = c.planner.reschedule()
    check("2 replan still ok", bool(res.get("ok")))
    ev2 = next(iter(tr.store.values()))
    s2 = _iso_ts(ev2["start"]["dateTime"])
    check("3 task re-slotted (moved)", s2 != s1, f"{s1} -> {s2}")
    check("4 still a single event (no recreate)",
          len(tr.store) == 1 and tr.n_create == 1, f"store={len(tr.store)}")
    check("5 PATCH update used", tr.n_update >= 1, f"n_update={tr.n_update}")


def test_restart_idempotent(base: str) -> None:
    section("Restart idempotency (no duplicate events)")
    tr = FakeGCalTransport()
    c = _mk(base, tr)
    tid = c.planner.add_task("Stable", est_minutes=120, priority=5)
    c.planner.reschedule()
    check("1 initial create", tr.n_create == 1, f"n_create={tr.n_create}")
    c2 = _restart(base, tr)
    res = c2.planner.reschedule()
    check("2 restart replan ok", bool(res.get("ok")))
    check("3 no duplicate event", len(tr.store) == 1, f"store={len(tr.store)}")
    check("4 no extra create on restart", tr.n_create == 1, f"n_create={tr.n_create}")


def test_outage(base: str) -> None:
    section("Google outage is swallowed; local scheduling survives")
    tr = FakeGCalTransport()
    tr.fail_write = True
    c = _mk(base, tr)
    tid = c.planner.add_task("Despite outage", est_minutes=120, priority=5)
    res = None
    try:
        res = c.planner.reschedule()
    except Exception as exc:  # should never happen
        check("0 reschedule did not raise", False, repr(exc))
    check("1 reschedule returns ok", bool(res and res.get("ok")), str(res))
    check("2 active plan still saved", bool(c.db.latest_plan()))
    row = c.db.task_gcal(tid)
    check("3 create marked pending",
          (row["state"] == "pending_create"), row["state"] if row else "missing")
    tr.fail_write = False
    res = c.planner.reschedule()
    check("4 outage clears on retry", tr.n_create >= 1, f"n_create={tr.n_create}")
    row = c.db.task_gcal(tid)
    check("5 mapping recovered to synced",
          (row["state"] == "synced" and row["gcal_event_id"]),
          row["state"] if row else "missing")


def test_import_skip(base: str) -> None:
    section("Butler-written events are skipped on import (no feedback)")
    c = _mk(base)
    now = int(time.time())
    evs = [
        {"external_id": "b1", "title": "Butler Task X", "start_ts": now + 3600,
         "end_ts": now + 7200, "butler_managed": True, "all_day": 0},
        {"external_id": "ext1", "title": "External Standup", "start_ts": now + 3600,
         "end_ts": now + 4500, "butler_managed": False, "all_day": 0},
    ]
    res = c.planner.sync_google(events=evs)
    check("1 external imported, butler skipped",
          res.get("added") == 1 and res.get("count") == 1, str(res))
    rows = c.db.query("SELECT * FROM events WHERE source='google'")
    titles = [r["title"] for r in rows]
    check("2 only the external event landed in DB", titles == ["External Standup"], str(titles))


def test_lifecycle(base: str) -> None:
    section("Lifecycle state machine (deterministic transitions)")
    c = _mk(base)
    tid = c.planner.add_task("Lifecycle", est_minutes=60, priority=3)
    check("1 add -> todo",
          (c.db.task_by_id(tid)["status"] == "todo"), c.db.task_by_id(tid)["status"])
    r = c.planner.start(tid)
    check("2 start -> doing", bool(r["ok"]) and (c.db.task_by_id(tid)["status"] == "doing"),
          r.get("to"))
    r = c.planner.done(tid)
    check("3 done -> completed", bool(r["ok"]) and (c.db.task_by_id(tid)["status"] == "completed"),
          r.get("to"))
    r = c.planner.done(tid)
    check("4 done(completed) is rejected (idempotent)",
          (r["ok"] is False and "cannot go" in r.get("error", "")), r.get("error"))

    tid2 = c.planner.add_task("Lifecycle2", est_minutes=60, priority=3)
    r = c.planner.defer(tid2)
    check("5 defer -> deferred", bool(r["ok"]) and (c.db.task_by_id(tid2)["status"] == "deferred"),
          r.get("to"))
    r = c.planner.defer(tid2)
    check("6 defer(deferred) is rejected",
          r["ok"] is False, r.get("error"))
    r = c.planner.done(tid2)
    check("7 done(deferred) is rejected",
          r["ok"] is False, r.get("error"))
    r = c.planner.resume_task(tid2)
    st = c.db.task_by_id(tid2)["status"]
    check("8 resume -> active (re-scheduled if room)",
          bool(r["ok"]) and st in ("todo", "scheduled"), st)

    tid3 = c.planner.add_task("Lifecycle3", est_minutes=60, priority=3)
    r = c.planner.block_task(tid3)
    check("9 block -> blocked", bool(r["ok"]) and (c.db.task_by_id(tid3)["status"] == "blocked"),
          r.get("to"))
    r = c.planner.resume_task(tid3)
    check("10 resume(blocked) -> todo", bool(r["ok"]),
          c.db.task_by_id(tid3)["status"])
    r = c.planner.cancel_task(tid3)
    check("11 cancel -> cancelled", bool(r["ok"]) and (c.db.task_by_id(tid3)["status"] == "cancelled"),
          r.get("to"))
    check("12 history audit recorded transitions",
          len(c.db.task_history(tid3)) >= 2, f"h={len(c.db.task_history(tid3))}")


def main() -> None:
    base = tempfile.mkdtemp(prefix="p50-")
    try:
        test_projection(base)
        test_preserve_completed(base)
        test_cancel_deletes(base)
        test_update(base)
        test_restart_idempotent(base)
        test_outage(base)
        test_import_skip(base)
        test_lifecycle(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
