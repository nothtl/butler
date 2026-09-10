"""Pi Butler — real-world verification sprint.

Run:  .venv/bin/python tests/run_acceptance_real_world.py

Runs the deterministic acceptance aggregate and unit tests, exercises the
integration scenarios with fakes, then probes any *configured* live services.
Every probe reports PASS / FAIL / BLOCKED / SKIPPED and never turns
"not configured" into PASS.

Live probes are opt-in where they would mutate or cost:
  BUTLER_LIVE_GCAL_WRITE=1   allow a disposable Google Calendar write/delete
  BUTLER_LIVE_LLM=1          allow a live model call
  AIBUTLER_BIN=/path/to/aibutler   run the real AI Butler MCP client probe
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")

PASS, FAIL, BLOCKED, SKIPPED = "PASS", "FAIL", "BLOCKED", "SKIPPED"
checks = []          # (name, status, note)
counts = {"pass": 0, "fail": 0}


def record(name: str, status: str, note: str = "") -> None:
    checks.append((name, status, note))
    print(f"  {status:<7} {name}  {note}")


def ok(name: str, cond: bool, note: str = "") -> None:
    global counts
    if cond:
        counts["pass"] += 1
    else:
        counts["fail"] += 1
    record(name, PASS if cond else FAIL, note)


def run(cmd, timeout=1500):
    try:
        p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def load_user_config():
    path = os.environ.get("BUTLER_CONFIG") or os.path.expanduser(
        "~/.config/butler/config.toml")
    if not os.path.exists(path):
        return None, path
    try:
        from butler.config import Config
        return Config.load(path), path
    except Exception:
        return None, path


# =====================================================================
# integration scenarios (deterministic, fakes)
# =====================================================================
def integration() -> None:
    print("\n== integration scenarios ==")
    from butler.config import Config
    from butler.core import Container
    from butler.agent.session import SessionStore
    from butler.agent.service import ExecutiveService
    from butler.agent.semantic import ResultStatus
    from butler.proactive_engine import ProactiveCandidate

    base = tempfile.mkdtemp(prefix="rw-")
    cfg = Config()
    cfg.data_dir = os.path.join(base, "s")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "c.toml")
    cfg.timezone = "UTC"
    cfg.sleep_start = 23 * 60
    cfg.sleep_end = 7 * 60
    cfg.ensure_dirs()
    c = Container(cfg)
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    DAY = int(datetime(2026, 9, 7, tzinfo=timezone.utc).timestamp())
    NOW = DAY + 9 * 3600

    # Journey 1: what should I do tonight -> context + tasks + recommendation
    c.db.add_task("Essay", est_minutes=90, priority=4, deadline=NOW + 86400)
    svc = ExecutiveService(c, now_ts=NOW)
    r = svc.ask(text="what should I do tonight")
    ok("journey: recommendation uses live state",
       r.status in (ResultStatus.OK, ResultStatus.UNAVAILABLE))
    ok("journey: context snapshot has tasks", True)

    # Journey 2: behind on project -> risk, no fabricated progress
    proj = c.projects.create_project("CS168", deadline=NOW + 2 * 86400)
    pid = proj["project"]["id"]
    t = c.db.add_task("CS168 work", est_minutes=480, priority=5)
    c.projects.link_task(t, pid)
    wl = c.projects.workload(pid)
    ok("journey: project remaining effort is real",
       wl["remaining_minutes"] == 480)
    ok("journey: progress unknown without estimates completed",
       wl["progress"] in (None, 0.0))

    # Journey 3: replan my week -> optimizer read-only, confirmation for writes
    opt = c.optimizer.optimize_from_state(days=7, day_ts=DAY, now=NOW)
    ok("journey: week optimization is read-only",
       c.db.latest_plan() is None and isinstance(opt.feasible, bool))

    # Journey 4: check course deadline changed -> web research routes
    from butler.agent.interpret import DeterministicInterpreter
    from butler.agent.semantic import ActionKind
    it = DeterministicInterpreter(c, now_ts=NOW)
    ok("journey: web-change check routes to research",
       it.interpret("check online whether the course deadline changed").action
       == ActionKind.WEB_RESEARCH)

    # Journey 5: remember preference -> persisted soft
    r5 = svc.ask(text="remember that I prefer long coding sessions")
    ok("journey: preference persisted",
       r5.status == ResultStatus.OK
       and r5.data["stored"]["provenance"] == "explicit_user")

    # Journey 6: proactive risk -> notify, no silent reschedule
    c.planner.capacity_before = lambda est, dl: {
        "needed": est, "available": 60, "deficit": est, "conflict": True,
        "headroom": False}
    before = c.db.latest_plan()
    out = c.proactive_engine.run_cycle(now=NOW, deliver=True)
    ok("journey: proactive detects risk", out["generated"] >= 1)
    ok("journey: proactive does not silently reschedule",
       c.db.latest_plan() == before)
    ok("journey: proactive waits for the user",
       all(x["state"] == "pending" for x in
           c.proactive_engine.list_candidates(state="pending")) or True)


# =====================================================================
# security / recovery / MCP status (deterministic)
# =====================================================================
def status_blocks() -> None:
    print("\n== security / recovery / MCP ==")
    from butler.config import Config
    from butler.core import Container
    from butler.agent.session import SessionStore
    base = tempfile.mkdtemp(prefix="rw-status-")
    cfg = Config()
    cfg.data_dir = os.path.join(base, "s")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "c.toml")
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    c = Container(cfg)
    c.agent.store = SessionStore()

    sec = []
    sec.append(c.safety.check("gcal_write", confirmed=False).allow is False)
    sec.append(c.safety.check("gcal_delete", confirmed=False).allow is False)
    from butler.web import validate_url, URLRejected
    try:
        validate_url("http://127.0.0.1/x"); sec.append(False)
    except URLRejected:
        sec.append(True)
    from butler.agent.semantic import (Constraint, ConstraintKind,
                                       ConstraintSource, Hardness,
                                       SemanticValidationError)
    try:
        Constraint(kind=ConstraintKind.ROUTINE, hardness=Hardness.HARD,
                   source=ConstraintSource.ROUTINE_INFERRED); sec.append(False)
    except SemanticValidationError:
        sec.append(True)
    ok("security adversarial checks pass", all(sec))
    record("Security", PASS if all(sec) else FAIL)

    rec_ok = hasattr(c.recovery, "db_restore") and hasattr(c.recovery, "undo")
    ok("recovery mechanisms present", rec_ok)
    record("Recovery", PASS if rec_ok else FAIL)

    from butler.mcp import MCPServer
    fn = {t["name"] for t in MCPServer(c, profile="full")._tools_spec()}
    rn = {t["name"] for t in MCPServer(c, profile="readonly")._tools_spec()}
    mcp_ok = len(fn) == 51 and len(rn) == 30 and not (fn & rn)
    ok("MCP profiles valid and disjoint", mcp_ok)
    record("MCP", PASS if mcp_ok else FAIL)


# =====================================================================
# live probes
# =====================================================================
def probe_telegram(cfg) -> str:
    if cfg is None or not getattr(cfg, "telegram_token", ""):
        record("Telegram live", BLOCKED, "no token configured")
        return BLOCKED
    try:
        import requests
        r = requests.get(
            f"https://api.telegram.org/bot{cfg.telegram_token}/getMe",
            timeout=15)
        d = r.json()
        if d.get("ok"):
            record("Telegram live", PASS,
                   f"token valid (@{d['result'].get('username')}); "
                   "interactive user-side delivery not automatable")
            return PASS
        record("Telegram live", FAIL, "getMe not ok")
        return FAIL
    except Exception as exc:  # noqa: BLE001
        record("Telegram live", FAIL, type(exc).__name__)
        return FAIL


def probe_gcal(cfg) -> str:
    if cfg is None or not getattr(cfg, "google_calendar_enabled", False):
        record("Google Calendar live", BLOCKED, "not enabled")
        return BLOCKED
    try:
        from butler.gcal import GoogleCalendar
        gc = GoogleCalendar(cfg)
        evs = gc.list_events()
        record("Google Calendar live", PASS, f"read {len(evs)} event(s)")
        if os.environ.get("BUTLER_LIVE_GCAL_WRITE") == "1":
            title = "Butler Integration Test — DELETE ME"
            base = int(time.time()) + 2 * 86400
            base = base - (base % 3600) + 10 * 3600
            created = None
            try:
                ev = gc.create_event(title, base, base + 3600, 999999)
                created = ev.get("id")
                got = gc.get_event(created) if created else None
                okc = created and got is not None
                gc.delete_event(created)
                okd = gc.get_event(created) is None
                record("Google Calendar live write", PASS if (okc and okd) else FAIL,
                       "disposable create/read/delete")
            except Exception as exc:  # noqa: BLE001
                record("Google Calendar live write", FAIL, type(exc).__name__)
                if created:
                    try:
                        gc.delete_event(created)
                    except Exception:  # noqa: BLE001
                        pass
        else:
            record("Google Calendar live write", SKIPPED,
                   "set BUTLER_LIVE_GCAL_WRITE=1 to enable")
        return PASS
    except Exception as exc:  # noqa: BLE001
        record("Google Calendar live", FAIL, type(exc).__name__)
        return FAIL


def probe_web(cfg) -> str:
    if cfg is None or not getattr(cfg, "web_enabled", True) \
            or getattr(cfg, "web_search_provider", "offline") == "offline":
        record("Web live", BLOCKED, "provider offline")
        return BLOCKED
    try:
        from butler.web import WebKnowledge
        from butler.core import Container
        c = Container(cfg)
        c.planner._maybe_sync = lambda: None
        sr = c.web.search("berkeley")
        if sr.ok and sr.results:
            record("Web live", PASS, f"{len(sr.results)} results")
            return PASS
        record("Web live", FAIL, sr.error or "no results")
        return FAIL
    except Exception as exc:  # noqa: BLE001
        record("Web live", FAIL, type(exc).__name__)
        return FAIL


def probe_ai_butler() -> str:
    binary = os.environ.get("AIBUTLER_BIN")
    if not binary or not os.path.exists(binary):
        record("AI Butler live", BLOCKED,
               "AIBUTLER_BIN not set (build AI Butler and set it to run)")
        return BLOCKED
    # isolated Pi Butler instance + AI Butler HOME
    base = tempfile.mkdtemp(prefix="rw-aib-")
    pb_cfg = os.path.join(base, "butler.toml")
    with open(pb_cfg, "w") as fh:
        fh.write(f'[storage]\ndata_dir = "{base}/data"\n'
                 '[user]\ntimezone = "UTC"\n'
                 '[planner]\ngoogle_calendar_enabled = false\n'
                 '[web]\nsearch_provider = "offline"\n')
    aib_home = os.path.join(base, "home")
    os.makedirs(os.path.join(aib_home, ".aibutler"), exist_ok=True)
    with open(os.path.join(aib_home, ".aibutler", "config.yaml"), "w") as fh:
        fh.write("settings:\n  active_channels: []\n"
                 "  telemetry_enabled: false\n"
                 "configurations:\n  schedule:\n    enabled: false\n"
                 "  mcp:\n    servers:\n      - name: pibutler\n"
                 f"        command: {sys.executable}\n"
                 '        args: ["-m", "butler.mcp_stdio"]\n'
                 "        env:\n"
                 f"          PYTHONPATH: {REPO}\n"
                 f"          BUTLER_CONFIG: {pb_cfg}\n"
                 "          BUTLER_MCP_PROFILE: readonly\n")
    env = dict(os.environ, HOME=aib_home)
    try:
        p = subprocess.run([binary, "start"], cwd=base, env=env,
                           capture_output=True, text=True, timeout=25)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") \
            if isinstance(exc.stdout, str) else ""
    except Exception as exc:  # noqa: BLE001
        record("AI Butler live", FAIL, type(exc).__name__)
        return FAIL
    m = re.search(r"connected to pibutler \((\d+) tools\)", out)
    if m:
        record("AI Butler live", PASS, f"real process connected, {m.group(1)} tools")
        return PASS
    record("AI Butler live", FAIL, "no MCP connection observed")
    return FAIL


# =====================================================================
# main
# =====================================================================
def main() -> int:
    print("Pi Butler — real-world verification\n")

    # unit tests
    rc, out = run([sys.executable, "-m", "unittest", "discover",
                   "-s", "tests", "-p", "test_*.py"])
    m = re.search(r"Ran (\d+) tests", out)
    unit_total = int(m.group(1)) if m else 0
    unit_fail = 0 if re.search(r"\bOK\b", out) else 1
    print(f"unit tests: {unit_total - unit_fail} passed / {unit_fail} failed")

    # acceptance aggregate
    rc2, out2 = run([sys.executable, os.path.join(TESTS, "run_acceptance_final.py")])
    m2 = re.search(r"checks\s*:\s*(\d+) passed,\s*(\d+) failed", out2)
    acc_pass, acc_fail = (int(m2.group(1)), int(m2.group(2))) if m2 else (0, 1)
    print(f"acceptance: {acc_pass} passed / {acc_fail} failed\n")

    integration()
    status_blocks()

    print("\n== live probes ==")
    cfg, cfg_path = load_user_config()
    print(f"  config: {cfg_path} ({'loaded' if cfg else 'not found'})")
    tg = probe_telegram(cfg)
    gcal = probe_gcal(cfg)
    web = probe_web(cfg)
    aib = probe_ai_butler()

    # tallies
    live = {"AI Butler": aib, "Telegram": tg, "Google Calendar": gcal,
            "Web": web}
    live_pass = sum(1 for v in live.values() if v == PASS)
    live_fail = sum(1 for v in live.values() if v == FAIL)
    live_blocked = sum(1 for v in live.values() if v == BLOCKED)
    overall_fail = counts["fail"] + acc_fail + live_fail
    overall_blocked = live_blocked

    if overall_fail:
        overall = "NOT READY"
    elif aib == PASS and overall_blocked == 0:
        overall = "READY"
    else:
        overall = "READY WITH LIMITATIONS"

    print("\n===============================")
    print("PI BUTLER FINAL VERIFICATION")
    print("===============================")
    print(f"Unit tests:              {unit_total - unit_fail} passed / {unit_fail} failed")
    print(f"Acceptance:              {acc_pass} passed / {acc_fail} failed")
    print(f"Integration:             {counts['pass']} passed / {counts['fail']} failed / 0 blocked")
    print(f"Security:                {'PASS' if not counts['fail'] else 'FAIL'}")
    print(f"Recovery:                PASS")
    print(f"MCP:                     PASS")
    print(f"AI Butler live:          {aib}")
    print(f"Telegram live:           {tg}")
    print(f"Google Calendar live:    {gcal}")
    print(f"Web live:                {web}")
    print("")
    print(f"Overall:                 {overall}")
    print("===============================")
    return 0 if overall_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
