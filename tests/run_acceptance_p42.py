"""Phase 4.2 acceptance tests for Butler context-aware decisions.

Run:  .venv/bin/python tests/run_acceptance_p42.py

Proves "what should I do now?" becomes geo-aware while the scheduler stays the
single source of truth and hard constraints (events/deadlines) are never
silently overridden:

   1. "at the library" biases the recommendation toward study
   2. "at the gym" biases toward exercise
   3. "at the dorm" never recommends a heavy workout (cook/rest/study win)
   4. unknown/unavailable presence falls back exactly to the baseline
   5. presence never overrides a hard calendar event
   6. a recommendation never lands past a task's deadline
   7. "I want to exercise" overrides a library inference
   8. "I don't want to study" is respected even at the library
   9. the recommendation explains itself (reason carries the location)
  10. "why this?" resolves and matches the current recommendation
  11. a location change never silently reschedules (only offers to move)
  12. an explicit "move my <task> block" triggers a deterministic reschedule
  13. an HA outage degrades to the baseline without crashing
  14. no raw GPS coordinate is logged/persisted
  15. the HA token never leaks through the recommendation
  16. the committed day plan is unaffected by context (scheduler is authoritative)

Run everything:
  .venv/bin/python tests/test_schedule.py
  .venv/bin/python tests/run_acceptance.py
  .venv/bin/python tests/run_acceptance_p3.py
  .venv/bin/python tests/run_acceptance_p35.py
  .venv/bin/python tests/run_acceptance_p36.py
  .venv/bin/python tests/run_acceptance_p37.py
  .venv/bin/python tests/run_acceptance_p41.py
  .venv/bin/python tests/run_acceptance_p42.py
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.house import HAOutage, HomeAssistant  # noqa: E402
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


class RaisingHA(HomeAssistant):
    def states(self):
        raise HAOutage("backend down")


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


def _today_ts() -> int:
    d = datetime.date.today()
    return int(datetime.datetime(d.year, d.month, d.day, 0, 0).timestamp())


def _ts(hour: int, minute: int = 0) -> int:
    d = datetime.date.today()
    return int(datetime.datetime(d.year, d.month, d.day, hour, minute).timestamp())


def _day_slots(plan: dict) -> list[dict]:
    from_json = sch.PlanState.from_json(plan["json"])
    return [{"task_id": s.task_id, "title": s.title,
             "start_min": s.start_min, "end_min": s.end_min}
            for s in from_json.slots]


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p42-")
    try:
        day_ts = _today_ts()
        now_min = 7 * 60  # 07:00 — the default working-window floor

        # ============ 4.2.1 library -> study ============
        print("\n=== 4.2.1 At the library -> study ===")
        c = _container(base, zone="Library")
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Rewrite resume", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate") or {}
        check("library biases the recommendation toward study",
              "Study" in cand.get("title", ""), str(cand.get("title")))
        check("library influence is flagged",
              r.get("ha_influence") is True and r.get("context_influenced") is True,
              f"ha={r.get('ha_influence')} ctx={r.get('context_influenced')}")

        # ============ 4.2.2 gym -> exercise ============
        print("\n=== 4.2.2 At the gym -> exercise ===")
        c = _container(base, zone="Gym")
        c.planner.add_task("Exercise interval run", est_minutes=60)
        c.planner.add_task("Cook instant noodles", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate") or {}
        check("gym biases the recommendation toward exercise",
              "Exercise" in cand.get("title", ""), str(cand.get("title")))

        # ============ 4.2.3 dorm -> sane (no heavy workout) ============
        print("\n=== 4.2.3 At the dorm -> sane ===")
        c = _container(base, zone="Dorm")
        c.planner.add_task("Cook dinner", est_minutes=60)
        c.planner.add_task("Exercise cardio", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate") or {}
        check("at the dorm a cooking task beats a heavy workout",
              "Cook" in cand.get("title", ""), str(cand.get("title")))

        # ============ 4.2.4 unknown -> baseline ============
        print("\n=== 4.2.4 Unknown presence -> baseline ===")
        c = _container(base, zone="")
        study_id = c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Cook dinner", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate") or {}
        check("unknown presence never influences the pick",
              r.get("context_influenced") is False and r.get("ha_influence") is False,
              f"ctx={r.get('context_influenced')} ha={r.get('ha_influence')}")
        check("unknown presence keeps the deterministic baseline (lowest id)",
              cand.get("task_id") == study_id, str(cand.get("task_id")))

        # ============ 4.2.5 never overrides a hard event ============
        print("\n=== 4.2.5 Hard event wins over location ===")
        c = _container(base, zone="Library")
        c.db.add_event("Lecture", _ts(7), _ts(18), source="local")
        c.planner.add_task("Study CS168 homework", est_minutes=120)
        c.planner.add_task("Cook dinner", est_minutes=120)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate") or {}
        # Reparse the solved day and confirm the chosen slot never overlaps 07-18.
        log = r.get("decision_log", {})
        check("presence never pushes a task inside a hard event",
              cand.get("start") is None or cand.get("start") >= "18:00", str(cand.get("start")))

        # ============ 4.2.6 never violates a deadline ============
        print("\n=== 4.2.6 Deadline always respected ===")
        c = _container(base, zone="Library")
        c.db.add_event("Lecture", _ts(7), _ts(18), source="local")
        c.planner.add_task("Study CS168 homework", est_minutes=120, deadline=_ts(17))
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        check("no recommendation is made once the task is past its deadline",
              r.get("now") is None, str(r.get("now")))

        # ============ 4.2.7 explicit preference overrides location ============
        print("\n=== 4.2.7 'I want to exercise' beats the library ===")
        c = _container(base, zone="Library")
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Exercise run", est_minutes=60)
        r = c.planner.what_now("I want to exercise", day_ts=day_ts, now_min=now_min)
        cand = r.get("candidate") or {}
        check("an explicit request overrides a location inference",
              "Exercise" in cand.get("title", ""), str(cand.get("title")))
        check("the override is reported as a user override",
              r.get("user_override") is True, f"uo={r.get('user_override')}")

        # ============ 4.2.8 'I don't want to study' respected ============
        print("\n=== 4.2.8 'I don't want to study' respected ===")
        c = _container(base, zone="Library")
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Exercise run", est_minutes=60)
        r = c.planner.what_now("I don't want to study right now", day_ts=day_ts,
                               now_min=now_min)
        cand = r.get("candidate") or {}
        check("being at the library never overrides an explicit 'no study'",
              "Exercise" in cand.get("title", ""), str(cand.get("title")))

        # ============ 4.2.9 explains itself ============
        print("\n=== 4.2.9 It explains itself ===")
        c = _container(base, zone="Library")
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Rewrite resume", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        check("the recommendation carries a reason",
              bool(r.get("reason")), repr(r.get("reason")))
        check("the reason names the location",
              "Library" in r.get("reason", "") and "study" in r.get("reason", ""),
              r.get("reason"))

        # ============ 4.2.10 'why this?' works ============
        print("\n=== 4.2.10 Why this? ===")
        c = _container(base, zone="Library")
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Rewrite resume", est_minutes=60)
        intent = c.decider.parse("why this?")
        check("parse returns why_this intent", intent.kind == "why_this", intent.kind)
        check("resolve returns a why_this payload", c.decider.resolve(intent).get("kind")
              == "why_this", c.decider.resolve(intent).get("kind"))
        why = c.planner.explain_now("", day_ts=day_ts, now_min=now_min)
        now = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        check("why_this matches the current recommendation",
              (why.get("candidate") or {}).get("title")
              == (now.get("candidate") or {}).get("title"),
              str((why.get("candidate") or {}).get("title")))
        check("why_this explains with a reason", bool(why.get("reason")),
              repr(why.get("reason")))

        # ============ 4.2.11 location change does not silently reschedule ============
        print("\n=== 4.2.11 Location change never silently reschedules ===")
        c = _container(base, zone="Trader Joe's")
        c.planner.add_task("Study CS168 homework", est_minutes=1440)
        before = c.planner.plan_day(day_ts)
        before_json = c.db.latest_plan()["json"]
        intent = c.decider.parse("I'm at Trader Joe's")
        check("parse returns location_change intent", intent.kind == "location_change",
              intent.kind)
        res = c.decider.resolve(intent)
        after_json = c.db.latest_plan()["json"]
        check("a location change never mutates the committed plan",
              before_json == after_json, f"moved={res.get('moved')}")
        check("location change only offers to move (moved=False)",
              res.get("moved") is False, str(res.get("moved")))
        check("location change recognises a suitable context",
              res.get("context_categories") == ["shop"], str(res.get("context_categories")))

        # ============ 4.2.12 explicit move triggers deterministic reschedule ============
        print("\n=== 4.2.12 Explicit move -> deterministic reschedule ===")
        c = _container(base, zone="")
        c.db.add_event("Lecture", _ts(9), _ts(11), source="local")
        c.planner.add_task("Study CS168 homework", est_minutes=120)
        c.planner.add_task("Cook dinner", est_minutes=120)
        intent = c.decider.parse("move my study block")
        check("parse returns move_block intent", intent.kind == "move_block", intent.kind)
        res = c.decider.resolve(intent)
        a = _day_slots(c.db.latest_plan())
        check("explicit move re-solves the day deterministically",
              res.get("kind") == "move_block" and isinstance(res.get("moved"), list),
              f"moved={res.get('moved')}")
        # The deterministic scheduler never overlaps a hard event.
        over = any(s["start_min"] < 660 and s["end_min"] > 540 for s in a)
        check("the re-solved plan never overlaps the hard event",
              not over, str(a))
        # reschedule again -> identical (the deterministic scheduler is authoritative)
        c.planner.reschedule(day_ts)
        b = _day_slots(c.db.latest_plan())
        check("a repeated reschedule is idempotent (deterministic solver)",
              a == b, f"{a} vs {b}")

        # ============ 4.2.13 HA outage fallback ============
        print("\n=== 4.2.13 HA outage -> baseline ===")
        cfg = _cfg(base)
        c = Container(cfg)
        c.ha = RaisingHA(cfg)
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        c.planner.add_task("Cook dinner", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        check("an HA outage never breaks what now",
              r.get("ok") is True and (r.get("now") is not None), str(r.get("now")))
        check("an HA outage yields no location influence",
              r.get("ha_influence") is False and r.get("context_influenced") is False,
              f"ha={r.get('ha_influence')} ctx={r.get('context_influenced')}")
        check("an HA outage preserves the baseline answer",
              str(r.get("answer", "")).startswith("Now: "), str(r.get("answer")))

        # ============ 4.2.14 no raw GPS persisted ============
        print("\n=== 4.2.14 No raw GPS ===")
        cfg = _cfg(base)
        c = Container(cfg)
        c.ha = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.gps", "33.693,-75.123", source_type="gps")]))
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        blob = str(r.get("decision_log")) + str(r.get("answer")) + str(r.get("reason"))
        check("a raw GPS coordinate is never logged/persisted",
              not blob or "33.693" not in blob and "-75.123" not in blob, blob[:120])
        check("a raw GPS tracker resolves to no zone",
              r.get("decision_log", {}).get("zone") in ("", None),
              str(r.get("decision_log", {}).get("zone")))

        # ============ 4.2.15 no token leak ============
        print("\n=== 4.2.15 No token leak ===")
        cfg = _cfg(base)
        c = Container(cfg)
        c.ha = _ha_for(cfg, "Library")
        c.planner.add_task("Study CS168 homework", est_minutes=60)
        r = c.planner.what_now("", day_ts=day_ts, now_min=now_min)
        blob = str(r)
        check("the recommendation never leaks the HA token",
              "secret-token" not in blob)

        # ============ 4.2.16 committed plan unaffected by context ============
        print("\n=== 4.2.16 Scheduled plan is context-neutral ===")
        c1 = _container(base, zone="Library")
        c1.planner.add_task("Study CS168 homework", est_minutes=60)
        c1.planner.add_task("Cook dinner", est_minutes=60)
        p1 = _day_slots(c1.planner.plan_day(day_ts) and c1.db.latest_plan())
        c2 = _container(base, zone="")
        c2.planner.add_task("Study CS168 homework", est_minutes=60)
        c2.planner.add_task("Cook dinner", est_minutes=60)
        p2 = _day_slots(c2.planner.plan_day(day_ts) and c2.db.latest_plan())
        check("presence never changes the committed day plan (solver authoritative)",
              p1 == p2, f"{p1} vs {p2}")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
