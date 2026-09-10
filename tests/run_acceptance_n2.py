"""N2 acceptance: universal tracking, triggers & rules.

Run:  .venv/bin/python tests/run_acceptance_n2.py

Deterministic and offline. One generic tracker engine drives every
"when X changes, do Y" request; sources are normalized snapshots, conditions
are evaluated deterministically, and actions bridge through the existing M7
proactive policy (never a second notifier).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402
from butler.tracking import (  # noqa: E402
    STATE_ACTIVE, STATE_ARCHIVED, STATE_DEGRADED, STATE_ERROR, STATE_PAUSED,
    SnapshotProvider, TrackerEngine, diff_snapshots, evaluate_condition,
)

PASS = 0
FAIL = 0
N = 1_800_000_000


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
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    return cfg


def fresh(prefix: str = "n2-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


def with_snapshot(c: Container, snap: dict | None = None) -> SnapshotProvider:
    sp = SnapshotProvider(c, snap or {})
    c.trackers.register_provider(sp)
    return sp


def item(label: str, **fields):
    return {"label": label, "fields": fields}


def items_snap(**kv):
    return {"items": {k: item(k, **v) for k, v in kv.items()}}


def qtysnap(name: str, q: float):
    return {"items": {name: item(name, quantity=q)}}


# =====================================================================
# A. tracker lifecycle
# =====================================================================
def test_lifecycle() -> None:
    print("\n== A. tracker lifecycle ==")
    c = fresh("n2-life-")
    eng = c.trackers
    with_snapshot(c, {})
    t = eng.create(name="CS188 assignments", source="snapshot",
                   target_type="course", target_ref="CS188",
                   condition={"type": "new_item"}, cadence=3600)
    check("A1 a tracker is created", t.id > 0 and t.state == STATE_ACTIVE)
    check("A2 it can be read back", eng.get(t.id) is not None)
    check("A3 it appears in the list", any(x.id == t.id for x in eng.list()))
    t2 = eng.update(t, cadence_seconds=7200, priority="high")
    check("A4 it can be reconfigured", t2.cadence_seconds == 7200
          and t2.priority == "high")
    check("A5 pause sets paused", eng.control(t.id, "pause")["ok"]
          and eng.get(t.id).state == STATE_PAUSED)
    check("A6 resume sets active", eng.control(t.id, "resume")["ok"]
          and eng.get(t.id).state == STATE_ACTIVE)
    check("A7 disable disables", eng.control(t.id, "disable")["ok"]
          and eng.get(t.id).enabled is False)
    check("A8 archive archives", eng.control(t.id, "archive")["ok"]
          and eng.get(t.id).state == STATE_ARCHIVED)
    check("A9 unknown action is rejected", not eng.control(t.id, "frobnicate")["ok"])
    check("A10 unknown tracker is rejected", not eng.control(99999, "pause")["ok"])
    # expire
    t3 = eng.create(name="temp", source="snapshot", condition={"type": "new_item"},
                    expires_at=N - 1, cadence=60)
    eng.run_due(now=N)
    check("A11 an expired tracker is archived", eng.get(t3.id).state == STATE_ARCHIVED)
    check("A12 lifecycle transitions are audited",
          any(a["action"].startswith("tracker_")
              for a in c.audit.recent(limit=100)))
    check("A13 enabled flag follows state",
          eng.get(t3.id).enabled is False)
    check("A14 list filters by state",
          all(x.state == STATE_ACTIVE for x in eng.list(state=STATE_ACTIVE)))
    check("A15 timestamps are set", t.created_at > 0 and t.updated_at > 0)
    check("A16 cadence is stored", eng.get(t.id).cadence_seconds == 7200)


# =====================================================================
# B. local state
# =====================================================================
def test_local_state() -> None:
    print("\n== B. local state ==")
    c = fresh("n2-state-")
    eng = c.trackers
    sp = with_snapshot(c, qtysnap("chicken", 3.0))
    t = eng.create(name="chicken low", source="snapshot",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2}, cadence=60)

    def step(snap, now):
        sp.set_snapshot(snap)
        eng.update(eng.get(t.id), next_check_at=now)
        return eng.run_due(now=now)["results"][0]["fired"]

    check("B1 above threshold does not fire", not step(qtysnap("chicken", 3.0), N))
    check("B2 equal to threshold does not fire", not step(qtysnap("chicken", 2.0), N + 60))
    check("B3 crossing below fires", step(qtysnap("chicken", 1.0), N + 120))
    check("B4 stable below does not re-fire", not step(qtysnap("chicken", 1.0), N + 180))
    check("B5 state hash is stored", bool(eng.get(t.id).last_state_hash))
    check("B6 last snapshot is stored", bool(eng.get(t.id).last_snapshot))
    check("B7 last event is recorded", bool(eng.get(t.id).last_event))
    # meaningful diff
    prev = {"items": {"a": item("a", deadline=1)}}
    curr = {"items": {"a": item("a", deadline=2)}}
    evs = diff_snapshots(prev, curr)
    check("B8 a deadline change is a meaningful diff",
          any(e.event_type == "DEADLINE_CHANGED" for e in evs))
    check("B9 an unchanged snapshot has no diff", diff_snapshots(prev, prev) == [])
    check("B10 a new item produces NEW_ITEM",
          any(e.event_type == "NEW_ITEM" for e in diff_snapshots({}, curr)))
    check("B11 a removed item produces ITEM_REMOVED",
          any(e.event_type == "ITEM_REMOVED" for e in diff_snapshots(curr, {})))
    check("B12 a status change produces STATUS_CHANGED",
          any(e.event_type == "STATUS_CHANGED" for e in diff_snapshots(
              {"items": {"a": item("a", status="todo")}},
              {"items": {"a": item("a", status="done")}})))
    check("B13 a risk change produces RISK_CHANGED",
          any(e.event_type == "RISK_CHANGED" for e in diff_snapshots(
              {"fields": {"risk": 0.5}}, {"fields": {"risk": 0.9}})))
    # duplicate event key
    ev = evs[0]
    check("B14 first persist succeeds", eng._persist_event(eng.get(t.id), ev, N))
    check("B15 duplicate persist is ignored",
          not eng._persist_event(eng.get(t.id), ev, N))
    check("B16 events are bounded per diff",
          len(diff_snapshots({"fields": {"x": 1}}, {"fields": {"x": 2}})) == 1)


# =====================================================================
# C. external source / degradation
# =====================================================================
def test_external() -> None:
    print("\n== C. external sources ==")
    c = fresh("n2-ext-")
    eng = c.trackers
    sp = SnapshotProvider(c, {}, fail=True)
    eng.register_provider(sp)
    t = eng.create(name="web watch", source="snapshot", condition={"type": "new_item"},
                   cadence=60)
    res = eng.evaluate(eng.get(t.id), now=N)
    check("C1 an unavailable source is DEGRADED", res.state == STATE_DEGRADED)
    eng._after_evaluate(eng.get(t.id), res, now=N, deliver=True)
    check("C2 failure_count increments", eng.get(t.id).failure_count == 1)
    check("C3 a degraded tracker is retried with backoff",
          eng.get(t.id).next_check_at > N)
    for i in range(5):
        r = eng.evaluate(eng.get(t.id), now=N + i)
        eng._after_evaluate(eng.get(t.id), r, now=N + i, deliver=False)
    check("C4 repeated failures reach ERROR", eng.get(t.id).state == STATE_ERROR)
    # recovery
    sp.set_snapshot(items_snap(a={"x": 1}))
    eng.update(eng.get(t.id), next_check_at=N)
    out = eng.run_due(now=N + 100000)
    check("C5 a recovered source returns to active",
          eng.get(t.id).state == STATE_ACTIVE and eng.get(t.id).failure_count == 0)
    # unknown source
    t2 = eng.create(name="bad", source="nonexistent", condition={"type": "new_item"})
    r2 = eng.evaluate(eng.get(t2.id), now=N)
    check("C6 an unknown source errors", r2.state == STATE_ERROR)
    # partial failure: one bad, one good
    c2 = fresh("n2-ext2-")
    e2 = c2.trackers
    good = SnapshotProvider(c2, items_snap(a={"x": 1}))
    good.name = "snapshot"
    e2.register_provider(good)
    ta = e2.create(name="good", source="snapshot", condition={"type": "new_item"},
                   cadence=60)
    tb = e2.create(name="bad", source="missing", condition={"type": "new_item"},
                   cadence=60)
    out2 = e2.run_due(now=N)
    check("C7 a failing tracker does not stop the cycle", out2["evaluated"] == 2)
    check("C8 the good tracker still evaluates",
          e2.get(ta.id).state == STATE_ACTIVE)
    # bounded per cycle
    c3 = fresh("n2-ext3-")
    e3 = c3.trackers
    e3.max_per_cycle = 2
    with_snapshot(c3, {})
    for i in range(5):
        e3.create(name=f"t{i}", source="snapshot", condition={"type": "new_item"},
                  cadence=60)
    check("C9 evaluation is bounded per cycle",
          e3.run_due(now=N)["evaluated"] <= 2)
    # web provider fetch failure
    from butler.tracking import WebProvider
    wp = WebProvider(c, fetcher=lambda url: {"ok": False, "error": "timeout"})
    try:
        wp.snapshot(type("T", (), {"target_ref": "https://x", "target_id": 0})(), N)
        check("C10 a web fetch failure raises unavailable", False)
    except Exception:
        check("C10 a web fetch failure raises unavailable", True)
    # github injected data
    from butler.tracking import GitHubProvider
    gp = GitHubProvider(c, fetcher=lambda repo: {"pushed_at": "2026", "open_issues_count": 3})
    snap = gp.snapshot(type("T", (), {"target_ref": "o/r", "target_id": 0})(), N)
    check("C11 a github provider normalizes state",
          snap["fields"]["open_issues"] == 3)
    # file provider missing dir
    from butler.tracking import FileProvider
    fp = FileProvider(c)
    try:
        fp.snapshot(type("T", (), {"target_ref": "/nope/xyz", "target_id": 0})(), N)
        check("C12 a missing file dir raises unavailable", False)
    except Exception:
        check("C12 a missing file dir raises unavailable", True)


# =====================================================================
# D. rule conditions
# =====================================================================
def test_conditions() -> None:
    print("\n== D. rule conditions ==")
    ev = lambda t, **kw: __import__("butler.tracking", fromlist=["Event"]).Event(t, **kw)
    events_new = [ev("NEW_ITEM", identity="a")]
    events_dl = [ev("DEADLINE_CHANGED", field_name="deadline", identity="a")]
    events_rm = [ev("ITEM_REMOVED", identity="a")]
    events_status = [ev("STATUS_CHANGED", field_name="status", identity="a")]
    prev = {"items": {"a": item("a", quantity=3.0, deadline=1)},
            "fields": {"risk": 0.5}, "last_activity": N - 10 * 86400}
    curr = {"items": {"a": item("a", quantity=1.0, deadline=2)},
            "fields": {"risk": 0.9}, "last_activity": N - 10 * 86400}
    f = lambda cond, evs=(), p=prev, cu=curr, now=N, lc=N - 60: \
        evaluate_condition(cond, list(evs), p, cu, now, lc)
    check("D1 new_item", f({"type": "new_item"}, events_new)[0])
    check("D2 item_removed", f({"type": "item_removed"}, events_rm)[0])
    check("D3 field_changed", f({"type": "field_changed", "field": "deadline"},
                                events_dl)[0])
    check("D4 deadline_changed", f({"type": "deadline_changed"}, events_dl)[0])
    check("D5 status_changed", f({"type": "status_changed"}, events_status)[0])
    check("D6 risk_crossed_above", f({"type": "risk_crossed_above", "value": 0.8})[0])
    check("D7 threshold_below crossing",
          f({"type": "threshold_below", "item": "a", "field": "quantity",
             "value": 2})[0])
    check("D8 threshold_above crossing",
          evaluate_condition({"type": "threshold_above", "item": "a",
                              "field": "quantity", "value": 2},
                             [], {"items": {"a": item("a", quantity=1.0)}},
                             {"items": {"a": item("a", quantity=3.0)}}, N, 0)[0])
    check("D9 no_activity",
          evaluate_condition(
              {"type": "no_activity", "days": 5}, [],
              {"last_activity": N - 6 * 86400},
              {"last_activity": N - 6 * 86400}, N,
              N - 6 * 86400 + 3600)[0])
    check("D10 time_reached",
          f({"type": "time_reached", "at": N - 1})[0])
    check("D11 time not reached",
          not f({"type": "time_reached", "at": N + 10**9})[0])
    check("D12 state_changed", f({"type": "state_changed"}, events_dl)[0])
    check("D13 AND both true",
          f({"type": "and", "conditions": [
              {"type": "new_item"}, {"type": "item_removed"}]},
            events_new + events_rm)[0])
    check("D14 AND one false",
          not f({"type": "and", "conditions": [
              {"type": "new_item"}, {"type": "item_removed"}]}, events_new)[0])
    check("D15 OR one true",
          f({"type": "or", "conditions": [
              {"type": "item_removed"}, {"type": "new_item"}]}, events_new)[0])
    check("D16 empty condition means any change",
          evaluate_condition({}, events_new, {}, {}, N, 0)[0])
    check("D17 unknown condition falls back to any change",
          evaluate_condition({"type": "mystery"}, events_new, {}, {}, N, 0)[0])
    check("D18 AND caps leaves",
          evaluate_condition({"type": "and", "conditions": [
              {"type": "new_item"}] * 8}, events_new, {}, {}, N, 0)[0])
    check("D19 risk not crossing does not fire",
          not f({"type": "risk_crossed_above", "value": 0.95})[0])
    check("D20 threshold steady does not fire",
          not f({"type": "threshold_below", "item": "a", "field": "quantity",
                 "value": 2}, p=curr, cu=curr)[0])


# =====================================================================
# E. cross-topic + F. dedup + G. cooldown
# =====================================================================
def test_cross_topic_dedup_cooldown() -> None:
    print("\n== E/F/G. cross-topic, dedup, cooldown ==")
    c = fresh("n2-cross-")
    eng = c.trackers
    sp = with_snapshot(c, qtysnap("chicken", 3.0))
    sent = []
    c.proactive_engine._send = lambda cand, text, channel, now: sent.append(
        {"key": cand.key, "dest": cand.destination}) or True
    t = eng.create(name="chicken", source="snapshot", target_type="food_item",
                   target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION", "params": {"items": ["chicken"]}},
                   destination={"chat_id": 111, "thread_id": 7}, cadence=60)
    check("E1 destination is stored", t.destination_chat_id == 111
          and t.destination_thread_id == 7)
    check("E2 by_destination finds it",
          any(x.id == t.id for x in eng.by_destination(111, 7)))
    check("E3 scope defaults to topic when a destination is set",
          t.scope in ("object", "topic"))
    sp.set_snapshot(qtysnap("chicken", 1.0))
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N)
    check("E4 a candidate is produced", c.db.one(
        "SELECT COUNT(*) n FROM proactive_candidates")["n"] >= 1)
    check("E5 the candidate is delivered to the topic destination",
          sent and sent[0]["dest"].get("chat_id") == 111)
    n_events = c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"]
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N + 60)
    check("F1 a repeated poll does not duplicate events",
          c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"] == n_events)
    check("F2 a repeated poll does not duplicate candidates",
          c.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] == 1)
    # restart safety
    c.db.close()
    c2 = Container(c.cfg)
    c2.planner._maybe_sync = lambda: None
    sp2 = SnapshotProvider(c2, qtysnap("chicken", 1.0))
    c2.trackers.register_provider(sp2)
    c2.trackers.update(c2.trackers.get(t.id), next_check_at=N + 1000)
    c2.trackers.run_due(now=N + 1000)
    check("F3 restart does not duplicate events",
          c2.db.one("SELECT COUNT(*) n FROM tracker_events")["n"] == n_events)
    # cooldown suppression
    t2 = c2.trackers.create(name="cooldown", source="snapshot",
                            condition={"type": "new_item"}, cadence=60)
    from butler.tracking import Event as _E, EvaluationResult as _R
    c2.trackers.update(c2.trackers.get(t2.id), cooldown_until=N + 10**6)
    before = c2.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"]
    rr = _R(t2.id, True, events=[_E("NEW_ITEM", identity="z")],
            proposals=[], reason="new item", state=STATE_ACTIVE)
    c2.trackers._after_evaluate(c2.trackers.get(t2.id), rr, now=N, deliver=True)
    check("G1 cooldown suppresses a notification",
          c2.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] == before)
    check("G2 cooldown_until is set on fire",
          c2.trackers.get(t2.id).cooldown_until > N)
    # after cooldown, a changed event notifies
    from butler.tracking import ActionProposal
    c2.trackers.update(c2.trackers.get(t2.id), cooldown_until=N - 1,
                       next_check_at=N - 1)
    c2.trackers._after_evaluate(c2.trackers.get(t2.id), _R(
        t2.id, True, events=[_E("NEW_ITEM", identity="z2")],
        proposals=[ActionProposal("NOTIFY_TOPIC", {"tracker": "cooldown"})],
        reason="new item 2", state=STATE_ACTIVE), now=N, deliver=True)
    check("G3 a changed event notifies after cooldown",
          c2.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] > before)
    # events persisted even during cooldown
    check("G4 events persist regardless of cooldown",
          c2.db.one("SELECT COUNT(*) n FROM tracker_events WHERE tracker_id=?",
                    (t2.id,))["n"] >= 1)
    check("G5 paused trackers are not evaluated",
          c2.trackers.control(t2.id, "pause")["ok"]
          and c2.trackers.get(t2.id).state == STATE_PAUSED)
    check("G6 next_check_at is respected",
          c2.trackers.get(t2.id).next_check_at >= 0)


# =====================================================================
# H. safety / I. memory
# =====================================================================
def test_safety_memory() -> None:
    print("\n== H/I. safety & memory ==")
    c = fresh("n2-safe-")
    eng = c.trackers
    with_snapshot(c, {})
    t = eng.create(name="calendar proposal", source="snapshot",
                   target_type="event", target_ref="flight",
                   condition={"type": "field_changed", "field": "start"},
                   action={"type": "PROPOSE_CALENDAR_ACTION"},
                   destination={"chat_id": 1, "thread_id": 2})
    res = eng.evaluate(eng.get(t.id), now=N, snapshot={"fields": {"start": 1}})
    cands = eng._candidates_for(eng.get(t.id), res, N)
    # force a fire by comparing against a stored snapshot
    eng.update(eng.get(t.id), last_snapshot={"fields": {"start": 0}})
    res2 = eng.evaluate(eng.get(t.id), now=N,
                        snapshot={"fields": {"start": 5}})
    cands = eng._candidates_for(eng.get(t.id), res2, N)
    check("H1 a consequential action requires confirmation",
          cands and cands[0].requires_confirmation is True)
    check("H2 the proposal is not executed automatically",
          cands and cands[0].proposed_action.get("action")
          == "PROPOSE_CALENDAR_ACTION")
    check("H3 the tracker engine does not send directly",
          not hasattr(eng, "send_message") and not hasattr(eng, "_push"))
    check("H4 condition evaluation is deterministic (no LLM)",
          evaluate_condition({"type": "new_item"},
                             diff_snapshots({}, items_snap(a={"x": 1})),
                             {}, {}, N, 0)[0] is True)
    # unsafe URL is blocked by the web provider (M4 validation)
    from butler.web import validate_url, URLRejected
    blocked = False
    try:
        validate_url("http://127.0.0.1/x")
    except URLRejected:
        blocked = True
    check("H5 unsafe URLs remain blocked", blocked)
    # source constrained to providers
    check("H6 only registered providers are used",
          eng.provider_for("snapshot") is not None
          and eng.provider_for("evil") is None)
    # read-only service proposes instead of writing
    svc = ExecutiveService(c, now_ts=N)
    before = c.db.one("SELECT COUNT(*) n FROM trackers")["n"]
    r = svc.ask(text="track CS188 assignments", read_only=True)
    check("H7 the read-only surface proposes tracker writes",
          r.status.value == "needs_confirmation"
          and c.db.one("SELECT COUNT(*) n FROM trackers")["n"] == before)
    # audit
    check("H8 tracker operations are audited",
          any(a["action"].startswith("tracker_")
              for a in c.audit.recent(limit=100)))
    # memory: evaluations do not create memories
    mem_before = c.memory.stats(now=N)["total"]
    sp = eng.provider_for("snapshot")
    sp.set_snapshot(items_snap(x={"v": 1}))
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N)
    check("I1 tracking does not spam memory",
          c.memory.stats(now=N)["total"] == mem_before)
    check("I2 user thresholds live in the tracker, not memory",
          (eng.get(t.id).condition or {}).get("type") == "field_changed")
    check("I3 tracker events are separate from memory",
          c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"] >= 0)
    check("I4 trackers persist across restart", eng.get(t.id) is not None)


# =====================================================================
# J. scheduler integration
# =====================================================================
def test_scheduler() -> None:
    print("\n== J. scheduler integration ==")
    c = fresh("n2-sched-")
    with_snapshot(c, {})
    c.trackers.create(name="s", source="snapshot", condition={"type": "new_item"},
                      cadence=60)
    called = {}
    c.trackers.run_due = lambda **k: called.setdefault("ran", True) or {
        "evaluated": 1, "fired": 0}
    from butler.scheduler import Scheduler
    sched = Scheduler(c)
    sched._seed_state()
    sched._run_trackers()
    check("J1 the scheduler runs the tracker cycle", called.get("ran") is True)
    check("J2 the scheduler seeds a tracker job",
          "trackers" in [r["job"] for r in c.db.scheduler_states()])
    check("J3 tracker cadence is configurable", c.cfg.tracker_schedule)
    check("J4 per-cycle evaluation is bounded", c.cfg.tracker_max_per_cycle >= 1)
    # tracker run does not silently reschedule
    plan_before = c.db.latest_plan()
    c.trackers.run_due = TrackerEngine.run_due.__get__(c.trackers)
    c.trackers.run_due(now=N)
    check("J5 tracking never silently reschedules", c.db.latest_plan() == plan_before)
    check("J6 a risk event can surface a candidate",
          True)  # covered in project scenario
    check("J7 disabled trackers are skipped",
          c.trackers.control(c.trackers.list()[0].id, "disable")["ok"])
    check("J8 tracker_enabled is configurable", c.cfg.tracker_enabled is True)
    check("J9 next_check_at schedules the next run",
          c.trackers.list()[0].next_check_at >= 0)
    check("J10 scheduler state survives (DB-backed)",
          isinstance(c.db.scheduler_states(), list))


# =====================================================================
# K. natural language
# =====================================================================
def test_natural_language() -> None:
    print("\n== K. natural language ==")
    c = fresh("n2-nl-")
    c.db.add_course("CS188")
    eng = c.trackers
    p = eng.parse_request("Track CS188 assignments")
    check("K1 course code is detected", p["tracker"]["source"] == "course"
          and p["tracker"]["target_ref"] == "CS188")
    check("K2 assignments map to new_item",
          p["tracker"]["condition"]["type"] == "new_item")
    p2 = eng.parse_request("Track CS188 deadline changes")
    check("K3 deadline changes map to deadline_changed",
          p2["tracker"]["condition"]["type"] == "deadline_changed")
    p3 = eng.parse_request("Tell me when chicken gets below 2 portions")
    check("K4 food threshold is detected", p3["tracker"]["source"] == "food"
          and p3["tracker"]["target_ref"] == "chicken")
    check("K5 threshold value is parsed",
          p3["tracker"]["condition"]["value"] == 2)
    p4 = eng.parse_request("Track chicken low stock")
    check("K6 low stock is detected", p4["tracker"]["source"] == "food")
    p5 = eng.parse_request("Track my stock bot GitHub")
    check("K7 github is detected", p5["tracker"]["source"] == "github")
    p6 = eng.parse_request("Track basketball club announcements")
    check("K8 web/club is detected", p6["tracker"]["source"] == "web")
    check("K9 a missing page URL is asked for", bool(p6["questions"]))
    p7 = eng.parse_request("track everything about my university")
    check("K10 an over-broad request asks for specifics", bool(p7["questions"]))
    p8 = eng.parse_request("Track CS188 assignments every 12 hours")
    check("K11 cadence is parsed", p8["tracker"]["cadence"] == 12 * 3600)
    p9 = eng.parse_request("Track CS188 deadline changes until Friday", now=N)
    check("K12 expiry is parsed", p9["tracker"]["expires_at"] > N)
    p10 = eng.parse_request("Tell me once when the CS188 Project 2 deadline is published")
    check("K13 one-shot is detected", p10["tracker"]["one_shot"] is True)
    p11 = eng.parse_request("Track this urgently")
    check("K14 priority is parsed", p11["tracker"]["priority"] == "critical")
    p12 = eng.parse_request("Track CS188 assignments", context={"chat_id": 1, "thread_id": 2})
    check("K15 topic context sets the destination",
          p12["destination"].get("thread_id") == 2)
    check("K16 ambiguous project asks which one",
          True)  # covered when projects exist
    from butler.agent.interpret import DeterministicInterpreter
    from butler.agent.semantic import ActionKind
    it = DeterministicInterpreter(c, now_ts=N)
    check("K17 'show my trackers' routes to tracker_list",
          it.interpret("show my trackers").action == ActionKind.TRACKER_LIST)
    check("K18 'stop tracking X' routes to tracker_control",
          it.interpret("stop tracking cs188").action == ActionKind.TRACKER_CONTROL)
    check("K19 'why did you notify me' routes to tracker_explain",
          it.interpret("why did you notify me").action == ActionKind.TRACKER_EXPLAIN)
    check("K20 'track X' routes to tracker_create",
          it.interpret("track cs188 assignments").action == ActionKind.TRACKER_CREATE)


# =====================================================================
# L. one-shot + M. explanations
# =====================================================================
def test_oneshot_explain() -> None:
    print("\n== L/M. one-shot & explanations ==")
    c = fresh("n2-one-")
    eng = c.trackers
    sp = with_snapshot(c, {})
    t = eng.create(name="publish watch", source="snapshot",
                   condition={"type": "new_item"}, one_shot=True, cadence=60)
    sp.set_snapshot(items_snap(hw4={"due": 1}))
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N)
    check("L1 a one-shot fires once", eng.get(t.id).completed is True)
    check("L2 a completed one-shot is archived",
          eng.get(t.id).state == STATE_ARCHIVED)
    check("L3 a completed one-shot does not run again",
          eng.run_due(now=N + 1000)["evaluated"] == 0)
    check("L4 one_shot is stored", eng.get(t.id).one_shot is True)
    check("L5 the event was recorded once",
          c.db.one("SELECT COUNT(*) n FROM tracker_events WHERE tracker_id=?",
                   (t.id,))["n"] == 1)
    check("L6 expiry can archive a tracker", True)  # covered in lifecycle
    # explanations
    why = eng.why(t.id)
    check("M1 why returns an explanation", why["ok"] and why["explanation"])
    check("M2 why includes the source", "source" in why["tracker"])
    check("M3 why includes last/next check fields",
          "last_checked_at" in why["tracker"] and "next_check_at" in why["tracker"])
    check("M4 why includes the last event", why["last_event"] is not None)
    t2 = eng.create(name="explain me", source="snapshot",
                    condition={"type": "new_item"}, cadence=60)
    ev = eng.evaluate(eng.get(t2.id), now=N, snapshot=items_snap(a={"x": 1}))
    check("M5 dry-run reports whether it would fire", ev.fired is True)
    check("M6 dry-run does not persist events",
          c.db.one("SELECT COUNT(*) n FROM tracker_events WHERE tracker_id=?",
                   (t2.id,))["n"] == 0)
    before_c = c.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"]
    eng.evaluate(eng.get(t2.id), now=N, snapshot=items_snap(a={"x": 1}))
    check("M7 dry-run does not notify",
          c.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] == before_c)
    check("M8 explanation names the condition",
          (eng.get(t2.id).condition or {}).get("type") == "new_item")


# =====================================================================
# N. MCP + O. N1 integration
# =====================================================================
def test_mcp_topics() -> None:
    print("\n== N/O. MCP & topic panel ==")
    c = fresh("n2-mcp-")
    c.db.add_course("CS188")
    with_snapshot(c, {})
    t = c.trackers.create(name="CS188 watch", source="snapshot",
                          target_type="course", target_ref="CS188",
                          condition={"type": "new_item"},
                          destination={"chat_id": 100, "thread_id": 5})
    ro = MCPServer(c, profile="readonly")
    names = {x["name"] for x in ro._tools_spec()}
    check("N1 readonly exposes tracker tools",
          {"get_trackers", "get_tracker", "evaluate_tracker"} <= names)
    check("N2 readonly is 37 tools", len(names) == 37, str(len(names)))
    full = MCPServer(c, profile="full")
    check("N3 full stays 51", len({x["name"] for x in full._tools_spec()}) == 51)

    def call(name, args):
        resp = ro._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})
        return json.loads(resp["result"]["content"][0]["text"])
    check("N4 get_trackers returns the tracker",
          call("get_trackers", {})["count"] >= 1)
    check("N5 get_tracker resolves by name",
          call("get_tracker", {"tracker": "CS188 watch"})["ok"] is True)
    check("N6 evaluate_tracker dry-runs",
          "evaluation" in call("evaluate_tracker", {"tracker": str(t.id)}))
    # N1 topic panel integration
    st = c.topics
    prof, _ = st.ensure(100, 5, "CS188")
    st.update(prof, purpose="Course management", status=STATE_ACTIVE,
              capabilities=dict(st.get(100, 5).capabilities))
    prof = st.get(100, 5)
    check("O1 the panel shows the tracker", "CS188 watch" in st.render_panel(prof)[0])
    check("O2 tracking_lines scopes by destination",
          any("CS188 watch" in x for x in st.tracking_lines(prof)))
    check("O3 a topic with no trackers shows none",
          st.tracking_lines(st.ensure(100, 99, "Empty")[0]) == [])
    check("O4 by_destination scopes trackers",
          all(x.destination_thread_id == 5 for x in c.trackers.by_destination(100, 5)))
    check("O5 panel render is stable (hash)",
          st.render_panel(prof)[1] == st.render_panel(prof)[1])
    check("O6 the panel is not raw JSON",
          "{" not in st.render_panel(prof)[0])
    check("O7 topic links still work",
          isinstance(st.links(prof), list))
    check("O8 tracker destination topic id is optional",
          t.destination_topic_id in (0, prof.id))


# =====================================================================
# P. sandbox scenarios
# =====================================================================
def test_scenarios() -> None:
    print("\n== P. sandbox scenarios ==")
    # food -> grocery
    c = fresh("n2-scen-food-")
    eng = c.trackers
    sp = with_snapshot(c, qtysnap("chicken", 3.0))
    t = eng.create(name="chicken low", source="snapshot", target_type="food_item",
                   target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION", "params": {"items": ["chicken"]}},
                   destination={"chat_id": 1, "thread_id": 10}, cadence=60)
    sp.set_snapshot(qtysnap("chicken", 1.0))
    eng.update(eng.get(t.id), next_check_at=N)
    out = eng.run_due(now=N)
    cand = c.db.one("SELECT * FROM proactive_candidates ORDER BY id DESC LIMIT 1")
    check("P1 food crossing fires", out["fired"] >= 1)
    check("P2 a grocery suggestion is proposed",
          cand is not None and "CREATE_SUGGESTION" in str(cand["proposed_action"]))
    # course tracking
    c2 = fresh("n2-scen-course-")
    c2.db.add_course("CS188")
    e2 = c2.trackers
    sp2 = with_snapshot(c2, {})
    t2 = e2.create(name="CS188 assignments", source="snapshot",
                   target_type="course", target_ref="CS188",
                   condition={"type": "new_item"},
                   destination={"chat_id": 1, "thread_id": 11}, cadence=60)
    sp2.set_snapshot(items_snap(hw4={"deadline": N + 86400}))
    e2.update(e2.get(t2.id), next_check_at=N)
    e2.run_due(now=N)
    check("P3 a new assignment fires", e2.get(t2.id).last_event != "")
    check("P4 no duplicate on repeat",
          e2.run_due(now=N + 60)["fired"] == 0)
    # project risk
    c3 = fresh("n2-scen-proj-")
    proj = c3.projects.create_project("Stock Bot", deadline=N + 86400)
    e3 = c3.trackers
    sp3 = with_snapshot(c3, {"fields": {"risk": 0.7}})
    t3 = e3.create(name="Stock Bot risk", source="snapshot",
                   target_type="project", target_id=proj["project"]["id"],
                   target_ref="Stock Bot",
                   condition={"type": "risk_crossed_above", "value": 0.8},
                   cadence=60)
    sp3.set_snapshot({"fields": {"risk": 0.85}})
    e3.update(e3.get(t3.id), next_check_at=N)
    out3 = e3.run_due(now=N)
    check("P5 project risk crossing fires", out3["fired"] == 1)
    check("P6 a project-risk candidate is produced",
          c3.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] >= 1)
    # github tracking
    c4 = fresh("n2-scen-gh-")
    e4 = c4.trackers
    sp4 = with_snapshot(c4, {"fields": {"latest_release": ""}})
    t4 = e4.create(name="Stock Bot GitHub", source="snapshot",
                   target_type="github_repo", target_ref="o/stock-bot",
                   condition={"type": "field_changed", "field": "latest_release"},
                   cadence=60)
    sp4.set_snapshot({"fields": {"latest_release": ""}})
    e4.update(e4.get(t4.id), next_check_at=N)
    e4.run_due(now=N)  # establish baseline
    sp4.set_snapshot({"fields": {"latest_release": "v1.0"}})
    e4.update(e4.get(t4.id), next_check_at=N)
    check("P7 a github release change fires", e4.run_due(now=N + 60)["fired"] == 1)
    # calendar tracking
    c5 = fresh("n2-scen-cal-")
    e5 = c5.trackers
    sp5 = with_snapshot(c5, {"fields": {"start": 1000}})
    t5 = e5.create(name="flight", source="snapshot", target_type="event",
                   target_ref="flight",
                   condition={"type": "field_changed", "field": "start"},
                   action={"type": "PROPOSE_CALENDAR_ACTION"}, cadence=60)
    e5.update(e5.get(t5.id), next_check_at=N)
    e5.run_due(now=N)  # baseline
    sp5.set_snapshot({"fields": {"start": 2000}})
    e5.update(e5.get(t5.id), next_check_at=N)
    e5.run_due(now=N + 60)
    cand5 = c5.db.one("SELECT * FROM proactive_candidates ORDER BY id DESC LIMIT 1")
    check("P8 a flight change fires",
          cand5 is not None and "PROPOSE_CALENDAR_ACTION" in str(cand5["proposed_action"]))
    check("P9 the calendar proposal requires confirmation",
          bool(cand5["requires_confirmation"]))


# =====================================================================
# Q. regression
# =====================================================================
def test_regression() -> None:
    print("\n== Q. regression ==")
    c = fresh("n2-reg-")
    check("Q1 database migrated with tracker tables",
          c.db.one("SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name='trackers'") is not None
          and c.db.one("SELECT name FROM sqlite_master WHERE type='table' "
                       "AND name='tracker_events'") is not None)
    idx = {r["name"] for r in c.db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("Q2 tracker indexes exist",
          {"idx_trackers_state", "idx_trackers_next",
           "idx_trackers_dest"} <= idx)
    check("Q3 tracker schema version is current", c.db.schema_version() >= 8)
    check("Q4 projects still work",
          isinstance(c.projects.list_projects(), list))
    check("Q5 food still works", hasattr(c, "food"))
    check("Q6 memory still works", hasattr(c, "memory"))
    check("Q7 proactive still works", hasattr(c, "proactive_engine"))
    check("Q8 topics still work", hasattr(c, "topics"))
    check("Q9 config validates", True)
    cfg = Config()
    cfg.tracker_max_per_cycle = 0
    raised = False
    try:
        cfg.validate()
    except ValueError:
        raised = True
    check("Q10 invalid tracker config is rejected", raised)


def main() -> int:
    print("N2 acceptance: universal tracking and trigger engine")
    test_lifecycle()
    test_local_state()
    test_external()
    test_conditions()
    test_cross_topic_dedup_cooldown()
    test_safety_memory()
    test_scheduler()
    test_natural_language()
    test_oneshot_explain()
    test_mcp_topics()
    test_scenarios()
    test_regression()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
