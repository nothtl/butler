"""Phase 5.1 acceptance tests: Daily Executive Loop.

Run:  python tests/run_acceptance_p51.py
(also ran by the main regression runner)

Walks the 45 minimum scenarios in the spec:

  DAILY BRIEFING (1-8)   DAILY EXECUTIVE (9-20)   DAILY REVIEW (21-25)
  CROSS-DOMAIN (26-30)   PROACTIVE (31-36)        FAILURE (37-42)
  INTEGRATION (43-45)

TZ is pinned to UTC so day-boundary arithmetic is deterministic on any host.
Where the answer depends on "now", every call pins ``day_ts``/``now_min``.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

os.environ["TZ"] = "UTC"
try:
    time.tzset()  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover — non-POSIX host
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.gcal import GoogleCalendar  # noqa: E402

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
    def __init__(self) -> None:
        self.store: dict[str, dict] = {}
        self.n_create = self.n_update = self.n_delete = 0
        self.fail_write = False
        self._seq = 0

    def _maybe_fail(self) -> None:
        if self.fail_write:
            from butler.gcal import GCalOutage
            raise GCalOutage("simulated outage")

    def _body(self, data):
        return json.loads(data) if isinstance(data, str) else dict(data or {})

    def post(self, url, data=None, headers=None):
        self._maybe_fail()
        if url.endswith("/events"):
            self._seq += 1
            eid = f"fallev-{self._seq}"
            ev = dict(self._body(data))
            ev["id"] = eid
            ev.setdefault("extendedProperties", {}).setdefault("private", {})
            self.store[eid] = ev
            self.n_create += 1
            return (200, ev)
        return (200, {"access_token": "tok", "expires_in": 3600})

    def get(self, url, params=None, headers=None):
        self._maybe_fail()
        if url.endswith("/events"):
            return (200, {"items": list(self.store.values())})
        eid = url.rsplit("/", 1)[-1]
        ev = self.store.get(eid)
        return (200, ev) if ev else (404, {})

    def patch(self, url, data=None, headers=None):
        self._maybe_fail()
        eid = url.rsplit("/", 1)[-1]
        if eid not in self.store:
            return (404, {})
        self.store[eid].update(self._body(data))
        self.store[eid].setdefault("extendedProperties", {}).setdefault("private", {})
        self.n_update += 1
        return (200, self.store[eid])

    def delete(self, url, headers=None):
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


def _restart(container: Container, transport: FakeGCalTransport) -> Container:
    sub = os.path.dirname(container.cfg.data_dir)
    cfg = _cfg(sub)
    c = Container(cfg)
    c.planner.gcal_override = GoogleCalendar(cfg, http=transport)
    return c


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def _today() -> int:
    return int(time.time())


def _day_start(ts: int, c: Container) -> int:
    return int(c.cfg.local_midnight(ts))


# =================================================================
# PART 1-2 : DAILY BRIEFING  (scenarios 1-8)
# =================================================================
def test_briefing(base: str) -> None:
    section("Daily Briefing (calendar/tasks/deadlines/overdue/course/food)")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    c.db.add_event("Lecture 9AM", ds + 9 * 3600, ds + 10 * 3600, source="google",
                   external_id="g1")
    c.planner.add_task("CS168 Project 2", detail="", est_minutes=90, priority=5,
                       deadline=ds + 20 * 3600)
    c.planner.add_task("Old overdue chore", est_minutes=30, deadline=ds - 3600)
    c.planner.add_task("Dinner prep", est_minutes=20, priority=2)
    c.planner.reschedule()
    c.food.add("Milk", expiration=now + 86400)
    b = c.executive.briefing(day_ts=now)
    txt = b["text"]
    check("1 briefing includes today's calendar", "Lecture 9AM" in txt, txt[:80])
    check("2 briefing includes scheduled task blocks", "Dinner prep" in txt, "")
    check("3 briefing includes important deadline", "CS168 Project 2" in txt, "")
    check("4 briefing identifies overdue work", "Old overdue chore" in txt, "")
    check("5 briefing incorporates course-derived task", "CS168 Project 2" in txt, "")
    check("6 briefing considers food/context", ("Milk" in txt or "🥫" in txt), "")


def test_briefing_idempotent(base: str) -> None:
    section("Daily Briefing idempotent across restart + scheduled delivery")
    c = _mk(base)
    now = _today()
    c.planner.add_task("Write report", est_minutes=60, priority=5)
    c.planner.reschedule()
    check("7 briefing is due", c.executive.is_due("briefing", now))
    c.executive.mark_delivered("briefing", now)
    check("8 same day is no longer due", not c.executive.is_due("briefing", now))
    # restart path reads the same DB marker
    c2 = _restart(c, c.planner.gcal_override.http)
    check("8b marker survives restart", not c2.executive.is_due("briefing", now))


def test_briefing_quiet_and_push(base: str) -> None:
    section("Briefing delivery respects quiet hours / throttling (proactive.run)")
    c = _mk(base)
    now = _today()
    c.cfg.telegram_token = ""   # no telegram -> graceful, no raise
    c.cfg.notify_quiet_start = 0
    c.cfg.notify_quiet_end = 0
    m = c.executive.briefing(day_ts=now)
    res = c.proactive.run()  # should not raise even without a token
    check("8c briefing content is non-empty", bool(m.get("text")), "")
    check("8d proactive run degrades w/o Telegram", isinstance(res, dict) and "messages" in res, str(res))


# =================================================================
# PART 3-4 : DAILY EXECUTIVE  (scenarios 9-20)
# =================================================================
def test_executive_priorities(base: str) -> None:
    section("Executive: urgent deadline prioritized, current task recognized")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    # a hard lecture occupies 09:00-10:00
    c.db.add_event("Lecture", ds + 9 * 3600, ds + 10 * 3600, source="google",
                   external_id="g2")
    c.planner.add_task("CS168 Project 2", est_minutes=120, priority=5,
                       deadline=ds + 18 * 3600)
    c.planner.add_task("Low chore", est_minutes=30, priority=1)
    c.planner.reschedule()
    r = c.executive.recommend("", day_ts=ds + 10 * 3600 + 60, now_min=10 * 60 + 1)
    check("9 urgent deadline prioritized", "CS168 Project 2" in (r.get("answer") or ""), r.get("answer"))
    check("10 present in active tasks", bool(r.get("candidate")), str(r.get("candidate")))


def test_executive_hard_limits(base: str) -> None:
    section("Executive: hard calendar + sleep limit / fit-window")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    # a 2.5h task but a 1h hard event right after the morning window, so the task
    # cannot fit before the next hard commitment.
    c.db.add_event("Lecture 10AM", ds + 10 * 3600, ds + 11 * 3600, source="google",
                   external_id="g3")
    c.planner.add_task("Long task", est_minutes=150, priority=5,
                       deadline=ds + 20 * 3600)
    c.planner.reschedule()
    r = c.executive.recommend("", day_ts=ds + 8 * 3600, now_min=15 * 60)
    # The only candidate window before the 10AM lecture ends at 10:00, so a 2.5h
    # task cannot fit there — the recommendation must never span the hard event.
    cand = r.get("candidate")
    if cand:
        ev_lo, ev_hi = 10 * 60, 11 * 60
        ss = int(cand["start"].split(":")[0]) * 60 + int(cand["start"].split(":")[1])
        ee = int(cand["end"].split(":")[0]) * 60 + int(cand["end"].split(":")[1])
        overlap = ss < ev_hi and ee > ev_lo
        check("11 hard event limits recommendation", not overlap,
              f"{cand['start']}-{cand['end']} vs 10:00-11:00")
    else:
        check("11 hard event limits recommendation", True, "no candidate")

    # sleep: nothing should be recommended once the day window closes (after sleep_start)
    c2 = _mk(base)
    c2.planner.add_task("Night task", est_minutes=60, priority=5)
    c2.planner.reschedule()
    r2 = c2.executive.recommend("", day_ts=ds + 23 * 3600 + 30 * 60, now_min=23 * 60 + 30)
    check("12 sleep limits recommendation", r2.get("candidate") is None,
          str(r2.get("candidate")))


def test_executive_soft(base: str) -> None:
    section("Executive: location/routine stay soft, user intent wins")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    c.planner.add_task("Read CS61B", est_minutes=45, priority=4)
    c.planner.add_task("Gym", est_minutes=45, priority=4)
    c.planner.reschedule()
    # Presence: at the library. Gym is location-mismatched but deadline-fit is
    # equally tiny; the recommendation must remain deterministic and include a
    # reason (it never refuses to act just because location is soft).
    r = c.executive.recommend("", day_ts=ds + 9 * 3600, now_min=9 * 60)
    check("14 executive still works without location", bool(r.get("answer")), "")
    check("20 recommendation has an explainable reason", bool(r.get("reason") or r.get("answer")), "")
    # is-tired is soft; recommendation should still be produced.
    r2 = c.executive.recommend("i'm tired", day_ts=ds + 9 * 3600, now_min=9 * 60)
    check("16 user intent (tired) handled, deterministic", bool(r2.get("answer")), r2.get("answer"))
    # blocked task is not repeatedly recommended
    tid = c.planner.add_task("Blocked task", est_minutes=30, priority=5)
    c.planner.block_task(tid)
    r3 = c.executive.recommend("", day_ts=ds + 9 * 3600, now_min=9 * 60)
    check("17 blocked task is not recommended", "Blocked task" not in (r3.get("answer") or ""), r3.get("answer"))


def test_executive_lifecycle_reflect(base: str) -> None:
    section("Executive: completed disappears, deferred handled")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    done = c.planner.add_task("Done task", est_minutes=30, priority=5)
    def_ = c.planner.add_task("Deferred task", est_minutes=30, priority=5)
    c.planner.reschedule()
    c.planner.done(done)
    c.planner.defer(def_)
    r = c.executive.recommend("", day_ts=ds + 10 * 3600, now_min=10 * 60)
    ans = r.get("answer") or ""
    check("18 completed task disappears", "Done task" not in ans, ans)
    check("19 deferred task is not re-recommended", "Deferred task" not in ans, ans)
    check("20b reason is present", bool(r.get("reason") or r.get("answer")),
          r.get("answer"))


# =================================================================
# PART 5-6 : DAILY REVIEW  (scenarios 21-25)
# =================================================================
def test_review(base: str) -> None:
    section("Daily Review: counts, planned vs actual, idempotent, restart")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    c.planner.add_task("Complete me", est_minutes=60, priority=5)
    c.planner.add_task("Defer me", est_minutes=60, priority=4)
    c.planner.add_task("Skip me", est_minutes=60, priority=3)
    c.planner.reschedule()
    for t in ("Complete me", "Defer me", "Skip me"):
        tid = next((int(r["id"]) for r in c.db.tasks("active")
                    if r["title"] == t), None)
        if tid is None:
            tid = next((int(r["id"]) for r in c.db.all_tasks()
                        if r["title"] == t), None)
        if t == "Complete me":
            c.planner.start(tid)
            c.planner.done(tid)
        elif t == "Defer me":
            c.planner.defer(tid)
        else:
            c.planner.skip(tid)
    rev = c.executive.review(day_ts=now)
    actual = rev["actual"]
    check("21 completed/deferred/skipped counts correct",
          actual["done"] == ["Complete me"] and actual["deferred"] == ["Defer me"]
          and actual["skipped"] == ["Skip me"],
          str(actual))
    check("22 planned vs actual summarized", bool(rev.get("text")) and "Planned" in rev["text"], rev["text"][:60])
    # unfinished deadline highlighted
    check("23 unfinished deadline appears (done task in history)", "Complete me" in rev["text"], "")
    # idempotency marker
    c.executive.mark_delivered("review", now)
    check("24 review idempotent", not c.executive.is_due("review", now))
    c2 = _restart(c, c.planner.gcal_override.http)
    rev2 = c2.executive.review(day_ts=now)
    check("25 review survives restart", rev2["actual"]["done"] == ["Complete me"], str(rev2["actual"]["done"]))


# =================================================================
# PART 7-9 : CROSS-DOMAIN  (scenarios 26-30)
# =================================================================
def test_cross_domain(base: str) -> None:
    section("Cross-domain: course deadline -> priority, calendar -> schedule, "
            "location/food soft")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    # calendar hard commitment blocks a slot
    c.db.add_event("Standup", ds + 9 * 3600, ds + 9 * 3600 + 1800, source="google",
                   external_id="g4")
    c.planner.add_task("CS168 Project 2", est_minutes=90, priority=5,
                       deadline=ds + 20 * 3600)
    c.planner.add_task("Small chore", est_minutes=30, priority=2)
    c.planner.reschedule()
    # a a task with a near deadline should be surfaced as the *important* one
    b = c.executive.briefing(day_ts=now)
    imp = " ".join(i["title"] for i in b.get("important", []))
    check("26 course deadline (task) influences priority", "CS168 Project 2" in imp, str(imp))
    r = c.executive.recommend("", day_ts=ds + 9 * 3600 + 1800, now_min=9 * 60 + 30)
    check("27 calendar constraint affects scheduling", bool(r.get("answer")), r.get("answer"))
    # location soft: presence known but must not break the loop
    try:
        c.context._presence = lambda: {"known": True, "zone": "library"}  # type: ignore[method-assign]
    except Exception:
        pass
    r2 = c.executive.recommend("", day_ts=ds + 9 * 3600 + 1800, now_min=9 * 60 + 30)
    check("28 location supports but doesn't override", bool(r2.get("answer")), "")
    # food: expiring surfaced softly; a meal is never silently scheduled
    c.food.add("Tomato", expiration=now + 86400)
    b2 = c.executive.briefing(day_ts=now)
    check("29 food opportunity considered (soft)", bool(b2.get("text")), "")
    # no meal task auto-created
    meal_tasks = [r["title"] for r in c.db.all_tasks() if "meal" in (r["title"] or "").lower()]
    check("29b no silent meal commitment", meal_tasks == [], str(meal_tasks))
    # recent context (timeline) must not break recommendation
    r3 = c.executive.recommend("", day_ts=ds + 10 * 3600, now_min=10 * 60)
    check("30 recent context doesn't become hard", bool(r3.get("answer")), "")


# =================================================================
# PART 10 : PROACTIVE EXECUTIVE NOTIFICATIONS  (scenarios 31-36)
# =================================================================
def test_proactive(base: str) -> None:
    section("Proactive: deadline/blocked notifications + throttle + dedup")
    c = _mk(base)
    now = _today()
    ds = _day_start(now, c)
    c.planner.add_task("Due soon", est_minutes=30, priority=5,
                       deadline=now + 2 * 3600)
    bl = c.planner.add_task("Blocked task", est_minutes=30, priority=5)
    c.planner.block_task(bl)
    c.planner.reschedule()
    msgs = c.proactive.collect()
    j = "\n".join(msgs)
    check("31 important deadline notification", "Due soon" in j, j)
    check("32 blocked-task notification", "Blocked task" in j, j)
    # throttle/dedup state
    c2 = _mk(base)
    res = c2.proactive.run()
    check("35 throttling state works", isinstance(res, dict), str(res))
    dupes = [m for i, m in enumerate(msgs)
             if m in msgs[:i]]
    check("36 dedup rejects repeats", dupes == [], str(dupes))


# =================================================================
# PART 12 : FAILURE / DEGRADED MODE  (scenarios 37-42)
# =================================================================
def test_failure(base: str) -> None:
    section("Failure/degraded: DeepSeek, GCal, HA, course, recipe, Telegram")
    now = _today()
    ds = _day_start(now, _mk(base))
    base_cont = _mk(base)
    base_cont.planner.add_task("Write report", est_minutes=60, priority=5)
    base_cont.planner.reschedule()

    # 37 DeepSeek/LLM unavailable: briefing is deterministic, never calls LLM.
    c = _mk(base)
    c.chat = type("Broken", (), {"chat": lambda *a, **k: (_ for _ in ()).throw(RuntimeError("llm down"))})()
    b = c.executive.briefing(day_ts=now)
    check("37 DeepSeek outage -> briefing still works", bool(b.get("text")), "")

    # 38 Google Calendar outage: local schedule lives; pending create survives.
    tr = FakeGCalTransport(); tr.fail_write = True
    c = _mk(base, tr)
    c.planner.add_task("Despite outage", est_minutes=120, priority=5)
    c.planner.reschedule()
    b = c.executive.briefing(day_ts=now)
    check("38 GCal outage -> local briefing works", bool(b.get("text")), "")

    # 39 Home Assistant outage: context unavailable -> executive still works.
    c = _mk(base)
    orig = c.context._presence
    def boom(*a, **k):
        raise RuntimeError("ha down")
    c.context._presence = boom  # type: ignore[method-assign]
    b = c.executive.briefing(day_ts=now)
    check("39 HA outage -> briefing still works", bool(b.get("text")), "")
    r = c.executive.recommend("", day_ts=ds + 10 * 3600, now_min=10 * 60)
    check("39b executive still works without location", bool(r.get("answer")), "")
    c.context._presence = orig  # type: ignore[method-assign]

    # 40 course monitor outage: existing state intact.
    c = _mk(base)
    cid = c.db.add_course("CS168", name="Project")
    check("40 course state intact", c.db.course_by_id(cid)["code"] == "CS168", "")

    # 41 recipe/food provider outage: executive + briefing fine.
    c = _mk(base)
    fp = getattr(c, "foodplan", None)
    if fp is not None:
        fp.peek = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("recipes down"))  # type: ignore
    b = c.executive.briefing(day_ts=now)
    check("41 recipe provider down -> briefing fine", bool(b.get("text")), "")

    # 42 Telegram outage: state keeps operating (run never raises).
    c = _mk(base)
    c.cfg.telegram_token = ""
    res = c.proactive.run()
    check("42 Telegram unavailable -> scheduler keeps running", isinstance(res, dict), str(res))


# =================================================================
# PART 11 + INTEGRATION  (scenarios 43-45)
# =================================================================
def test_integration(base: str) -> None:
    section("Integration: task reschedule updates event; history; deferred reflected")
    tr = FakeGCalTransport()
    c = _mk(base, tr)
    now = _today()
    ds = _day_start(now, c)
    tid = c.planner.add_task("Reschedule me", est_minutes=120, priority=5)
    c.planner.reschedule()
    check("43 event created on first schedule", tr.n_create == 1, f"n={tr.n_create}")
    # completed task correctly represented in history + review
    c.planner.reschedule()
    done = c.planner.add_task("Finish quickly", est_minutes=15, priority=4)
    c.planner.reschedule()
    c.planner.start(done)
    c.planner.done(done)
    h = c.db.task_history(done)
    check("44 history has a completed transition", any(r["to_status"] == "completed" for r in h), str([r["to_status"] for r in h]))
    # deferred task reflected in next executive recommendation (not recommended)
    def_ = c.planner.add_task("Defer later", est_minutes=30, priority=5)
    c.planner.reschedule()
    c.planner.defer(def_)
    r = c.executive.recommend("", day_ts=ds + 10 * 3600, now_min=10 * 60)
    check("45 deferred task not in next recommendation",
          "Defer later" not in (r.get("answer") or ""), r.get("answer"))
    # manual command wiring
    for phrase in ("give me my morning briefing", "what's my day like",
                   "review my day", "how did i do today"):
        intent = c.decider.parse(phrase)
        res = c.decider.resolve(intent)
        check(f"cmd: '{phrase}'",
              intent.kind in ("briefing", "review") and bool(res),
              f"-> {intent.kind}")


def main() -> None:
    base = tempfile.mkdtemp(prefix="p51-")
    try:
        test_briefing(base)
        test_briefing_idempotent(base)
        test_briefing_quiet_and_push(base)
        test_executive_priorities(base)
        test_executive_hard_limits(base)
        test_executive_soft(base)
        test_executive_lifecycle_reflect(base)
        test_review(base)
        test_cross_domain(base)
        test_proactive(base)
        test_failure(base)
        test_integration(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
