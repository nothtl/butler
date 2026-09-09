"""Phase 4.4 acceptance tests for Butler learned routines & habits.

Run:  .venv/bin/python tests/run_acceptance_p44.py

Proves the "learned routines" feature is deterministic, explainable and SOFT:

  1. a repeated behaviour surfaces as a candidate routine (scan/detect)
  2. a one-off never becomes a routine
  3. scattered or cross-weekday events do NOT group into a routine
  4. zone-aware repetition is detected (and never stores raw GPS)
  5. a repeated A-then-B sequence on the same day is detected
  6. confirming a candidate promotes it to active (state confirmed)
  7. rejecting a candidate marks it declined and it never influences
  8. a confirmed routine nudges the "what now?" recommendation
  9. a confirmed routine never rewrites the committed day plan (soft-only)
 10. an explicit user preference (+6) always out-ranks a routine boost (+4)
 11. a routine boost never pushes a task inside a hard calendar event
 12. a routine boost never pushes a task past its deadline
 13. "I no longer want to go to the gym on Mondays" disables the routine
 14. forget() disables a routine so it stops influencing
 15. long-uncontacted routines decay to stale, then reactivate
 16. detection is deterministic and never touches an LLM
 17. the Telegram/CLI routine intents resolve to a readable reply

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
from butler.house import HomeAssistant  # noqa: E402
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


def _tracker(eid: str, state: str, **attrs) -> dict:
    return {"entity_id": eid, "state": state, "attributes": attrs}


class FakeHA:
    def __init__(self, states=None, status: int = 200):
        self._states = states if states is not None else []
        self._status = status

    def get(self, url: str, headers=None) -> tuple[int, object]:
        return self._status, self._states


def _cfg(base: str, enabled: bool = True, token: str = "secret-token") -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.home_assistant_enabled = enabled
    cfg.home_assistant_url = "http://ha.local:8123"
    cfg.home_assistant_token = token
    cfg.ensure_dirs()
    return cfg


def _ha_for(cfg: Config, zone: str = "") -> HomeAssistant:
    if not zone:
        return HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "unknown", location_name="Unknown",
                     battery=72, source_type="gps")]))
    return HomeAssistant(cfg, http=FakeHA(states=[
        _tracker("device_tracker.phone", zone, location_name=zone,
                 battery=72, source_type="gps")]))


def _container(base: str, zone: str = "") -> Container:
    sub = tempfile.mkdtemp(prefix="c-", dir=base)
    cfg = _cfg(sub)
    c = Container(cfg)
    c.ha = _ha_for(cfg, zone)
    return c


def _gps_in(text: str) -> bool:
    return bool(re.search(r"[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?", str(text)))


def _this_monday() -> datetime.date:
    today = datetime.date.today()
    return today - datetime.timedelta(days=today.weekday())


def _mon_dt(weeks_ago: int = 0) -> datetime.datetime:
    monday = _this_monday() - datetime.timedelta(weeks=weeks_ago)
    return datetime.datetime(monday.year, monday.month, monday.day, 0, 0)


def _mon_ts(hour: int, minute: int = 0, weeks_ago: int = 0) -> int:
    return int(_mon_dt(weeks_ago).replace(hour=hour, minute=minute).timestamp())


def _mon_midnight() -> int:
    return int(_mon_dt(0).timestamp())


def _day_slots(plan: dict) -> list[dict]:
    from_json = sch.PlanState.from_json(plan["json"])
    return [{"task_id": s.task_id, "title": s.title,
             "start_min": s.start_min, "end_min": s.end_min}
            for s in from_json.slots]


def _seed_weekly(c: Container, title: str, hour: int, minute: int = 0,
                 weeks: tuple[int, ...] = (1, 2, 3, 4), tid0: int = 1000) -> None:
    """Record a task completion called ``title`` each week on the Monday."""
    for i, wk in enumerate(weeks):
        c.timeline.record_task_completed(tid0 + i, title, ts=_mon_ts(hour, minute, wk))


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p44-")
    try:
        # ============ 4.4.1 repeated behaviour -> candidate ============
        print("\n=== 4.4.1 Repeated behaviour surfaces a candidate ===")
        c = _container(base)
        _seed_weekly(c, "Workout", 17, 0)
        res = c.routines.scan()
        cands = res["candidates"]
        exercise = [x for x in cands
                    if x["kind"] == "activity" and x["category"] == "exercise"]
        check("a repeated weekly workout becomes a candidate routine",
              len(exercise) == 1 and exercise[0]["state"] == "candidate",
              f"{[(x.get('category'), x.get('count')) for x in cands]}")
        check("the candidate carries a learned frequency and weekday",
              exercise[0]["count"] >= 3 and exercise[0]["weekday"] == 0,
              f"count={exercise[0]['count']} weekday={exercise[0]['weekday']}")
        check("the candidate observed the exact 17:00 window",
              exercise[0]["start_min"] == 17 * 60,
              f"start_min={exercise[0]['start_min']}")

        # ============ 4.4.2 a one-off never becomes a routine ============
        print("\n=== 4.4.2 A one-off never becomes a routine ===")
        c2 = _container(base)
        c2.timeline.record_task_completed(1, "Workout", ts=_mon_ts(17, 0, 1))
        check("a single observation does not surface a candidate",
              len(c2.routines.scan()["candidates"]) == 0,
              f"candidates={len(c2.routines.scan()['candidates'])}")

        # ============ 4.4.3 scattered / cross-weekday never groups ============
        print("\n=== 4.4.3 Scattered or cross-weekday times never group ===")
        c3 = _container(base)
        # same weekday but wildly different times
        for i, (h, wk) in enumerate([(8, 1), (20, 2), (21, 3), (19, 4)]):
            c3.timeline.record_task_completed(10 + i, "Workout", ts=_mon_ts(h, 0, wk))
        # same time but on different weekdays
        for i, (wk, dadd) in enumerate([(1, 0), (2, 1), (3, 2)]):
            ts = _mon_ts(17, 0, wk) + dadd * 86400
            c3.timeline.record_task_completed(20 + i, "Workout", ts=ts)
        check("neither scattered nor cross-weekday events form a routine",
              len(c3.routines.scan()["candidates"]) == 0,
              f"candidates={len(c3.routines.scan()['candidates'])}")

        # ============ 4.4.4 zone-aware detection, no raw GPS ============
        print("\n=== 4.4.4 Zone-aware repetition is detected ===")
        c4 = _container(base)
        for wk in (1, 2, 3, 4):
            c4.timeline.establish_zone("Office", ts=_mon_ts(16, 30, wk))
            c4.timeline.establish_zone("Gym", ts=_mon_ts(17, 0, wk))
        cands4 = c4.routines.scan()["candidates"]
        gym = [x for x in cands4
               if x["kind"] == "activity" and x["zone"].lower() == "gym"]
        check("a repeated weekly gym visit is a zone-aware candidate",
              len(gym) == 1 and gym[0]["weekday"] == 0,
              f"zone_cands={[(x['kind'], x['zone']) for x in cands4]}")
        check("the detected routine stores a zone name, never a GPS point",
              gym and not _gps_in(gym[0]["zone"]),
              f"zone={gym[0]['zone'] if gym else None}")
        db_bytes = b"".join(str(r).encode() for r in c4.db.query("SELECT * FROM routines"))
        check("no raw GPS coordinate is stored anywhere in the routine store",
              not _gps_in(db_bytes))

        # ============ 4.4.5 sequence detection ============
        print("\n=== 4.4.5 A repeated A-then-B sequence is detected ===")
        c5 = _container(base)
        for i, wk in enumerate((1, 2, 3, 4)):
            c5.timeline.record_task_completed(100 + i, "Lecture", ts=_mon_ts(16, 0, wk))
            c5.timeline.record_task_completed(200 + i, "Workout", ts=_mon_ts(17, 0, wk))
        cands5 = c5.routines.scan()["candidates"]
        seq = [x for x in cands5 if x["kind"] == "sequence"]
        check("a repeated study->exercise chain is a candidate sequence",
              len(seq) == 1 and seq[0]["category"] == "study->exercise",
              f"{[(x.get('category')) for x in seq]}")

        # ============ 4.4.6 confirm promotes to active ============
        print("\n=== 4.4.6 Confirming promotes a candidate to active ===")
        c6 = _container(base)
        _seed_weekly(c6, "Workout", 17, 0)
        c6.routines.scan()
        check("a candidate is present before confirm",
              len(c6.routines.candidates()) == 1)
        conf = c6.routines.confirm()
        check("confirm promotes the candidate to confirmed",
              conf.get("ok") and conf["routine"]["state"] == "confirmed",
              str(conf.get("routine", {}).get("state")))
        check("the confirmed routine is now active",
              len(c6.routines.active()) == 1,
              f"active={len(c6.routines.active())}")

        # ============ 4.4.7 reject marks declined, never influences ============
        print("\n=== 4.4.7 Rejecting marks a routine declined ===")
        c7 = _container(base)
        _seed_weekly(c7, "Workout", 17, 0)
        c7.routines.scan()
        rej = c7.routines.reject()
        check("reject marks the candidate declined",
              rej.get("ok") and rej["routine"]["state"] == "declined",
              str(rej.get("routine", {}).get("state")))
        check("a declined routine is never active and never boosts",
              len(c7.routines.active()) == 0
              and c7.routines.affinity_for("Workout", day_ts=_mon_midnight(),
                                           now_min=17 * 60)["score"] == 0,
              f"active={len(c7.routines.active())}")

        # ============ 4.4.8 confirmed routine nudges "what now?" ============
        print("\n=== 4.4.8 A confirmed routine nudges the recommendation ===")
        c8 = _container(base)
        c8.routines.create_explicit("I always go to the gym on Monday")
        c8.db.add_event("Lecture", _mon_ts(7, 0), _mon_ts(16, 30), source="local")
        c8.planner.add_task("Workout", est_minutes=60)
        c8.planner.add_task("Cook dinner", est_minutes=60)
        r8 = c8.planner.what_now("", day_ts=_mon_midnight(), now_min=17 * 60)
        cand8 = r8.get("candidate") or {}
        check("the routine time favours the workout",
              "Workout" in cand8.get("title", ""), str(cand8.get("title")))
        check("the routine influence is reported",
              r8.get("routine_influenced") is True,
              f"routine_influenced={r8.get('routine_influenced')}")
        check("the reason explains the habit",
              "usually" in r8.get("reason", "") and "17:00" in r8.get("reason", ""),
              r8.get("reason"))

        # ============ 4.4.9 soft-only: the committed day plan never moves ============
        print("\n=== 4.4.9 A routine never rewrites the committed day plan ===")
        c9 = _container(base)
        c9.planner.add_task("Workout", est_minutes=60)
        c9.planner.add_task("Cook dinner", est_minutes=60)
        c9.planner.reschedule(_mon_midnight())
        plan_without = _day_slots(c9.db.latest_plan())
        c9.routines.create_explicit("I always go to the gym on Monday")
        c9.planner.reschedule(_mon_midnight())
        plan_with = _day_slots(c9.db.latest_plan())
        check("plan_day is identical with and without a confirmed routine",
              plan_without == plan_with,
              f"{plan_without} vs {plan_with}")

        # ============ 4.4.10 explicit (+6) always out-ranks a routine (+4) ============
        print("\n=== 4.4.10 An explicit preference out-ranks a soft routine ===")
        c10 = _container(base)
        c10.routines.create_explicit("I always go to the gym on Monday")
        c10.db.add_event("Lecture", _mon_ts(7, 0), _mon_ts(16, 30), source="local")
        c10.planner.add_task("Workout", est_minutes=60)
        c10.planner.add_task("Cook dinner", est_minutes=60)
        r10 = c10.planner.what_now("I want to cook dinner", day_ts=_mon_midnight(),
                                   now_min=17 * 60)
        cand10 = r10.get("candidate") or {}
        check("an explicit cooking preference beats the gym routine",
              "Cook" in cand10.get("title", ""), str(cand10.get("title")))
        check("the pick is reported as an explicit user override, not a routine",
              r10.get("user_override") is True
              and r10.get("routine_influenced") is False,
              f"uo={r10.get('user_override')} routine={r10.get('routine_influenced')}")

        # ============ 4.4.11 never overrides a hard event ============
        print("\n=== 4.4.11 A routine never overrides a hard calendar event ===")
        c11 = _container(base)
        c11.routines.create_explicit("I always go to the gym on Monday")
        c11.db.add_event("Lecture", _mon_ts(6, 0), _mon_ts(18, 0), source="local")
        c11.planner.add_task("Workout", est_minutes=120, deadline=_mon_ts(22))
        r11 = c11.planner.what_now("", day_ts=_mon_midnight(), now_min=7 * 60)
        cand11 = r11.get("candidate") or {}
        check("the routine boost never pushes the workout into the lecture",
              cand11.get("start") is None or cand11.get("start") >= "18:00",
              str(cand11.get("start")))

        # ============ 4.4.12 never violates a deadline ============
        print("\n=== 4.4.12 A routine never pushes a task past its deadline ===")
        c12 = _container(base)
        c12.routines.create_explicit("I always go to the gym on Monday")
        c12.db.add_event("Lecture", _mon_ts(7, 0), _mon_ts(18, 0), source="local")
        c12.planner.add_task("Workout", est_minutes=120, deadline=_mon_ts(17, 0))
        r12 = c12.planner.what_now("", day_ts=_mon_midnight(), now_min=7 * 60)
        check("no recommendation is made once the only slot is past the deadline",
              r12.get("now") is None, str(r12.get("now")))

        # ============ 4.4.13 negative statement disables the routine ============
        print("\n=== 4.4.13 'no thanks to the gym' disables the routine ===")
        c13 = _container(base)
        c13.routines.create_explicit("I always go to the gym on Monday")
        neg = c13.routines.create_explicit(
            "I don't want to go to the gym on Mondays anymore")
        check("the negative statement disables the matching routine",
              neg.get("ok") and neg.get("action") == "disable",
              str(neg))
        check("no confirmed routine remains after the disable",
              len(c13.routines.confirmed()) == 0,
              f"confirmed={len(c13.routines.confirmed())}")
        r13 = c13.planner.what_now("", day_ts=_mon_midnight(), now_min=17 * 60)
        check("the disabled routine no longer influences the recommendation",
              r13.get("routine_influenced") is False,
              f"routine_influenced={r13.get('routine_influenced')}")

        # ============ 4.4.14 forget() disables an active routine ============
        print("\n=== 4.4.14 forget() disables a routine ===")
        c14 = _container(base)
        c14.routines.create_explicit("I always go to the gym on Monday")
        check("a routine is active before forget", len(c14.routines.active()) == 1)
        fg = c14.routines.forget()
        check("forget disables the active routine",
              fg.get("ok") and len(c14.routines.confirmed()) == 0
              and len(c14.routines.active()) == 0,
              str(fg))

        # ============ 4.4.15 stale decay + reactivation ============
        print("\n=== 4.4.15 Long-untouched routines decay, then reactivate ===")
        c15 = _container(base)
        c15.routines.create_explicit("I always go to the gym on Monday")
        now = int(datetime.datetime.now().timestamp())
        c15.db.execute("UPDATE routines SET last_ts=? WHERE state='confirmed'",
                       (now - 30 * 86400,))
        decayed = c15.routines.decay()
        check("a routine untouched past the stale window decays to stale",
              decayed == 1 and len(c15.routines.active()) == 0,
              f"decayed={decayed}")
        react = c15.routines.reactivate()
        check("a stale routine can be reactivated (back to confirmed)",
              react.get("ok") and react["routine"]["state"] == "confirmed"
              and len(c15.routines.confirmed()) == 1,
              str(react.get("routine", {}).get("state")))

        # ============ 4.4.16 deterministic, never an LLM ============
        print("\n=== 4.4.16 Detection is deterministic and never an LLM ===")
        c16 = _container(base)
        _seed_weekly(c16, "Workout", 17, 0)
        now = int(datetime.datetime.now().timestamp())
        events = c16.timeline.get_between(now - 90 * 86400, now)
        a = c16.routines.detect(events, now)
        b = c16.routines.detect(events, now)
        check("identical input yields an identical detection",
              a == b, f"len(a)={len(a)} len(b)={len(b)}")
        check("the routine module never touches an LLM",
              not hasattr(c16.routines, "agent"),
              f"has_agent={hasattr(c16.routines, 'agent')}")

        # ============ 4.4.17 routines intents resolve (Telegram/CLI) ============
        print("\n=== 4.4.17 Routine intents resolve to a readable reply ===")
        c17 = _container(base)
        _seed_weekly(c17, "Workout", 17, 0)
        c17.routines.scan()
        cases = ("show my routines", "make it a routine", "/routines")
        all_ok = True
        replies = []
        for q in cases:
            intent = c17.decider.parse(q)
            result = c17.decider.resolve(intent, "user")
            tok = result.get("kind") if isinstance(result, dict) else ""
            text = result.get("text", "") if isinstance(result, dict) else ""
            if tok != "routines" or not text:
                all_ok = False
            replies.append((q, tok, text.split("\n")[0]))
        check("routine intents resolve to kind 'routines' with readable text",
              all_ok, str(replies))
        c18 = _container(base)
        res = c18.decider.resolve(c18.decider.parse("i always go to the gym on monday"),
                                  "user")
        check("an explicit routine intent is acknowledged",
              res.get("kind") == "routines" and res.get("routine") is not None,
              str(res.get("error", "")))

        for cc in (c, c2, c3, c4, c5, c6, c7, c8, c9,
                   c10, c11, c12, c13, c14, c15, c16, c17, c18):
            try:
                cc.db.close()
            except Exception:  # noqa: BLE001
                pass
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\nP44: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
