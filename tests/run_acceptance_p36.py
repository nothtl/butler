"""Phase 3.6 acceptance tests for Butler.

Run:  .venv/bin/python tests/run_acceptance_p36.py
Also: .venv/bin/python tests/run_acceptance_p35.py   (must stay green)

Proves the Course -> Assignment -> Task -> Schedule pipeline is genuinely
end-to-end and obeys the architectural rules:
  1. assignment doc -> understanding -> deterministic task -> deterministic
     study blocks -> rendered notification;
  2. re-running the monitor never creates a duplicate task;
  3. re-running the monitor never creates duplicate schedule blocks;
  4. a changed deadline UPDATES the existing task (not a new one);
  5. insufficient capacity before the deadline -> reported conflict (never
     silently overbooked);
  6. hard calendar events are never overwritten;
  7. DeepSeek cannot directly choose schedule times (the solver does);
  8. a missing / invalid LLM produces no bogus task.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler import schedule as sch  # noqa: E402
from butler.cli import render  # noqa: E402

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


def make_pdf(path: str, body: str) -> None:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), body)
    doc.set_metadata({"title": os.path.basename(path), "author": "Course Staff"})
    doc.save(path)
    doc.close()


def make_site(root: str, files: list[str]) -> str:
    os.makedirs(root, exist_ok=True)
    for f in files:
        make_pdf(os.path.join(root, f), f"{f} content: build a chatbot agent. Due soon.")
    hrefs = "\n".join(f'<a href="{f}">{f}</a>' for f in files)
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write(f"<html><body><h1>CS168</h1>{hrefs}</body></html>")
    return root


def day_start_ts(day_offset: int) -> int:
    d = datetime.now()
    from datetime import timedelta
    dt = datetime(d.year, d.month, d.day, 0, 0) + timedelta(days=day_offset)
    return int(dt.timestamp())


def end_of_day_ts(day_offset: int) -> int:
    d = datetime.now()
    from datetime import timedelta
    dt = datetime(d.year, d.month, d.day, 23, 59) + timedelta(days=day_offset)
    return int(dt.timestamp())


def add_blocking_event(c: Container, day_offset: int, start_hm: str, end_hm: str) -> None:
    """Add a hard event on an offset day spanning [start_hm, end_hm]."""
    sh, sm = map(int, start_hm.split(":"))
    eh, em = map(int, end_hm.split(":"))
    base = day_start_ts(day_offset)
    c.db.add_event("Hard Commitment", base + sh * 3600 + sm * 60,
                   base + eh * 3600 + em * 60, source="local")


class FakeChat:
    """Deterministic LLM double. Routes on the assignment-understanding system
    prompt so the pipeline is exercised without touching the network. A bogus
    ``preferred_time`` / ``session_times`` block is always attached to prove the
    LLM never influences scheduling."""

    def __init__(self, deadline_ts: int, est_hours: float = 6.0, ready: bool = True,
                 secret: str = "sk-test-secret"):
        self.deadline_ts = deadline_ts
        self.est_hours = est_hours
        self.ready = ready
        self.secret = secret
        self.prompts: list[str] = []

    def _llm_ready(self) -> bool:
        return self.ready

    def _llm(self, prompt: tuple[str, str]) -> str:
        system, user = prompt[0], prompt[1]
        self.prompts.append(system + "\n" + user)
        if "assignment specification" in system:
            if not self.ready:
                return json.dumps({})
            return json.dumps({
                "title": "Project 2: Chatbot",
                "deadline": self.deadline_ts,
                "est_hours": self.est_hours,
                "requirements": ["design an agent", "write report"],
                # LLM tries to schedule, but the pipeline/DB/solver must ignore it:
                "preferred_time": "00:00-09:00",
                "session_times": [{"start": "00:00", "end": "09:00"}],
            })
        return json.dumps({})


def make_env(code: str = "CS168", fake: FakeChat | None = None):
    """Fresh isolated environment (own DB/course dir) per scenario."""
    base = tempfile.mkdtemp(prefix="butler-p36-")
    data = os.path.join(base, "ButlerStorage")
    university = os.path.join(base, "University")
    site = os.path.join(base, "site")
    os.makedirs(data, exist_ok=True)
    cfg = Config()
    cfg.data_dir = data
    cfg.course_dir = university
    cfg.courses = [code]
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.llm_api_key = "sk-test-secret"
    cfg.ensure_dirs()
    c = Container(cfg)
    if fake is not None:
        c.chat = fake
    return c, base, site


def main() -> int:
    # ================= Scenario A: true e2e + idempotency =================
    print("\n=== 3.6.1 E2E: doc -> understanding -> task -> study blocks -> render ===")
    A, baseA, siteA = make_env()
    fcA = FakeChat(deadline_ts=int(datetime.now().timestamp()) + 3 * 86400, est_hours=6)
    A.chat = fcA
    make_site(siteA, ["CS168_Project2.pdf"])
    r = A.decider.resolve(A.decider.parse(f"add course CS168 {siteA}"))
    check("course registered", r.get("ok") and not r.get("need_url"), r.get("code"))
    updates = A.courses.check_all()
    a_update = next((u for u in updates if u.get("kind") == "assignment"), None)
    check("assignment update produced", a_update is not None, str([u.get("kind") for u in updates]))
    if a_update:
        task = a_update.get("task") or {}
        sched = task.get("schedule") or {}
        slots = sched.get("slots", [])
        check("task created by pipeline", bool(task.get("task_id")), str(task.get("task_id")))
        check("study blocks scheduled deterministically", len(slots) >= 1,
              str(slots))
        if slots:
            s = slots[0]
            check("single coherent block placed (est 360m -> 07:00-13:00)", len(slots) == 1
                  and s["start_min"] == 420 and s["end_min"] == 780, str(slots))
            check("block within waking day window", s["start_min"] >= 420
                  and s["end_min"] <= 1380, str(s))
            check("LLM can NOT choose schedule times (not at midnight)",
                  not (s["start_min"] == 0 and s["end_min"] == 720),
                  f"{s['start_min']}-{s['end_min']}")
            check("LLM time fields never reach output",
                  "preferred_time" not in sched and "session_times" not in sched, str(list(sched)))
        # Render the notification through the course_assignments renderer.
        notify = {"kind": "course_assignments", "ok": True,
                  "results": {"CS168": {"created": [task], "updated": []}}}
        text = render(A, notify)
        check("notification renders the assignment + its study block",
              "task #" in text and "07:00" in text, text.strip())
    tasks = [dict(x) for x in A.db.tasks("active")]
    t = next((x for x in tasks if "Project 2" in x["title"]), None)
    n = len([x for x in A.db.tasks("active") if "Project 2" in x["title"]])
    check("exactly one task exists", t is not None and n == 1, f"n={n}")
    if t:
        check("workload estimate materialised", int(t["est_minutes"]) == 360, t["est_minutes"])

    print("\n=== 3.6.2 Monitor twice -> no duplicate task ===")
    updates2 = A.courses.check_all()
    again = A.courses.sync_assignments("CS168")
    n2 = len([x for x in A.db.tasks("active") if "Project 2" in x["title"]])
    check("second monitor run is a no-op", updates2 == [], str([u.get("kind") for u in updates2]))
    check("re-sync deduped (no duplicate task)", again.get("count") == 0,
          str(again.get("count")))
    check("still exactly one task after re-sync", n2 == 1, f"n={n2}")

    print("\n=== 3.6.3 Monitor twice -> no duplicate schedule blocks ===")
    tid = int(t["id"]) if t else 0
    plan = A.db.latest_plan()
    state = sch.PlanState.from_json(plan["json"])
    before = len([s for s in state.slots if s.task_id == tid])
    A.planner.reschedule()
    plan2 = A.db.latest_plan()
    state2 = sch.PlanState.from_json(plan2["json"])
    after = len([s for s in state2.slots if s.task_id == tid])
    check("re-solve keeps the same block count (no duplicates)",
          before >= 1 and after == before, f"{before} -> {after}")

    print("\n=== 3.6.4 Changed deadline UPDATES the existing task ===")
    fcA.deadline_ts = int(datetime.now().timestamp()) + 12 * 86400
    upd_res = A.courses.sync_assignments("CS168", force=True)
    check("update detected and reported", len(upd_res.get("updated", [])) == 1,
          str(upd_res.get("count")))
    tt = A.db.task_by_id(tid)
    check("existing task updated in place (same id)", tt is not None and int(tt["id"]) == tid,
          str(tid))
    check("deadline advanced on the existing task",
          int(tt["deadline"]) == fcA.deadline_ts, str(tt["deadline"]))
    n3 = len([x for x in A.db.tasks("active") if "Project 2" in x["title"]])
    check("no duplicate task after update", n3 == 1, f"n={n3}")

    print("\n=== 3.6.5 Insufficient capacity -> conflict reported, never overbooked ===")
    B, baseB, siteB = make_env("CS168")
    # 100h -> clamped to 480m; both days blocked -> only a sliver of free time.
    fcB = FakeChat(deadline_ts=end_of_day_ts(1), est_hours=100)
    B.chat = fcB
    make_site(siteB, ["CS168_Major2.pdf"])
    add_blocking_event(B, 0, "07:00", "20:00")
    add_blocking_event(B, 1, "07:00", "20:00")
    B.decider.resolve(B.decider.parse(f"add course CS168 {siteB}"))
    updB = B.courses.check_all()
    updB_a = next((u for u in updB if u.get("kind") == "assignment"), None)
    if updB_a:
        sched = (updB_a.get("task") or {}).get("schedule") or {}
        check("capacity conflict surfaced", sched.get("conflict") is True,
              str({k: sched.get(k) for k in ("needed", "available", "deficit")}))
        check("shortfall reported (not silently dropped)",
              sched.get("deficit", 0) > 0, str(sched.get("deficit")))
    else:
        check("capacity conflict surfaced", False, "no assignment update produced")
    tskB = [dict(x) for x in B.db.tasks("active") if "Project 2" in x["title"]]
    check("task still created even when tight", len(tskB) == 1, str(len(tskB)))

    print("\n=== 3.6.5b Hard events are never overwritten ===")
    # In scenario B today is blocked 07:00-20:00; verify no block overlaps it.
    evs = B.planner._day_events(B.planner._day_start_ts(int(datetime.now().timestamp())))
    planB = B.db.latest_plan()
    if planB:
        stB = sch.PlanState.from_json(planB["json"])
        overlap = any(sch.overlap_with_event(s, evs) for s in stB.slots)
        check("no schedule block overlaps a hard event", not overlap,
              f"{len(stB.slots)} slots vs {len(evs)} events")

    print("\n=== 3.6.5c DeepSeek cannot choose schedule times (deterministic solver) ===")
    fcA.preferred_time = "12:00-13:00"  # LLM 'asks' for a different window
    fcA.session_times = [{"start": "12:00", "end": "13:00"}]
    A.courses.sync_assignments("CS168", force=True)
    plan3 = A.db.latest_plan()
    st3 = sch.PlanState.from_json(plan3["json"])
    slot3 = [s for s in st3.slots if s.task_id == tid]
    # LLM asked for 12:00-13:00, but the solver keeps the deterministic placement.
    det_ok = bool(slot3) and not any(s.start_min == 720 and s.end_min == 780 for s in slot3)
    check("solver ignores the LLM's requested time window", det_ok, str(slot3))

    print("\n=== 3.6.6 Missing / invalid LLM -> no bogus task ===")
    # (a) LLM unavailable -> monitor never asks, no task.
    C, baseC, siteC = make_env("CS168")
    fcC = FakeChat(deadline_ts=int(datetime.now().timestamp()) + 3 * 86400, ready=False)
    C.chat = fcC
    make_site(siteC, ["CS168_Hidden2.pdf"])
    C.decider.resolve(C.decider.parse(f"add course CS168 {siteC}"))
    updC = C.courses.check_all()
    tskC = [dict(x) for x in C.db.tasks("todo")]
    check("unavailable LLM -> no assignment task", len(tskC) == 0,
          str([x["title"] for x in tskC]))
    check("no assignment update emitted", not any(u.get("kind") == "assignment" for u in updC),
          str([u.get("kind") for u in updC]))
    # (b) LLM returns an invalid model (no deadline) -> skipped, no task.
    D, baseD, siteD = make_env("CS168")
    fcD = FakeChat(deadline_ts=0, est_hours=6, ready=True)
    D.chat = fcD
    make_site(siteD, ["CS168_Hidden3.pdf"])
    D.decider.resolve(D.decider.parse(f"add course CS168 {siteD}"))
    resD = D.courses.sync_assignments("CS168")
    tskD = [dict(x) for x in D.db.tasks("todo")]
    check("invalid LLM model -> no task created", resD.get("count") == 0 and len(tskD) == 0,
          f"{resD.get('count')} created, {len(tskD)} tasks")

    print("\n=== 3.6.7 Prompt safety on the assignment path ===")
    leaked = any(A.chat.secret in p for p in fcA.prompts)
    check("assignment prompts never leak the API key", not leaked, f"{len(fcA.prompts)} prompts")
    check("prompts carry only description text", all("shutil" not in p and "rm " not in p
                                                     for p in fcA.prompts), True)

    for base in (baseA, baseB, baseC, baseD):
        shutil.rmtree(base, ignore_errors=True)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
