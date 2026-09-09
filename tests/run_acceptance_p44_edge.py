"""Phase 4.4 (routines/habits) acceptance tests — lifecycle + isolation edge cases.

Run:  .venv/bin/python tests/run_acceptance_p44_edge.py

Complements the main ``run_acceptance_p44.py`` (the 17 behavioral checks) by
asserting the Phase 4.4 guarantees that are hardest to see in a single run:

  1. weak evidence (a one-off) never surfaces a candidate
  2. repeated behaviour creates ONE candidate, never duplicates
  3. re-scanning (as if Butler restarted) does not duplicate routines
  4. a confirmed routine is stored distinctly from a candidate
  5. a rejected routine never influences scheduling
  6. a separate config/data_dir never sees another user's routines
  7. candidates persist separately from confirmed routines
  8. confirm()/reject() lifecycle is deterministic and reversible (decline,
     then a fresh explicit statement re-creates)

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
  .venv/bin/python tests/run_acceptance_p44.py
  .venv/bin/python tests/run_acceptance_config_audit.py
  .venv/bin/python tests/run_acceptance_config_audit_verify.py
  .venv/bin/python tests/run_acceptance_p44_edge.py
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config      # noqa: E402
from butler.core import Container     # noqa: E402

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


def _cfg(base: str, sub: str) -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, sub)
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.routines_min_observations = 2
    cfg.ensure_dirs()
    return cfg


def _this_monday() -> datetime.date:
    t = datetime.date.today()
    return t - datetime.timedelta(days=t.weekday())


def _mon_ts(hour: int, minute: int = 0, weeks_ago: int = 0) -> int:
    d = _this_monday() - datetime.timedelta(weeks=weeks_ago)
    return int(datetime.datetime(d.year, d.month, d.day, hour, minute).timestamp())


def test_weak_evidence_no_routine():
    print("\n=== 1. Weak evidence never becomes a routine ===")
    c = _cfg(tempfile.mkdtemp(prefix="p44e-"), "weak")
    ct = Container(c)
    ct.timeline.record_task_completed(1, "Workout", ts=_mon_ts(17, 0, 1))
    check("a single observation yields no candidate",
          len(ct.routines.scan()["candidates"]) == 0,
          f"candidates={len(ct.routines.scan()['candidates'])}")


def test_no_duplicates_on_rescan():
    print("\n=== 2. Repeated behaviour creates ONE candidate, not duplicates ===")
    c = _cfg(tempfile.mkdtemp(prefix="p44e-"), "dedupe")
    ct = Container(c)
    for i, wk in enumerate((0, 1, 2, 3)):
        ct.timeline.record_task_completed(i + 1, "Workout", ts=_mon_ts(17, 0, wk))
    res = ct.routines.scan()
    cats = [x["category"] for x in res["candidates"]]
    check("exactly one exercise candidate", cats.count("exercise") == 1, str(cats))
    n1 = len(ct.db.query("SELECT * FROM routines"))
    # Scan 3 more times (as if Butler restarted) — identity is signature-keyed.
    for _ in range(3):
        ct.routines.scan()
    n2 = len(ct.db.query("SELECT * FROM routines"))
    check("rescanning never duplicates rows", n1 == n2 and n1 == 1, f"{n1} -> {n2}")


def test_candidate_vs_confirmed_stored_distinctly():
    print("\n=== 3. Candidates and confirmed routines are stored separately ===")
    c = _cfg(tempfile.mkdtemp(prefix="p44e-"), "states")
    ct = Container(c)
    for i, wk in enumerate((0, 1, 2, 3)):
        ct.timeline.record_task_completed(i + 1, "Workout", ts=_mon_ts(17, 0, wk))
    ct.routines.scan()
    rid = ct.routines.candidates()[0]["id"]
    check("starts as a candidate", ct.routines.active() == [])
    ct.routines.confirm(rid)
    check("confirm promotes to an active routine", len(ct.routines.active()) == 1)
    confirmed = ct.routines.confirmed()
    check("confirmed row keeps the id, distinct store",
          len(confirmed) == 1 and confirmed[0]["id"] == rid,
          f"states={[r['state'] for r in ct.db.query('SELECT state FROM routines')]}")


def test_rejected_never_influences():
    print("\n=== 4. A rejected routine never influences scheduling ===")
    c = _cfg(tempfile.mkdtemp(prefix="p44e-"), "reject")
    ct = Container(c)
    for i, wk in enumerate((0, 1, 2, 3)):
        ct.timeline.record_task_completed(i + 1, "Workout", ts=_mon_ts(17, 0, wk))
    ct.routines.scan()
    rid = ct.routines.candidates()[0]["id"]
    ct.routines.reject(rid)
    check("reject marks the row declined", ct.routines.active() == [])
    mon = _mon_ts(17, 0, 0)
    aff = ct.routines.affinity_for(title="Workout", day_ts=mon, now_min=17 * 60)
    check("declined routine contributes zero score",
          aff.get("score", 0) == 0, str(aff))


def test_cross_config_isolation():
    print("\n=== 5. A separate configuration never sees another user's routines ===")
    base = tempfile.mkdtemp(prefix="p44e-")
    c1 = _cfg(base, "user-a")
    ct1 = Container(c1)
    for i, wk in enumerate((0, 1, 2, 3)):
        ct1.timeline.record_task_completed(i + 1, "Workout", ts=_mon_ts(17, 0, wk))
    ct1.routines.scan()
    check("user A learns an exercise routine",
          any(x["category"] == "exercise" for x in ct1.routines.candidates()))

    # A brand-new config + data_dir must be empty.
    c2 = _cfg(tempfile.mkdtemp(prefix="other-"), "user-b")
    ct2 = Container(c2)
    check("user B has no routine rows at all",
          len(ct2.db.query("SELECT * FROM routines")) == 0)
    check("user B sees no candidates", ct2.routines.scan()["candidates"] == [])


def test_lifecycle_reversible():
    print("\n=== 6. Lifecycle is deterministic and reversible ===")
    c = _cfg(tempfile.mkdtemp(prefix="p44e-"), "life")
    ct = Container(c)
    for i, wk in enumerate((0, 1, 2, 3)):
        ct.timeline.record_task_completed(i + 1, "Study", ts=_mon_ts(18, 0, wk))
    ct.routines.scan()
    rid = ct.routines.candidates()[0]["id"]
    ct.routines.reject(rid)
    check("rejected => not active", ct.routines.active() == [])
    # A fresh explicit statement re-creates a confirmed routine.
    res = ct.routines.create_explicit("I always study every Monday")
    check("explicit statement can re-create a routine",
          res.get("action") == "create" and len(ct.routines.active()) == 1,
          str(res))


def main() -> int:
    test_weak_evidence_no_routine()
    test_no_duplicates_on_rescan()
    test_candidate_vs_confirmed_stored_distinctly()
    test_rejected_never_influences()
    test_cross_config_isolation()
    test_lifecycle_reversible()

    print("\n==== RESULT: %d passed, %d failed ====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
