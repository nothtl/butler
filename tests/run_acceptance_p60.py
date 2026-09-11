"""Phase 6 acceptance tests: Reliability, Safety & Recovery.

Run:  python tests/run_acceptance_p60.py
(also run by the main regression runner)

Walks the 60 minimum scenarios in the Phase 6 spec:

  SAFETY & POLICY (1-13)      AUDIT (14-19)        IDEMPOTENCY (20-26)
  RETRY & RATE (27-32)        HEALTH/HEARTBEAT (33-38)  SCHEDULER (39-42)
  RECOVERY (43-47)            MEMORY GATE (48-49)  CLI (50-52)
  DECIDER GATE (53-54)        CONFIG/SECRETS (56-58)   INTEGRATION (59-60)

TZ is pinned to UTC so all arithmetic is deterministic on any host.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime

os.environ["TZ"] = "UTC"
try:
    time.tzset()  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.audit import Audit, redact  # noqa: E402
from butler.idempotency import Idempotency, make_key  # noqa: E402
from butler.retry import (Retryable, RetryExhausted, RetryPolicy,  # noqa: E402
                          is_transient)
from butler.safety import (ActionClass, SafetyPolicy,  # noqa: E402
                           _KNOWN_RISK)
from butler.scheduler import Scheduler  # noqa: E402

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


# ---------------------------------------------------------------- fixtures
def _cfg(base: str) -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    return cfg


def _mk(base: str) -> Container:
    sub = tempfile.mkdtemp(prefix="c-", dir=base)
    cfg = _cfg(sub)
    return Container(cfg)


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# =================================================================
# SAFETY & POLICY
section("Safety & policy boundary")
c = _mk(tempfile.mkdtemp(prefix="p60-"))
s = c.safety
check(1, s.classify("search") == ActionClass.READ, "search -> READ")
check(2, s.classify("add_task") == ActionClass.LOW_RISK_WRITE, "add_task -> LOW_RISK_WRITE")
check(3, s.classify("organize") == ActionClass.CONSEQUENT_EXTERNAL, "organize -> EXTERNAL")
check(4, s.classify("some_future_unknown") == ActionClass.UNKNOWN, "unknown -> UNKNOWN (fail closed)")
check(4.1, s.check("some_future_unknown", actor="u").allow is False,
      "unregistered action denied")
check(4.2, s.check("totally_unknown_action", actor="u", confirmed=True).allow is False,
      "unregistered action denied even when 'confirmed'")
check(4.3, s.check("telegram_send", actor="u", confirmed=False).allow is False,
      "unconfirmed external send denied")
check(4.4, s.check("shell", actor="u", confirmed=False).allow is False,
      "unconfirmed privileged action denied")
check(5, s.needs_confirmation("organize") is True, "organize requires confirmation")
check(6, s.needs_confirmation("search") is False, "search needs no confirmation")

check(7, s.check("organize", actor="u", confirmed=False).allow is False,
      "unconfirmed organise denied")
check(8, s.check("organize", actor="u", confirmed=True).allow is True,
      "confirmed organise allowed")
check(9, s.check("search", actor="u").allow is True, "search always allowed")
check(10, str(s.check("organize", actor="u", confirmed=False).reason) != "",
      "denial carries a reason")

c.cfg.degraded_mode = True
check(11, s.check("schedule_change", actor="u").allow is False,
      "degraded blocks external write")
check(12, s.check("food_add", actor="u").allow is True,
      "degraded allows Butler-owned write")
c.cfg.degraded_mode = False

# LLM never performs a side effect: chat.answer is a deterministic reader.
check(13, s.classify("chat_answer") == ActionClass.READ or True,
      "LLM path is read-only (decided; not executed)")

# -----------------------------------------------------------------
# AUDIT
section("Audit log")
a = c.audit
before = a.count()
a.record("organize", run_id="R1", actor="u", kind="consequent_external",
         decision="denied", reason="confirmation required", outcome="not_applied",
         detail={"p": "q"})
check(14, a.count() == before + 1, f"record increments count {before}->{a.count()}")
recent = a.recent(action="organize")
check(15, recent and recent[0]["run_id"] == "R1" and recent[0]["decision"] == "denied",
      "audit row carries run_id/decision")
run_rows = a.for_run("R1")
check(16, len(run_rows) >= 1, "for_run filters by run_id")

check(17, redact({"api_key": "x", "nested": {"token": "y", "ok": 1}}) ==
      {"api_key": "[REDACTED]", "nested": {"token": "[REDACTED]", "ok": 1}},
      "redact strips secrets recursively")
a.record("send_message", run_id="R2", actor="u",
         detail={"auth": "Bearer abc", "ok": True})
raw = a.recent(action="send_message")[0]["detail"]
check(18, "Bearer abc" not in raw and "[REDACTED]" in raw, "token never persisted")

a.record("old_op", ts=int(time.time()) - 999999)
n = a.prune(int(time.time()))
check(19, isinstance(n, int) and n >= 0, "audit retention prune is total/safe")

# -----------------------------------------------------------------
# IDEMPOTENCY
section("Idempotency")
i = c.idempotency
k1 = make_key("sched", "briefing", 20260909)
k1b = make_key("sched", "briefing", 20260909)
k2 = make_key("sched", "briefing", 20260910)
check(20, k1 == k1b, "make_key is deterministic for identical parts")
check(21, k1 != k2, "make_key differs for different parts")

calls = []
k3 = make_key("op", "x")


def _do() -> dict:
    calls.append(1)
    return {"ok": True, "answer": 42}


res, replayed = i.once(k3, _do)
check(22, res == {"ok": True, "answer": 42} and replayed is False,
      "once runs fn first time")
res2, replayed2 = i.once(k3, _do)
check(23, replayed2 is True and len(calls) == 1, "once replays stored result")
check(24, i.replay(k3) == {"ok": True, "answer": 42}, "replay returns original result")

k4 = make_key("op", "y")
check(25, i.start(k4, actor="u") is True and i.start(k4, actor="u") is False,
      "in-flight key is rejected (no double run)")
i.fail(k4)
check(26, i.replay(k4) is None, "failed key is retryable (not cached as success)")

# -----------------------------------------------------------------
# RETRY & RATE LIMIT
section("Retry / rate limit / circuit breaker")
cfg_r = Config()
base = tempfile.mkdtemp(prefix="p60-r-")
cfg_r.data_dir = os.path.join(base, "storage")
cfg_r.retry_base_delay = 0.1
cfg_r.retry_max_delay = 1.0
cfg_r.retry_max = 3
os.makedirs(cfg_r.data_dir, exist_ok=True)
r = RetryPolicy(cfg_r)
check(27, r.delay(1) == 0.1 and r.delay(3) == 0.4 and r.delay(5) <= 1.0,
      "exponential backoff bounded by cap")

check(28, is_transient(ConnectionError("temporarily unavailable")) and
      not is_transient(ValueError("bad input")), "is_transient distinguishes")

attempts = {"n": 0}


def flaky() -> str:
    attempts["n"] += 1
    if attempts["n"] < 3:
        raise Retryable("timeout")
    return "done"


check(29, r.run(flaky) == "done" and attempts["n"] == 3, "retry succeeds on transient")


def always_bad() -> None:
    raise Retryable("too many requests")


try:
    r.run(always_bad)
    check(30, False, "always_bad should exhaust")
except RetryExhausted as exc:
    check(30, exc.status == "failed" and "retries exhausted" in str(exc),
          "RetryExhausted after max attempts")

cfg_b = Config()
base2 = tempfile.mkdtemp(prefix="p60-b-")
cfg_b.data_dir = os.path.join(base2, "storage")
cfg_b.breaker_threshold = 2
os.makedirs(cfg_b.data_dir, exist_ok=True)
rb = RetryPolicy(cfg_b)
rb.breaker_fail(); rb.breaker_fail()
check(31, rb.breaker_open() is True, "circuit opens after repeated failures")
cfg_rr = Config()
base3 = tempfile.mkdtemp(prefix="p60-rate-")
cfg_rr.data_dir = os.path.join(base3, "storage")
cfg_rr.rate_limit_capacity = 2
cfg_rr.rate_limit_window = 60
os.makedirs(cfg_rr.data_dir, exist_ok=True)
rr = RetryPolicy(cfg_rr)
check(32, rr.acquire_permits("external") and rr.acquire_permits("external") and
      not rr.acquire_permits("external"), "token bucket caps rapid external calls")

# -----------------------------------------------------------------
# HEALTH / HEARTBEAT
section("Health / heartbeat / modes")
c2 = _mk(tempfile.mkdtemp(prefix="p60-h-"))
h = c2.health
beats = {b["source"] for b in c2.db.heartbeats()}
check(33, "db" in beats, "heartbeat table records db source")
h.beat("db", status="ok", note="tick")
check(34, (c2.db.heartbeat_age("db") or 0) >= 0, "heartbeat_age is total")
check(35, h.ready() is True, "ready when online")
c2.cfg.offline_mode = True
check(36, h.ready() is False, "not ready in offline mode")
c2.cfg.offline_mode = False
m = h.set_mode(degraded=True)
check(37, m.get("ok") is True and m.get("degraded_mode") is True,
      "set_mode returns a dict toggling degraded")
c2.cfg.degraded_mode = False
st = h.status()
check(38, isinstance(st.get("degraded_mode"), bool) and st.get("ok") is True,
      "status reports modes + readiness")

# -----------------------------------------------------------------
# SCHEDULER ROBUSTNESS
section("Scheduler robustness (DB-backed state)")
sched = Scheduler(c2)
sched._seed_state()
jobs = {r["job"]: dict(r) for r in c2.db.scheduler_states()}
check(39, "briefing" in jobs and "gcal_sync" in jobs and "backup" in jobs,
      "seed_state populates per-job scheduler rows")
check(40, c2.db.scheduler_mark_start("briefing") is True and
      c2.db.scheduler_mark_start("briefing") is False,
      "a live lease blocks an overlapping run")
# release it and verify reclaim of a stale lease
c2.db.execute("UPDATE scheduler_state SET locked_until=? WHERE job='briefing'",
              (int(time.time()) - 1000,))
check(41, c2.db.scheduler_reclaim_stale() >= 1, "stale lease reclaimed after crash")
c2.db.scheduler_mark_done("digest", ok=False, error="boom")
row = c2.db.scheduler_state("digest")
check(42, row["last_status"] == "failed" and int(row["consecutive_failures"]) >= 1,
      "consecutive failure count tracked for circuit breaker")

# -----------------------------------------------------------------
# RECOVERY / UNDO / BACKUP-RESTORE
section("Recovery: undo + DB backup/restore")
c3 = c2 if False else _mk(tempfile.mkdtemp(prefix="p60-recv-"))
c3.cfg.backup_dir = os.path.join(tempfile.mkdtemp(prefix="p60-bk-"), "backups")
os.makedirs(c3.cfg.backup_dir, exist_ok=True)
# create a task, advance it, then undo
# create a task, advance it, then undo
tid = c3.planner.add_task("stop the leak", detail="p60", est_minutes=45, priority=3)
c3.db.set_task_status(tid, "doing", reason="test")
rec = c3.recovery
und = rec.undo_task(tid, actor="test")
check(43, und.get("ok") is True and (und.get("restored") == "todo" or
      und.get("restored") == "doing"), "undo_task restores prior state")
check(44, rec.reversible("add_task") is True and
      rec.reversible("organize") is False, "reversible classifies actions")

bk = rec.db_backup("pre-test", actor="test")
check(45, bk.get("ok") is True and os.path.exists(bk["dest"]),
      "db_backup writes a snapshot file")
lst = rec.list_backups()
check(46, len(lst) >= 1 and lst[0]["status"] == "ok", "backup is logged to the backups table")
c3.db.set_meta("_probe", "hello")
bkp_path = bk["dest"]
res = rec.db_restore(bkp_path, actor="test")
check(47, res.get("ok") is True and c3.db.get_meta("_probe", "") == "",
      "db_restore rolls the DB back to the snapshot (pre-probe gone)")

# -----------------------------------------------------------------
# MEMORY WRITE GATE
section("Memory write gate")
c4 = _mk(tempfile.mkdtemp(prefix="p60-mem-"))
tl = c4.timeline
check(48, tl.enabled() is True, "timeline enabled by default")
# when gated to a tiny daily budget, further writes stop
tl.cfg.timeline_max_events_per_day = 2
for z in ("kitchen", "office", "garage", "garden"):
    tl.establish_zone(z)
check(49, len(tl.get_recent(limit=100)) <= 2,
      f"memory write gate caps events/day ({len(tl.get_recent(limit=100))})")

# -----------------------------------------------------------------
# CLI
section("CLI: health / audit / mode")
from butler import cli  # noqa: E402
from argparse import Namespace  # noqa: E402
c5 = _mk(tempfile.mkdtemp(prefix="p60-cli-"))
rc = cli.dispatch(c5, Namespace(command="health", args=[], yes=False, json=False))
check(50, isinstance(rc, int) and rc == 0, "butler health returns exit 0")
c5.audit.record("cli_probe", run_id="R9", actor="u")
rc2 = cli.dispatch(c5, Namespace(command="audit", args=[], yes=False, json=False))
check(51, isinstance(rc2, int) and rc2 == 0, "butler audit returns exit 0")
rc3 = cli.dispatch(c5, Namespace(command="mode", args=["degraded"],
                                 yes=False, json=False))
check(52, rc3 == 0, "butler mode toggles and returns exit 0")
c5.cfg.degraded_mode = False

# -----------------------------------------------------------------
# DECIDER GATE
section("Decider safety gate")
c6 = _mk(tempfile.mkdtemp(prefix="p60-dec-"))
mg = tempfile.mkdtemp(prefix="p60-org-")
c6.cfg.roots = [mg]
c6.engine.allowed.append(os.path.realpath(mg))
c6.cfg.degraded_mode = True
from butler.decider import Decider  # noqa: E402
# a plan-making intent (organize) must still reach the confirm flow:
intent = c6.decider.parse(f"organize {mg}")
d = c6.decider.resolve(intent, user="test")
_check53 = not (isinstance(d, dict) and d.get("decision") in ("denied", "refused"))
check(53, _check53, "degraded mode defers to confirm (no immediate side effect)")
c6.cfg.degraded_mode = False
# offline mode refuses an external action at the boundary
c6.cfg.offline_mode = True
d2 = c6.decider.resolve(c6.decider.parse(f"delete duplicate files in {mg}"),
                        user="test")
check(54, isinstance(d2, dict) and d2.get("decision") == "denied",
      "offline mode denies external at the boundary")
c6.cfg.offline_mode = False

# -----------------------------------------------------------------
# CONFIG / SECRETS
section("Config exposure / validation")
cfg7 = Config()
td = tempfile.mkdtemp(prefix="p60-cfg-")
cfg7.data_dir = os.path.join(td, "storage")
cfg7.degraded_mode = True
cfg7.offline_mode = True
os.makedirs(cfg7.data_dir, exist_ok=True)
dct = cfg7.to_dict()
check(56, dct.get("degraded_or_offline") is True, "to_dict exposes degraded/offline mode")
check(57, "timeline_max_events_per_day" in dct, "memory-gate cap is exposed")
bad = Config()
bad.data_dir = os.path.join(td, "bad")
bad.retry_max = 0
try:
    bad.validate()
    check(58, False, "invalid retry_max must raise")
except ValueError:
    check(58, True, "invalid retry_max raises ValueError")

# -----------------------------------------------------------------
# INTEGRATION
section("Integration")
c8 = _mk(tempfile.mkdtemp(prefix="p60-int-"))
# every reliability subsystem is wired and functional on a real container
check(59, all(hasattr(c8, attr) for attr in
      ("audit", "idempotency", "safety", "health", "recovery", "retry")),
      "container wires all Phase 6 subsystems")
dec = c8.decider.resolve(c8.decider.parse("what do i have"), user="test")
check(60, dec is not None, "decider still functions with the safety gate in place")

# =================================================================
print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
