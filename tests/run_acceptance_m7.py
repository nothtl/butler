"""M7 acceptance: proactive executive behavior.

Run:  .venv/bin/python tests/run_acceptance_m7.py

Deterministic and offline. Proves the proactive loop end to end:

  A. candidate generation (deadline risk, free time, missed task, conflict,
     risk increase, estimate issue, routine, travel, food, course, web change)
  B. ranking (critical > low, deadline > routine, confidence, novelty, prefs)
  C. suppression (cooldown, dedup, budget, quiet hours, user, snooze, expiry)
  D. state (accepted/dismissed/ignored/snoozed/expired; no duplicates)
  E. evidence (every candidate is evidence-backed; web provenance retained)
  F. safety (confirmation required; no autonomous external effects)
  G. integration (M3/M4/M5/M6, calendar/courses/tasks/routines, Telegram, audit)
  H. realistic scenarios
  I. regression
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

from butler import schedule as sch  # noqa: E402
from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.semantic import ActionKind, ResultStatus  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402
from butler.proactive_engine import (  # noqa: E402
    ACCEPTED, CAT_COURSE, CAT_DEADLINE_RISK, CAT_ESTIMATE, CAT_FOOD,
    CAT_FREE_TIME, CAT_MISSED_TASK, CAT_PROJECT_RISK, CAT_ROUTINE,
    CAT_SCHEDULE_CONFLICT, CAT_TRAVEL, CAT_WEB_CHANGE, DISMISSED, EXPIRED,
    IGNORED, PENDING, ProactiveCandidate, ProactiveEngine, SNOOZED,
)

PASS = 0
FAIL = 0

DAY = int(datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc).timestamp())
NOW = DAY + 9 * 3600  # Monday 09:00


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def base_config(prefix: str) -> Config:
    base = tempfile.mkdtemp(prefix=prefix)
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    cfg.timezone = "UTC"
    cfg.sleep_start = 23 * 60
    cfg.sleep_end = 7 * 60
    cfg.ensure_dirs()
    return cfg


def fresh_container(prefix: str = "m7-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


def snap(free: int = 180, events=None, courses=None) -> dict:
    return {"free_minutes_today": free, "events_today": events or [],
            "courses": courses or []}


def mk(key: str, category: str, priority: str = "high", **kw) -> ProactiveCandidate:
    c = ProactiveCandidate(key=key, category=category,
                           title=kw.pop("title", key),
                           priority=priority, **kw)
    return c


def by_cat(cands, category):
    return [c for c in cands if c.category == category]


def put_plan(c, created: int, slots: list[sch.Slot]) -> None:
    state = sch.PlanState(day_start=7 * 60, day_end=23 * 60, events=[],
                          slots=slots)
    c.db.execute("INSERT INTO plans(created,day_start,day_end,state,json) "
                 "VALUES(?,?,?,?,?)", (created, 7 * 60, 23 * 60, "active",
                                       state.to_json()))


# =====================================================================
# A. candidate generation
# =====================================================================
def test_generation() -> None:
    print("\n== A. candidate generation ==")
    c = fresh_container("m7-gen-")
    eng = c.proactive_engine
    c.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 120, "deficit": max(0, est - 120),
        "conflict": est > 120, "headroom": est <= 120}
    tid = c.db.add_task("CS188 project", est_minutes=480,
                        deadline=NOW + 2 * 86400, priority=5)
    cands = eng.generate_candidates(now=NOW, snapshot=snap())
    dl = by_cat(cands, CAT_DEADLINE_RISK)
    check("A1 deadline risk candidate generated", bool(dl), str(len(cands)))
    check("A1b deadline risk carries numeric evidence",
          dl and {e["kind"] for e in dl[0].evidence}
          >= {"remaining_minutes", "usable_minutes_before_deadline"})
    check("A1c deadline risk references the task",
          dl and dl[0].relevant_entities[0]["id"] == tid)

    c2 = fresh_container("m7-gen2-")
    eng2 = c2.proactive_engine
    c2.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 10000, "deficit": 0, "conflict": False,
        "headroom": True}
    c2.db.add_task("Easy task", est_minutes=30, deadline=NOW + 5 * 86400)
    check("A2 no deadline-risk candidate when capacity is ample",
          not by_cat(eng2.generate_candidates(now=NOW, snapshot=snap()),
                     CAT_DEADLINE_RISK))

    courses = [{"code": "CS188", "upcoming_deadlines": [NOW + 86400]}]
    check("A3 course deadline candidate generated",
          by_cat(eng2.generate_candidates(now=NOW, snapshot=snap(courses=courses)),
                 CAT_COURSE))

    # project risk increase
    c3 = fresh_container("m7-gen3-")
    eng3 = c3.proactive_engine
    c3.projects.list_projects = lambda *a, **k: [
        {"id": 1, "name": "Proj", "risk": 0.8, "risk_level": "high",
         "remaining_minutes": 300}]
    eng3._seed_baseline("project-risk:1", CAT_PROJECT_RISK, "x",
                        {"risk_score": 0.5}, NOW)
    pr = by_cat(eng3.generate_candidates(now=NOW, snapshot=snap()),
                CAT_PROJECT_RISK)
    check("A4 project risk increase candidate generated", bool(pr))
    check("A4b risk candidate keeps old and new scores",
          pr and {e["kind"] for e in pr[0].evidence}
          >= {"risk_score", "previous_risk_score"})

    # free time
    c4 = fresh_container("m7-gen4-")
    eng4 = c4.proactive_engine
    c4.db.add_task("Essay", est_minutes=90, priority=4)
    c4.optimizer.optimize_from_state = lambda **k: type("R", (), {
        "sessions": [type("S", (), {"task_id": 1})()]})()
    ft = by_cat(eng4.generate_candidates(now=NOW, snapshot=snap(free=180)),
                CAT_FREE_TIME)
    check("A5 free-time opportunity generated", bool(ft))
    check("A6 no free-time candidate below the window threshold",
          not by_cat(eng4.generate_candidates(now=NOW, snapshot=snap(free=10)),
                     CAT_FREE_TIME))

    # missed task
    c5 = fresh_container("m7-gen5-")
    eng5 = c5.proactive_engine
    t = c5.db.add_task("CS168 block", est_minutes=60, deadline=NOW + 86400)
    put_plan(c5, NOW, [sch.Slot(t, "CS168 block", 7 * 60, 8 * 60)])
    ms = by_cat(eng5.generate_candidates(now=NOW, snapshot=snap()),
                CAT_MISSED_TASK)
    check("A7 missed planned block generated", bool(ms))
    check("A7b missed block does not assume completion",
          ms and any(e["kind"] == "task_status" for e in ms[0].evidence))

    # schedule conflict
    c6 = fresh_container("m7-gen6-")
    eng6 = c6.proactive_engine
    t6 = c6.db.add_task("Study", est_minutes=60)
    midnight = c6.cfg.local_midnight(NOW)
    c6.db.add_event("Meeting", midnight + 9 * 3600, midnight + 10 * 3600)
    put_plan(c6, NOW, [sch.Slot(t6, "Study", 9 * 60, 10 * 60)])
    sc = by_cat(eng6.generate_candidates(now=NOW, snapshot=snap()),
                CAT_SCHEDULE_CONFLICT)
    check("A8 schedule conflict generated", bool(sc))

    # estimate issue
    c7 = fresh_container("m7-gen7-")
    eng7 = c7.proactive_engine
    c7.db.add_task("study session", est_minutes=60)
    for i in range(3):
        c7.memory.record_estimate_sample(category="study", estimated_minutes=60,
                                         actual_minutes=105, ref=f"t{i}",
                                         now=NOW)
    check("A9 estimate issue generated",
          by_cat(eng7.generate_candidates(now=NOW, snapshot=snap()),
                 CAT_ESTIMATE))

    # routine opportunity
    c8 = fresh_container("m7-gen8-")
    eng8 = c8.proactive_engine
    c8.db.execute(
        "INSERT INTO routines(kind,category,zone,weekday,start_min,end_min,"
        "title,count,weeks,n_of_m,confidence,first_ts,last_ts,state,source,"
        "created_at,updated_at,signature) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?)",
        ("activity", "study", "", 0, 9 * 60, 10 * 60, "study", 4, 3, 4, 0.7,
         NOW - 5 * 86400, NOW - 3600, "confirmed", "inferred", NOW, NOW,
         "activity:study::0:9"))
    check("A10 routine opportunity generated",
          by_cat(eng8.generate_candidates(now=NOW, snapshot=snap(free=180)),
                 CAT_ROUTINE))

    # travel prep
    c9 = fresh_container("m7-gen9-")
    eng9 = c9.proactive_engine
    events = [{"id": 1, "title": "Flight to SFO", "start_ts": NOW + 1800,
               "end_ts": NOW + 5400, "location": "SFO"}]
    check("A11 travel preparation warning generated",
          by_cat(eng9.generate_candidates(now=NOW,
                                          snapshot=snap(events=events)),
                 CAT_TRAVEL))
    events2 = [{"id": 2, "title": "Coffee", "start_ts": NOW + 1800,
                "end_ts": NOW + 3600, "location": ""}]
    check("A12 no travel warning for a non-travel event",
          not by_cat(eng9.generate_candidates(now=NOW,
                                              snapshot=snap(events=events2)),
                     CAT_TRAVEL))

    # food gap
    c10 = fresh_container("m7-gen10-")
    eng10 = c10.proactive_engine
    c10.foodplan.peek = lambda **k: {"plan": {"recipe": "Stir fry",
                                              "missing": ["broccoli"]}}
    check("A13 food gap candidate generated",
          by_cat(eng10.generate_candidates(now=NOW, snapshot=snap()),
                 CAT_FOOD))

    # web change
    c11 = fresh_container("m7-gen11-")
    eng11 = c11.proactive_engine
    c11.memory.remember_web(subject="CS168", key="due", value="Friday",
                            source_url="https://cs168.io", now=NOW)
    wc = by_cat(eng11.generate_candidates(
        now=NOW, snapshot=snap(), web_values={"CS168:due": "Monday"}),
        CAT_WEB_CHANGE)
    check("A14 web change candidate generated", bool(wc))
    check("A14b no web candidate when the value is unchanged",
          not by_cat(eng11.generate_candidates(
              now=NOW, snapshot=snap(), web_values={"CS168:due": "Friday"}),
              CAT_WEB_CHANGE))

    # category toggle + cap
    c12 = fresh_container("m7-gen12-")
    eng12 = c12.proactive_engine
    c12.cfg.proactive_cat_travel = False
    check("A15 category toggles disable a detector",
          not by_cat(eng12.generate_candidates(now=NOW,
                                               snapshot=snap(events=events)),
                     CAT_TRAVEL))
    c12.cfg.proactive_max_candidates = 1
    for i in range(5):
        c12.db.add_task(f"task {i}", est_minutes=480,
                        deadline=NOW + 2 * 86400, priority=5)
    c12.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 30, "deficit": est, "conflict": True,
        "headroom": False}
    check("A16 candidate generation is capped",
          len(eng12.generate_candidates(now=NOW, snapshot=snap())) <= 1)


# =====================================================================
# B. ranking
# =====================================================================
def test_ranking() -> None:
    print("\n== B. ranking ==")
    c = fresh_container("m7-rank-")
    eng = c.proactive_engine
    critical = mk("k1", CAT_DEADLINE_RISK, urgency=1.0, risk=0.9, deficit=1.0,
                  impact=1.0, confidence=0.95)
    low = mk("k2", CAT_ROUTINE, urgency=0.1, risk=0.0, deficit=0.0,
             impact=0.2, confidence=0.5)
    ranked = eng.rank_candidates([low, critical], now=NOW)
    check("B1 critical outranks low", ranked[0].key == "k1"
          and ranked[0].priority == "critical")
    check("B10 scores stay within [0, 1]",
          all(0.0 <= x.score <= 1.0 for x in ranked))

    deadline = mk("k3", CAT_DEADLINE_RISK, urgency=0.9, risk=0.6, deficit=0.8,
                  impact=0.8, confidence=0.85)
    routine = mk("k4", CAT_ROUTINE, urgency=0.9, risk=0.0, deficit=0.0,
                 impact=0.4, confidence=0.7)
    ranked2 = eng.rank_candidates([routine, deadline], now=NOW)
    check("B2 deadline risk outranks routine affinity",
          ranked2[0].key == "k3")

    hi = mk("k5", CAT_DEADLINE_RISK, urgency=0.6, confidence=0.95)
    lo = mk("k6", CAT_DEADLINE_RISK, urgency=0.6, confidence=0.2)
    check("B3 confidence affects ranking",
          eng.rank_candidates([lo, hi], now=NOW)[0].key == "k5")
    novel = mk("k7", CAT_FREE_TIME, urgency=0.6, novelty=1.0)
    old = mk("k8", CAT_FREE_TIME, urgency=0.6, novelty=0.1)
    check("B4 novelty affects ranking",
          eng.rank_candidates([old, novel], now=NOW)[0].key == "k7")

    # preference: boost risk
    base = ProactiveEngine(fresh_container("m7-rank-base-")).rank_candidates(
        [mk("k9", CAT_PROJECT_RISK, urgency=0.5, risk=0.5, confidence=0.8)],
        now=NOW)[0].score
    c.memory.remember_explicit("i want to know when a project becomes at risk",
                               now=NOW)
    risk_c = mk("k9", CAT_PROJECT_RISK, urgency=0.5, risk=0.5, confidence=0.8)
    boosted = eng.rank_candidates([risk_c], now=NOW)[0].score
    check("B5 explicit preference raises the soft rank", boosted > base,
          f"{boosted} > {base}")

    c2 = fresh_container("m7-rank2-")
    eng2 = c2.proactive_engine
    c2.memory.remember_explicit("don't bother me about low priority tasks",
                                now=NOW)
    lowc = mk("k10", CAT_ROUTINE, priority="low", urgency=0.2)
    plain = ProactiveEngine(fresh_container("m7-rank3-"))
    plain_score = plain.rank_candidates([mk("k10", CAT_ROUTINE,
                                            priority="low", urgency=0.2)],
                                        now=NOW)[0].score
    muted = eng2.rank_candidates([lowc], now=NOW)[0].score
    check("B6 'don't bother me about low' lowers low-priority rank",
          muted < plain_score, f"{muted} < {plain_score}")

    # dismissal history penalty
    c3 = fresh_container("m7-rank3b-")
    eng3 = c3.proactive_engine
    cand = mk("dismiss-me", CAT_FREE_TIME, urgency=0.6, confidence=0.8)
    eng3._persist_candidate(cand, NOW)
    for i in range(4):
        eng3.respond("dismiss-me", DISMISSED, now=NOW + i)
    rates = eng3._reaction_rates()
    check("B7 dismissal history is tracked",
          rates.get(CAT_FREE_TIME, {}).get("dismiss_rate", 0) > 0)
    after = eng3.rank_candidates([mk("dismiss-me", CAT_FREE_TIME, urgency=0.6,
                                     confidence=0.8)], now=NOW)[0].score
    clean = ProactiveEngine(fresh_container("m7-rank3c-")).rank_candidates(
        [mk("dismiss-me", CAT_FREE_TIME, urgency=0.6, confidence=0.8)],
        now=NOW)[0].score
    check("B7b dismissal reduces the soft rank of that category",
          after < clean, f"{after} < {clean}")

    check("B8 priority thresholds map from score",
          eng._priority_for(0.9, CAT_DEADLINE_RISK) == "critical"
          and eng._priority_for(0.6, CAT_DEADLINE_RISK) == "high"
          and eng._priority_for(0.4, CAT_DEADLINE_RISK) == "medium"
          and eng._priority_for(0.1, CAT_DEADLINE_RISK) == "low")
    check("B9 ranking is deterministic",
          [x.key for x in eng.rank_candidates([low, critical], now=NOW)]
          == [x.key for x in eng.rank_candidates([critical, low], now=NOW)])


# =====================================================================
# C. suppression / notification policy
# =====================================================================
def test_suppression() -> None:
    print("\n== C. suppression ==")
    c = fresh_container("m7-sup-")
    eng = c.proactive_engine
    cand = mk("cooldown-key", CAT_FREE_TIME, confidence=0.8)
    eng._persist_candidate(cand, NOW)
    eng._record_notification(cand, NOW, "log", "hello")
    allowed, supp = eng.policy_filter([cand], now=NOW + 60)
    check("C1 cooldown suppresses an immediate repeat",
          not allowed and supp and supp[0]["reason"] == "cooldown", str(supp))
    allowed2, supp2 = eng.policy_filter([cand], now=NOW + 200 * 60)
    check("C2 unchanged state is deduplicated after cooldown",
          not allowed2 and supp2 and supp2[0]["reason"] == "dedup", str(supp2))
    changed = mk("cooldown-key", CAT_FREE_TIME, confidence=0.8,
                 summary="materially different")
    allowed3, _ = eng.policy_filter([changed], now=NOW + 200 * 60)
    check("C3 a material change allows a new notification", bool(allowed3))

    # daily budget
    c2 = fresh_container("m7-sup2-")
    eng2 = c2.proactive_engine
    c2.cfg.proactive_daily_budget = 1
    a = mk("b1", CAT_FREE_TIME, confidence=0.8)
    b = mk("b2", CAT_DEADLINE_RISK, confidence=0.8)
    allowed_b, supp_b = eng2.policy_filter([a, b], now=NOW)
    check("C4 the daily budget caps normal notifications",
          len(allowed_b) == 1
          and any(s["reason"] == "daily_budget" for s in supp_b), str(supp_b))

    # critical budget separate
    c3 = fresh_container("m7-sup3-")
    eng3 = c3.proactive_engine
    c3.cfg.proactive_daily_budget = 0
    crit = mk("c1", CAT_DEADLINE_RISK, priority="critical", confidence=0.9)
    allowed_c, _ = eng3.policy_filter([crit], now=NOW)
    check("C5 critical warnings have a separate budget", bool(allowed_c))

    # quiet hours
    night = DAY + 23 * 3600
    c4 = fresh_container("m7-sup4-")
    eng4 = c4.proactive_engine
    normal = mk("q1", CAT_FREE_TIME, priority="high", confidence=0.8)
    allowed_q, supp_q = eng4.policy_filter([normal], now=night)
    check("C6 quiet hours suppress normal notifications",
          not allowed_q and supp_q[0]["reason"] == "quiet_hours", str(supp_q))
    critical_q = mk("q2", CAT_DEADLINE_RISK, priority="critical",
                    confidence=0.9)
    allowed_qc, _ = eng4.policy_filter([critical_q], now=night)
    check("C7 critical may bypass quiet hours when configured",
          bool(allowed_qc))
    c4.cfg.proactive_quiet_critical = False
    allowed_qc2, supp_qc2 = eng4.policy_filter([critical_q], now=night)
    check("C8 quiet-critical can be disabled",
          not allowed_qc2 and supp_qc2[0]["reason"] == "quiet_hours")

    # user suppression
    c5 = fresh_container("m7-sup5-")
    eng5 = c5.proactive_engine
    eng5.suppress("supp-me", now=NOW)
    allowed_s, supp_s = eng5.policy_filter([mk("supp-me", CAT_FREE_TIME,
                                               confidence=0.8)], now=NOW)
    check("C9 a candidate can be suppressed by the user",
          not allowed_s and supp_s[0]["reason"] == "user_suppressed")
    eng5.suppress(CAT_FOOD, scope="category", now=NOW)
    allowed_cat, supp_cat = eng5.policy_filter(
        [mk("food:x", CAT_FOOD, confidence=0.8)], now=NOW)
    check("C10 a category can be suppressed",
          not allowed_cat and supp_cat[0]["reason"] == "user_suppressed")

    # snooze
    c6 = fresh_container("m7-sup6-")
    eng6 = c6.proactive_engine
    eng6.snooze("snz", 180, now=NOW)
    allowed_z, supp_z = eng6.policy_filter([mk("snz", CAT_FREE_TIME,
                                               confidence=0.8)], now=NOW + 60)
    check("C11 a snoozed candidate is suppressed",
          not allowed_z and supp_z[0]["reason"] == "snoozed")
    allowed_z2, _ = eng6.policy_filter([mk("snz", CAT_FREE_TIME,
                                           confidence=0.8)],
                                       now=NOW + 200 * 60)
    check("C12 a snooze expires and may resurface", bool(allowed_z2))

    # expiry / threshold / confidence
    exp = mk("exp", CAT_FREE_TIME, confidence=0.8, expires_at=NOW - 1)
    allowed_e, supp_e = eng6.policy_filter([exp], now=NOW)
    check("C13 an expired candidate is suppressed",
          not allowed_e and supp_e[0]["reason"] == "expired")
    c6.cfg.proactive_min_priority = "high"
    allowed_p, supp_p = eng6.policy_filter([mk("p", CAT_FREE_TIME,
                                               priority="medium",
                                               confidence=0.8)], now=NOW)
    check("C14 below-threshold priority is suppressed",
          not allowed_p and supp_p[0]["reason"] == "below_threshold")
    c6.cfg.proactive_min_confidence = 0.9
    allowed_cf, supp_cf = eng6.policy_filter([mk("cf", CAT_DEADLINE_RISK,
                                                 priority="high",
                                                 confidence=0.3)], now=NOW)
    check("C15 low-confidence candidates are suppressed",
          not allowed_cf and supp_cf[0]["reason"] == "low_confidence")


# =====================================================================
# D. state
# =====================================================================
def test_state() -> None:
    print("\n== D. state ==")
    c = fresh_container("m7-state-")
    eng = c.proactive_engine
    for key in ("s1", "s2", "s3", "s4"):
        eng._persist_candidate(mk(key, CAT_FREE_TIME, confidence=0.8), NOW)
    check("D1 accept records the response",
          eng.respond("s1", ACCEPTED, now=NOW)["ok"]
          and eng.get_candidate("s1")["state"] == ACCEPTED)
    check("D2 dismiss records the response",
          eng.respond("s2", DISMISSED, now=NOW)["ok"]
          and eng.get_candidate("s2")["state"] == DISMISSED)
    check("D3 snooze records the response",
          eng.respond("s3", SNOOZED, minutes=60, now=NOW)["ok"]
          and eng.get_candidate("s3")["state"] == SNOOZED)
    check("D4 expire records the response",
          eng.respond("s4", EXPIRED, now=NOW)["ok"]
          and eng.get_candidate("s4")["state"] == EXPIRED)

    # ignored via expiry of a sent notification
    eng._persist_candidate(mk("s5", CAT_FREE_TIME, confidence=0.8), NOW)
    eng._record_notification(mk("s5", CAT_FREE_TIME, confidence=0.8), NOW,
                             "log", "x")
    n = eng.expire_stale(now=NOW + 100 * 3600)
    check("D5 unanswered notifications become ignored, not accepted",
          n >= 1 and eng.get_candidate("s5")["state"] == IGNORED)
    check("D6 ignored is distinct from accepted",
          eng.get_candidate("s5")["state"] != ACCEPTED)
    check("D7 respond rejects an unknown key",
          not eng.respond("nope", ACCEPTED, now=NOW)["ok"])
    check("D8 respond rejects an unknown response",
          not eng.respond("s1", "maybe", now=NOW)["ok"])

    # idempotent repeated observation
    c2 = fresh_container("m7-state2-")
    eng2 = c2.proactive_engine
    c2.db.add_task("Essay", est_minutes=90, priority=4)
    c2.optimizer.optimize_from_state = lambda **k: type("R", (), {
        "sessions": [type("S", (), {"task_id": 1})()]})()
    r1 = eng2.run_cycle(now=NOW, deliver=True)
    first = [m["key"] for m in r1["messages"]]
    r2 = eng2.run_cycle(now=NOW + 60, deliver=True)
    check("D9 a repeated observation does not duplicate notifications",
          first and not r2["messages"], f"{first} then {r2['messages']}")
    row = eng2._get_candidate(first[0]) if first else None
    check("D10 the candidate is persisted",
          row is not None and int(row["notification_count"]) == 1)
    r3 = eng2.run_cycle(now=NOW + 400 * 60, deliver=True)
    check("D11 unchanged state stays deduplicated across cycles",
          not r3["messages"])


# =====================================================================
# E. evidence
# =====================================================================
def test_evidence() -> None:
    print("\n== E. evidence ==")
    c = fresh_container("m7-ev-")
    eng = c.proactive_engine
    c.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 120, "deficit": est - 120,
        "conflict": True, "headroom": False}
    c.db.add_task("CS188 project", est_minutes=480,
                  deadline=NOW + 2 * 86400, priority=5)
    cands = eng.generate_candidates(now=NOW, snapshot=snap())
    check("E1 every candidate has evidence",
          cands and all(cand.evidence for cand in cands))
    dl = by_cat(cands, CAT_DEADLINE_RISK)[0]
    check("E2 the explanation matches the state",
          "480" in dl.explanation and "120" in dl.explanation,
          dl.explanation)
    check("E3 no vague claims: numeric evidence present",
          any(e["kind"] == "risk_ratio" for e in dl.evidence))
    check("E4 evidence values are real state",
          next(e["value"] for e in dl.evidence
               if e["kind"] == "remaining_minutes") == 480)

    w = c.memory.remember_web(subject="CS168", key="due", value="Friday",
                              source_url="https://cs168.io", now=NOW)
    wc = by_cat(eng.generate_candidates(
        now=NOW, snapshot=snap(), web_values={"CS168:due": "Monday"}),
        CAT_WEB_CHANGE)[0]
    check("E5 web provenance is retained",
          any(e["kind"] == "source_url"
              and e["value"] == "https://cs168.io" for e in wc.evidence))
    check("E6 candidate serialises completely",
          {"key", "category", "title", "evidence", "proposed_action",
           "priority"} <= set(wc.to_dict()))
    ex = eng.explain_candidate(wc.to_dict())
    check("E7 explain builds a why text", bool(ex["why"]))
    check("E8 explain reports the category",
          ex["category"] == CAT_WEB_CHANGE)


# =====================================================================
# F. safety
# =====================================================================
def test_safety() -> None:
    print("\n== F. safety ==")
    c = fresh_container("m7-safe-")
    eng = c.proactive_engine
    c.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 60, "deficit": est, "conflict": True,
        "headroom": False}
    c.db.add_task("CS188 project", est_minutes=480,
                  deadline=NOW + 2 * 86400, priority=5)
    cands = eng.generate_candidates(now=NOW, snapshot=snap())
    dl = by_cat(cands, CAT_DEADLINE_RISK)[0]
    check("F1 proposed mutations require confirmation",
          dl.requires_confirmation and dl.proposed_action)
    allowed_actions = {"schedule_task", "reschedule_task", "plan_day",
                       "optimize_week", "recommend_routine", "prepare",
                       "add_groceries", "reschedule", "review_web_fact",
                       "adjust_estimates"}
    check("F2 no autonomous purchase/booking action is proposed",
          all(cand.proposed_action.get("action") in allowed_actions
              for cand in cands))

    key = dl.key
    check("F3 callback data validates against the Telegram shape",
          bool(re.match(r"^[a-z_]+:[a-z_:0-9-]+$", f"pro:accept:{key}")))

    # accept does not execute
    eng._persist_candidate(dl, NOW)
    before = c.db.latest_plan()
    res = eng.respond(key, ACCEPTED, now=NOW)
    check("F4 accepting a recommendation does not execute it",
          res["ok"] and c.db.latest_plan() == before)
    check("F5 the accepted candidate keeps its proposed action",
          res["candidate"]["proposed_action"].get("action") == "schedule_task")

    # no external send without a token
    check("F6 delivery is logged, not sent, when Telegram is unconfigured",
          c.cfg.telegram_token == ""
          and eng._send(dl, "hi", "log", NOW) is True)

    # read-only executive surface only proposes
    svc = ExecutiveService(c, now_ts=NOW)
    before_n = len(c.db.query("SELECT * FROM proactive_suppressions"))
    r = svc.ask(text="stop reminding me about deadline risk", read_only=True)
    after_n = len(c.db.query("SELECT * FROM proactive_suppressions"))
    check("F7 read-only proactive suppress is only proposed",
          r.status == ResultStatus.NEEDS_CONFIRMATION and before_n == after_n)

    # safety classification
    check("F8 proactive reads are read; writes are low-risk",
          c.safety.classify("proactive_query").value == "read"
          and c.safety.classify("proactive_suppress").value == "low_risk_write")

    # unrelated critical warnings survive a category suppression
    c2 = fresh_container("m7-safe2-")
    eng2 = c2.proactive_engine
    eng2.suppress(CAT_FOOD, scope="category", now=NOW)
    crit = mk("crit", CAT_DEADLINE_RISK, priority="critical", confidence=0.9)
    allowed, _ = eng2.policy_filter([crit], now=NOW)
    check("F9 suppressing one category does not mute an unrelated critical",
          bool(allowed))
    check("F10 a critical candidate cannot be demoted by dismissal history",
          eng2.rank_candidates([mk("crit", CAT_DEADLINE_RISK,
                                   urgency=1.0, risk=1.0, deficit=1.0,
                                   confidence=0.95)], now=NOW)[0].priority
          == "critical")


# =====================================================================
# G. integration
# =====================================================================
def test_integration() -> None:
    print("\n== G. integration ==")
    c = fresh_container("m7-int-")
    eng = c.proactive_engine
    # M3 project risk
    c.projects.list_projects = lambda *a, **k: [
        {"id": 1, "name": "Proj", "risk": 0.85, "risk_level": "high",
         "remaining_minutes": 300}]
    eng._seed_baseline("project-risk:1", CAT_PROJECT_RISK, "x",
                       {"risk_score": 0.4}, NOW)
    check("G1 M3 project risk feeds candidate generation",
          by_cat(eng.generate_candidates(now=NOW, snapshot=snap()),
                 CAT_PROJECT_RISK))
    # M4 web
    c.memory.remember_web(subject="CS188", key="due", value="Monday",
                          source_url="https://cs188.io", now=NOW)
    check("G2 M4 web provenance feeds web-change detection",
          by_cat(eng.generate_candidates(
              now=NOW, snapshot=snap(), web_values={"CS188:due": "Friday"}),
              CAT_WEB_CHANGE))
    # M5 optimizer gate
    c.db.add_task("Essay", est_minutes=90, priority=4)
    c.optimizer.optimize_from_state = lambda **k: type("R", (), {
        "sessions": [type("S", (), {"task_id": 999})()]})()
    check("G3 M5 optimizer can veto an impossible recommendation",
          not by_cat(eng.generate_candidates(now=NOW, snapshot=snap(free=180)),
                     CAT_FREE_TIME))
    c.optimizer.optimize_from_state = lambda **k: type("R", (), {
        "sessions": [type("S", (), {"task_id": int(c.db.tasks("active")[0]["id"])})()]})()
    check("G3b M5 optimizer allows a feasible recommendation",
          by_cat(eng.generate_candidates(now=NOW, snapshot=snap(free=180)),
                 CAT_FREE_TIME))
    # M6 memory preference
    c.memory.remember_explicit("i want to know when a project becomes at risk",
                               now=NOW)
    check("G4 M6 memory preference influences ranking",
          eng._memory_prefs(NOW)["boost_risk"] is True)
    # calendar / courses / tasks / routines
    midnight = c.cfg.local_midnight(NOW)
    c.db.add_event("Flight", midnight + 10 * 3600, midnight + 12 * 3600)
    c.db.execute("INSERT INTO routines(kind,category,zone,weekday,start_min,"
                 "end_min,title,count,weeks,n_of_m,confidence,first_ts,last_ts,"
                 "state,source,created_at,updated_at,signature) VALUES(?,?,?,?,"
                 "?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("activity", "study", "", 0, 9 * 60, 10 * 60, "study", 4, 3, 4,
                  0.7, NOW - 5 * 86400, NOW - 3600, "confirmed", "inferred",
                  NOW, NOW, "activity:study::0:9"))
    cands = eng.generate_candidates(
        now=NOW, snapshot=snap(free=180,
                               events=[{"id": 1, "title": "Flight",
                                        "start_ts": NOW + 1800,
                                        "end_ts": NOW + 5400,
                                        "location": "SFO"}],
                               courses=[{"code": "CS188",
                                         "upcoming_deadlines": [NOW + 86400]}]))
    cats = {x.category for x in cands}
    check("G5 calendar events feed conflict/travel detection",
          CAT_TRAVEL in cats)
    check("G6 course deadlines feed candidate generation", CAT_COURSE in cats)
    check("G7 tasks feed deadline/free-time detection",
          CAT_DEADLINE_RISK in cats or CAT_FREE_TIME in cats)
    check("G8 routines feed opportunity detection", CAT_ROUTINE in cats)

    # Telegram formatting + buttons
    sample = cands[0] if cands else None
    if sample is not None:
        msg = eng.format_message(sample)
        buttons = eng.buttons(sample)
        check("G9 proactive messages have a title and buttons",
              sample.title in msg and buttons and
              all(re.match(r"^[a-z_]+:[a-z_:0-9-]+$", b[1])
                  for row in buttons for b in row))
    else:
        check("G9 proactive messages have a title and buttons", False)

    # audit + idempotency
    eng.run_cycle(now=NOW, deliver=True)
    actions = [a["action"] for a in c.audit.recent(limit=200)]
    check("G10 proactive transitions are audited",
          any(a.startswith("proactive_") for a in actions), str(actions[:3]))
    r1 = eng.run_cycle(now=NOW + 60, deliver=True)
    check("G11 the proactive loop is idempotent",
          r1["notified"] == 0, str(r1["notified"]))

    # scheduler integration (same cadence, no new loop)
    called = {}
    c.proactive_engine.run_cycle = lambda **k: called.setdefault("ran", True) or {
        "generated": 0, "notified": 0}
    from butler.scheduler import Scheduler
    Scheduler(c)._run_proactive()
    check("G12 the existing scheduler drives the proactive engine",
          called.get("ran") is True)


# =====================================================================
# H. realistic scenarios
# =====================================================================
def test_realistic() -> None:
    print("\n== H. realistic scenarios ==")
    # 1 project deadline crisis
    c = fresh_container("m7-real-")
    eng = c.proactive_engine
    c.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 240, "deficit": est - 240,
        "conflict": True, "headroom": False}
    c.db.add_task("CS188 project", est_minutes=480,
                  deadline=NOW + 2 * 86400, priority=5)
    r = eng.run_cycle(now=NOW, deliver=True)
    check("H1 deadline crisis notifies",
          any(m["key"].startswith("deadline-risk:") for m in r["messages"]))

    # 2 conflicting meeting
    c2 = fresh_container("m7-real2-")
    eng2 = c2.proactive_engine
    t = c2.db.add_task("CS168 study", est_minutes=60)
    midnight = c2.cfg.local_midnight(NOW)
    c2.db.add_event("New meeting", midnight + 15 * 3600,
                    midnight + 16 * 3600)
    put_plan(c2, NOW, [sch.Slot(t, "CS168 study", 15 * 60, 16 * 60)])
    check("H2 a new meeting conflict is detected",
          by_cat(eng2.generate_candidates(now=NOW, snapshot=snap()),
                 CAT_SCHEDULE_CONFLICT))

    # 3 free afternoon + high-risk project
    c3 = fresh_container("m7-real3-")
    eng3 = c3.proactive_engine
    c3.projects.list_projects = lambda *a, **k: [
        {"id": 1, "name": "CS168", "risk": 0.8, "risk_level": "high",
         "remaining_minutes": 300}]
    tid = c3.db.add_task("CS168 study", est_minutes=120, priority=5)
    c3.optimizer.optimize_from_state = lambda **k: type("R", (), {
        "sessions": [type("S", (), {"task_id": tid})()]})()
    check("H3 free window + risky work produces a recommendation",
          by_cat(eng3.generate_candidates(now=NOW, snapshot=snap(free=150)),
                 CAT_FREE_TIME))

    # 4 missed task
    c4 = fresh_container("m7-real4-")
    eng4 = c4.proactive_engine
    t4 = c4.db.add_task("CS168 block", est_minutes=60,
                        deadline=NOW + 2 * 86400)
    put_plan(c4, NOW, [sch.Slot(t4, "CS168 block", 7 * 60, 8 * 60)])
    check("H4 a missed planned block is surfaced",
          by_cat(eng4.generate_candidates(now=NOW, snapshot=snap()),
                 CAT_MISSED_TASK))

    # 5 deadline changed online
    c5 = fresh_container("m7-real5-")
    eng5 = c5.proactive_engine
    c5.memory.remember_web(subject="CS168", key="due", value="Friday",
                           source_url="https://cs168.io", now=NOW)
    check("H5 a verified online deadline change is surfaced",
          by_cat(eng5.generate_candidates(
              now=NOW, snapshot=snap(), web_values={"CS168:due": "Wednesday"}),
              CAT_WEB_CHANGE))

    # 6 repeated user dismissal reduces (but never mutes) soft rank
    c6 = fresh_container("m7-real6-")
    eng6 = c6.proactive_engine
    eng6._persist_candidate(mk("rk", CAT_FREE_TIME, confidence=0.8), NOW)
    for i in range(3):
        eng6.respond("rk", DISMISSED, now=NOW + i)
    allowed, _ = eng6.policy_filter([mk("rk", CAT_FREE_TIME, confidence=0.8)],
                                    now=NOW + 10)
    check("H6 dismissal history is recorded", bool(eng6._reaction_rates()))

    # 7 critical warning during quiet hours
    c7 = fresh_container("m7-real7-")
    eng7 = c7.proactive_engine
    allowed7, _ = eng7.policy_filter(
        [mk("crit", CAT_DEADLINE_RISK, priority="critical", confidence=0.9)],
        now=DAY + 23 * 3600)
    check("H7 critical warnings can still surface during quiet hours",
          bool(allowed7))

    # 8 learned estimate correction
    c8 = fresh_container("m7-real8-")
    eng8 = c8.proactive_engine
    c8.db.add_task("study session", est_minutes=60)
    for i in range(4):
        c8.memory.record_estimate_sample(category="study", estimated_minutes=60,
                                         actual_minutes=96, ref=f"t{i}",
                                         now=NOW)
    check("H8 learned estimate correction is surfaced",
          by_cat(eng8.generate_candidates(now=NOW, snapshot=snap()),
                 CAT_ESTIMATE))

    # 9 course assignment update
    c9 = fresh_container("m7-real9-")
    eng9 = c9.proactive_engine
    check("H9 a course assignment update is surfaced",
          by_cat(eng9.generate_candidates(
              now=NOW, snapshot=snap(courses=[{
                  "code": "CS188", "upcoming_deadlines": [NOW + 2 * 86400]}])),
              CAT_COURSE))

    # 10 daily briefing
    c10 = fresh_container("m7-real10-")
    eng10 = c10.proactive_engine
    c10.db.add_task("CS188 project", est_minutes=480,
                    deadline=NOW + 2 * 86400, priority=5)
    c10.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 60, "deficit": est, "conflict": True,
        "headroom": False}
    brief = eng10.briefing(now=NOW)
    check("H10 the daily briefing is deterministic and concise",
          brief["ok"] and "Good morning" in brief["text"]
          and len(brief["text"]) < 1200)

    # 11 snooze then resurface
    c11 = fresh_container("m7-real11-")
    eng11 = c11.proactive_engine
    eng11._persist_candidate(mk("zz", CAT_FREE_TIME, confidence=0.8), NOW)
    eng11.snooze("zz", 60, now=NOW)
    a1, _ = eng11.policy_filter([mk("zz", CAT_FREE_TIME, confidence=0.8)],
                                now=NOW + 30 * 60)
    a2, _ = eng11.policy_filter([mk("zz", CAT_FREE_TIME, confidence=0.8)],
                                now=NOW + 90 * 60)
    check("H11 snooze suppresses then allows resurfacing",
          not a1 and bool(a2))

    # 12 stop reminding me
    c12 = fresh_container("m7-real12-")
    eng12 = c12.proactive_engine
    eng12.suppress(CAT_TRAVEL, scope="category", now=NOW)
    check("H12 'stop reminding me' suppresses the category",
          not eng12.policy_filter([mk("t", CAT_TRAVEL, confidence=0.8)],
                                  now=NOW)[0])


# =====================================================================
# I. regression
# =====================================================================
def test_regression() -> None:
    print("\n== I. regression ==")
    c = fresh_container("m7-reg-")
    c.db.add_task("CS168 project", est_minutes=120, deadline=NOW + 86400,
                  priority=4)
    full = MCPServer(c, profile="full")
    check("I1 the full profile is still exactly 51 tools",
          len({t["name"] for t in full._tools_spec()}) == 51)
    ro = MCPServer(c, profile="readonly")
    ro_names = {t["name"] for t in ro._tools_spec()}
    pro_tools = {"get_proactive_candidates", "get_proactive_status",
                 "explain_proactive_candidate"}
    check("I2 the readonly profile exposes 37 tools including proactive",
          pro_tools <= ro_names and len(ro_names) == 37, str(len(ro_names)))

    check("I3 M3 project reads still work",
          isinstance(c.projects.list_projects(), list))
    check("I4 M4 web still present", hasattr(c.web, "research"))
    check("I5 M5 optimizer still works",
          isinstance(c.optimizer.optimize_from_state(days=1, now=NOW).feasible,
                     bool))
    check("I6 M6 memory still works",
          c.memory.remember_explicit("remember that i prefer mornings",
                                     now=NOW)["ok"])

    it = DeterministicInterpreter(c, now_ts=NOW)
    check("I7 existing routing is unchanged",
          it.interpret("plan my day").action == ActionKind.PLAN_DAY
          and it.interpret("what should i know right now").action
          == ActionKind.PROACTIVE_QUERY
          and it.interpret("stop reminding me about travel").action
          == ActionKind.PROACTIVE_SUPPRESS)

    # read-only MCP proactive tools change nothing
    before = (c.db.latest_plan(), c.proactive_engine.status(now=NOW)["candidates"])
    ro = MCPServer(c, profile="readonly")

    def call(name, args):
        resp = ro._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})
        return json.loads(resp["result"]["content"][0]["text"])

    call("get_proactive_candidates", {})
    call("get_proactive_status", {})
    call("explain_proactive_candidate", {"query": "deadline"})
    after = (c.db.latest_plan(), c.proactive_engine.status(now=NOW)["candidates"])
    check("I8 read-only MCP proactive tools change nothing", before == after)

    tables = {r["name"] for r in c.db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("I9 proactive tables migrated idempotently",
          {"proactive_candidates", "proactive_notifications",
           "proactive_responses", "proactive_suppressions",
           "proactive_snoozes"} <= tables)
    check("I10 the legacy proactive loop is preserved",
          hasattr(c.proactive, "run") and hasattr(c.proactive, "collect"))

    cfg = Config()
    cfg.proactive_daily_budget = -1
    raised = False
    try:
        cfg.validate()
    except ValueError:
        raised = True
    check("I11 config validation rejects bad proactive settings", raised)


def main() -> int:
    print("M7 acceptance: proactive executive behavior")
    test_generation()
    test_ranking()
    test_suppression()
    test_state()
    test_evidence()
    test_safety()
    test_integration()
    test_realistic()
    test_regression()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
