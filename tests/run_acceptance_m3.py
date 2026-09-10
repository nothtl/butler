"""M3 acceptance: project intelligence (Goal → Project → Milestone → Task).

Run:  .venv/bin/python tests/run_acceptance_m3.py

Proves the M3 project layer end to end:
  1. A realistic pre-M3 database migrates in place: tasks gain the new
     columns, the new tables/index appear, and existing rows are untouched.
  2. Projects/milestones/tasks link and read back through the domain API.
  3. Progress is effort-based (never completed-task counts) and is reported as
     ``unknown`` when there is no estimate.
  4. Workload exposes remaining effort, blocked tasks and next tasks.
  5. Risk is a deterministic, explainable weighted sum of named factors.
  6. Dependencies form a validated DAG: self/unknown/cycle edges are rejected,
     inferred edges stay advisory, and blocked tasks are reported.
  7. ``propose_project`` is a deterministic proposal (no write) that keeps
     inferred provenance and asks for missing details.
  8. The executive service answers project questions and never persists a
     create request (NEEDS_CONFIRMATION only).
  9. The read-only MCP surface exposes the project reads and the full profile
     stays at its historical 51 tools.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TZ", "UTC")

from butler.agent.mcp_tools import build_mcp_registry  # noqa: E402
from butler.agent.semantic import ActionKind, ResultStatus  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.db import DB  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402

PASS = 0
FAIL = 0
UTC = ZoneInfo("UTC")
NOW = int(datetime(2026, 9, 9, 20, 0, tzinfo=UTC).timestamp())  # Wed 20:00


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


def fresh_container(prefix: str = "m3-") -> Container:
    cfg = base_config(prefix)
    c = Container(cfg)
    c.agent.store = SessionStore()
    return c


# =====================================================================
# 1. Migration from a realistic pre-M3 database
# =====================================================================
def test_migration() -> None:
    print("\n== pre-M3 database migration ==")
    cfg = base_config("m3-migrate-")
    path = cfg.db_path()
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE tasks("
        " id INTEGER PRIMARY KEY, title TEXT, detail TEXT, deadline INTEGER,"
        " priority INTEGER, est_minutes INTEGER, status TEXT DEFAULT 'todo',"
        " sort INTEGER DEFAULT 0, created INTEGER, completed INTEGER,"
        " tags TEXT, note TEXT);"
        "CREATE INDEX idx_tasks_status ON tasks(status);")
    conn.execute("INSERT INTO tasks(title,est_minutes,status,created,priority) "
                 "VALUES('Legacy essay',90,'todo',?,3)", (NOW,))
    conn.commit()
    conn.close()

    c = Container(cfg)
    cols = {r["name"] for r in c.db.query("PRAGMA table_info(tasks)")}
    check("legacy tasks table gained project_id",
          "project_id" in cols and "milestone_id" in cols
          and "remaining_minutes" in cols, ",".join(sorted(cols & {
              "project_id", "milestone_id", "remaining_minutes"})))
    tables = {r["name"] for r in c.db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("new project tables created",
          {"projects", "milestones", "task_deps"} <= tables)
    idx = {r["name"] for r in c.db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    check("tasks.project_id index created after ALTER",
          "idx_tasks_project" in idx)
    legacy = c.db.tasks("todo")
    check("legacy task survived migration",
          len(legacy) == 1 and legacy[0]["title"] == "Legacy essay"
          and int(legacy[0]["est_minutes"]) == 90)
    # Re-opening must be idempotent (the ALTER/index path runs again).
    try:
        DB(cfg)
        reopened = True
    except Exception as exc:  # noqa: BLE001
        reopened = False
        print("      reopen error:", exc)
    check("migration is idempotent on reopen", reopened)


# =====================================================================
# 2. Projects, milestones and task links
# =====================================================================
def build_project(c: Container) -> dict:
    p = c.projects
    res = p.create_project("CS168 Project", objective="Build a reliable protocol",
                           estimated_total_minutes=720)
    check("create_project succeeds", res.get("ok") is True)
    pid = res["project"]["id"]
    m1 = p.add_milestone(pid, "Protocol", estimated_minutes=300)["milestone"]
    m2 = p.add_milestone(pid, "Testing", estimated_minutes=240)["milestone"]
    m3 = p.add_milestone(pid, "Report", estimated_minutes=180)["milestone"]
    t1 = c.db.add_task("Implement protocol", est_minutes=300, priority=5)
    t2 = c.db.add_task("Write tests", est_minutes=240, priority=4)
    t3 = c.db.add_task("Write report", est_minutes=180, priority=3)
    p.link_task(t1, pid, m1["id"])
    p.link_task(t2, pid, m2["id"])
    p.link_task(t3, pid, m3["id"])
    return {"pid": pid, "t1": t1, "t2": t2, "t3": t3,
            "m1": m1, "m2": m2, "m3": m3}


def test_model() -> None:
    print("\n== project / milestone / task model ==")
    c = fresh_container("m3-model-")
    ids = build_project(c)
    p = c.projects
    got = p.get_project(ids["pid"])
    check("get_project returns the project",
          got is not None and got["name"] == "CS168 Project")
    check("get_project exposes three milestones",
          got is not None and len(got["milestones"]) == 3)
    check("list_projects includes it", len(p.list_projects()) == 1)
    check("milestone effort rolls up",
          sum(m["estimated_minutes"] for m in got["milestones"]) == 720)
    check("project resolves by case-insensitive name",
          p.get_project("cs168 project") is not None)
    bad = p.create_project("Bad", status="nonsense")
    check("invalid project status rejected", bad.get("ok") is False)
    missing = p.link_task(ids["t1"], 99999)
    check("linking to an unknown project rejected",
          missing.get("ok") is False)


# =====================================================================
# 3. Effort-based progress
# =====================================================================
def test_progress() -> None:
    print("\n== effort-based progress ==")
    c = fresh_container("m3-progress-")
    ids = build_project(c)
    wl = c.projects.workload(ids["pid"])
    check("initial progress is 0.0 (derived)",
          wl["progress"] == 0.0 and wl["progress_source"] == "derived")
    check("initial remaining equals total effort",
          wl["remaining_minutes"] == 720 and wl["estimated_minutes"] == 720)

    c.db.update_task(ids["t1"], status="completed")
    wl = c.projects.workload(ids["pid"])
    check("progress is completed/estimated minutes",
          abs(wl["progress"] - 300 / 720) < 1e-6, str(wl["progress"]))
    check("progress is not a completed-task count ratio",
          abs(wl["progress"] - 1 / 3) > 1e-6, str(wl["progress"]))
    check("remaining drops to 420", wl["remaining_minutes"] == 420)
    check("completed count is 1", wl["completed_count"] == 1)

    c.db.update_task(ids["t2"], status="skipped")
    wl = c.projects.workload(ids["pid"])
    check("skipped tasks leave the denominator",
          wl["estimated_minutes"] == 480 and wl["remaining_minutes"] == 180)

    c.db.update_task(ids["t3"], status="cancelled")
    wl = c.projects.workload(ids["pid"])
    check("cancelled tasks are excluded too",
          wl["estimated_minutes"] == 300 and wl["remaining_minutes"] == 0)

    # No estimates anywhere -> progress unknown, not a misleading number.
    c2 = fresh_container("m3-unknown-")
    pid = c2.projects.create_project("Vague")["project"]["id"]
    t = c2.db.add_task("Do something", est_minutes=0)
    c2.projects.link_task(t, pid)
    wl2 = c2.projects.workload(pid)
    check("no estimates -> progress unknown",
          wl2["progress"] is None and wl2["progress_source"] == "unknown")
    check("unknown estimate reports zero remaining",
          wl2["remaining_minutes"] == 0)


# =====================================================================
# 4. Workload + next tasks
# =====================================================================
def test_workload() -> None:
    print("\n== workload ==")
    c = fresh_container("m3-workload-")
    ids = build_project(c)
    c.projects.add_dependency(ids["t2"], ids["t1"])
    wl = c.projects.workload(ids["pid"])
    check("workload reports blocked task", wl["blocked_task_ids"] == [ids["t2"]])
    check("workload offers next tasks", len(wl["next_tasks"]) == 3)
    check("highest-priority unblocked task is first",
          wl["next_tasks"][0]["task_id"] == ids["t1"]
          and wl["next_tasks"][0]["blocked"] is False)
    check("blocked task is flagged",
          any(t["task_id"] == ids["t2"] and t["blocked"] for t in wl["next_tasks"]))
    check("active_count counts unfinished tasks", wl["active_count"] == 3)
    check("workload for unknown project is None",
          c.projects.workload("does-not-exist") is None)


# =====================================================================
# 5. Deterministic, explainable risk
# =====================================================================
def test_risk() -> None:
    print("\n== risk ==")
    c = fresh_container("m3-risk-")
    ids = build_project(c)
    c.projects.update_project(ids["pid"], deadline=NOW + 7 * 86400)
    r = c.projects.risk(ids["pid"], now=NOW)
    check("risk returns named factors", len(r["factors"]) == 5)
    names = {f["name"] for f in r["factors"]}
    check("expected factor names present",
          {"deadline_pressure", "overdue_milestones", "dependency_bottleneck",
           "stalled_progress", "estimate_uncertainty"} <= names)
    check("factor weights sum to 1.0",
          abs(sum(f["weight"] for f in r["factors"]) - 1.0) < 1e-9)
    check("score is bounded", 0.0 <= r["score"] <= 1.0, str(r["score"]))
    check("level is one of low/medium/high",
          r["level"] in ("low", "medium", "high"))
    check("risk declares its deterministic methodology",
          "deterministic" in r["methodology"] and "LLM" not in r["methodology"]
          or "no LLM" in r["methodology"])
    r2 = c.projects.risk(ids["pid"], now=NOW)
    check("risk is deterministic", r == r2)
    worst = c.projects.most_at_risk(now=NOW)
    check("most_at_risk picks the only active project",
          worst is not None and worst["project_id"] == ids["pid"])

    # A project with a deadline far in the past and no progress is riskier.
    old = c.projects.create_project("Overdue", estimated_total_minutes=600,
                                    deadline=NOW - 5 * 86400)["project"]["id"]
    ro = c.projects.risk(old, now=NOW)
    check("overdue project scores higher than unplanned one",
          ro["score"] >= r["score"], f"{ro['score']} >= {r['score']}")


# =====================================================================
# 6. Validated dependency DAG
# =====================================================================
def test_dependencies() -> None:
    print("\n== dependency DAG ==")
    c = fresh_container("m3-deps-")
    ids = build_project(c)
    p = c.projects
    ok = p.add_dependency(ids["t2"], ids["t1"])
    check("valid dependency accepted", ok.get("ok") is True)
    check("self-dependency rejected",
          p.add_dependency(ids["t1"], ids["t1"]).get("ok") is False)
    check("unknown task rejected",
          p.add_dependency(ids["t1"], 99999).get("ok") is False)
    cycle = p.add_dependency(ids["t1"], ids["t2"])
    check("cycle rejected", cycle.get("ok") is False
          and bool(cycle.get("cycles")))
    check("cycle rejection rolled the edge back",
          all(int(d["depends_on"]) != ids["t2"]
              for d in c.db.task_dependencies(ids["t1"])))

    deps = p.dependencies(ids["pid"])
    check("dependencies reports the edge",
          any(e["task_id"] == ids["t2"] and e["depends_on"] == ids["t1"]
              for e in deps["edges"]))
    check("blocked task reported with its prerequisite",
          deps["blocked"] and deps["blocked"][0]["task_id"] == ids["t2"]
          and ids["t1"] in deps["blocked"][0]["waiting_on"])
    check("no cycles in the DAG", deps["cycles"] == [])

    # Inferred edges are advisory: they never mark a task blocked.
    inf = p.add_dependency(ids["t3"], ids["t1"], inferred=True)
    check("inferred dependency accepted", inf.get("ok") is True
          and inf.get("inferred") is True)
    check("inferred edge is advisory (not blocking)",
          ids["t3"] not in p.blocked_task_ids(ids["pid"]))
    deps = p.dependencies(ids["pid"])
    check("inferred edge is marked in the graph",
          any(e["task_id"] == ids["t3"] and e["inferred"] for e in deps["edges"]))

    # Completing the prerequisite unblocks the dependent.
    c.db.update_task(ids["t1"], status="completed")
    check("completing prerequisite unblocks dependent",
          ids["t2"] not in p.blocked_task_ids(ids["pid"]))


# =====================================================================
# 7. Deterministic proposal (no write)
# =====================================================================
def test_proposal() -> None:
    print("\n== project proposal ==")
    c = fresh_container("m3-propose-")
    text = ("I have a CS168 project due next Friday, ~12 hours, protocol "
            "implementation, testing, report")
    prop = c.projects.propose_project(text, now=NOW)
    check("proposal is a project_proposal",
          prop["kind"] == "project_proposal")
    check("proposal parses the name", prop["name"].lower().startswith("cs168"),
          prop["name"])
    check("proposal parses ~12 hours", prop["estimated_total_minutes"] == 720)
    check("proposal parses a future deadline", prop["deadline"] > NOW)
    check("proposal splits three milestones", len(prop["milestones"]) == 3,
          str([m["name"] for m in prop["milestones"]]))
    check("all proposal fields are inferred",
          set(prop["provenance"].values()) == {"inferred"})
    check("proposal requires confirmation",
          prop["requires_confirmation"] is True)
    check("complete proposal has no questions", prop["questions"] == [])
    check("proposal did not write anything",
          c.projects.list_projects() == [])

    vague = c.projects.propose_project("new project", now=NOW)
    check("vague proposal asks for missing details",
          bool(vague["questions"]) and vague["confidence"] < 0.9)
    check("vague proposal still writes nothing",
          c.projects.list_projects() == [])


# =====================================================================
# 8. Executive service integration
# =====================================================================
def test_service() -> None:
    print("\n== executive service ==")
    c = fresh_container("m3-service-")
    ids = build_project(c)
    c.projects.add_dependency(ids["t2"], ids["t1"])
    svc = ExecutiveService(c, now_ts=NOW)

    r = svc.ask(text="how much work is left on my CS168 project")
    check("workload question resolves to ok", r.status == ResultStatus.OK,
          r.status.value)
    check("workload fact has remaining effort",
          r.facts and r.facts[0].get("remaining_minutes") == 720)

    r = svc.ask(text="what is the risk on the CS168 project")
    check("risk question resolves to ok", r.status == ResultStatus.OK)
    check("risk fact names the level",
          r.facts and r.facts[0].get("level") in ("low", "medium", "high"))

    r = svc.ask(text="what are the dependencies for the CS168 project")
    check("dependency question resolves to ok", r.status == ResultStatus.OK)
    check("dependency fact counts the edge",
          r.facts and r.facts[0].get("edges") == 1)

    r = svc.ask(text="what should I work on next for the CS168 project")
    check("next-task question resolves to ok", r.status == ResultStatus.OK)
    check("next-task question recommends a task",
          bool(r.recommendations))

    r = svc.ask(text="list my projects")
    check("project list resolves to ok", r.status == ResultStatus.OK)
    check("project list fact counts projects",
          r.facts and r.facts[0].get("project_count") == 1)

    before = len(c.projects.list_projects())
    r = svc.ask(text="I have a CS168 project due next Friday, ~12 hours, "
                     "protocol implementation, testing, report")
    check("create request is gated",
          r.status == ResultStatus.NEEDS_CONFIRMATION
          and r.confirmation_required is True, r.status.value)
    check("create request carries a proposal",
          bool(r.candidate_actions)
          and r.candidate_actions[0]["action"] == "create_project")
    check("create request did not persist",
          len(c.projects.list_projects()) == before)

    r = svc.ask(request={"intent": "query", "action": "project_workload"})
    check("aggregate workload with no target is ok",
          r.status == ResultStatus.OK, r.status.value)


# =====================================================================
# 9. Read-only MCP surface + backward compatibility
# =====================================================================
def test_mcp() -> None:
    print("\n== read-only MCP surface ==")
    c = fresh_container("m3-mcp-")
    ids = build_project(c)
    ro = MCPServer(c, profile="readonly")
    names = {t["name"] for t in ro._tools_spec()}
    expected = {
        "get_time", "get_context", "get_day", "plan_day", "get_schedule",
        "get_tasks", "get_courses", "get_projects", "get_project",
        "get_project_workload", "get_project_risk", "get_project_dependencies",
        "find_available_time", "get_week", "web_search", "web_research",
        "web_fetch", "knowledge_lookup", "executive_ask",
    }
    check("readonly surface is exactly 19 tools", names == expected,
          str(len(names)))
    check("no project mutating tool is exposed",
          not (names & {"create_project", "add_project", "link_task"}))
    full = MCPServer(c, profile="full")
    full_names = {t["name"] for t in full._tools_spec()}
    check("full profile is unchanged at 51 tools", len(full_names) == 51,
          str(len(full_names)))
    check("full profile still hides the executive surface",
          "get_projects" not in full_names and "executive_ask" not in full_names)

    def call(name: str, args: dict | None = None) -> dict:
        resp = ro._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args or {}}})
        content = resp.get("result", {}).get("content", [{}])
        return json.loads(content[0].get("text", "{}"))

    listing = call("get_projects")
    check("get_projects returns the linked project",
          listing.get("ok") and listing.get("count") == 1)
    one = call("get_project", {"project": "CS168 Project"})
    check("get_project returns the project by name",
          one.get("ok") and one["project"]["name"] == "CS168 Project")
    wl = call("get_project_workload", {"project": ids["pid"]})
    check("get_project_workload returns remaining effort",
          wl.get("ok") and wl["workload"]["remaining_minutes"] == 720)
    rk = call("get_project_risk", {"project": ids["pid"]})
    check("get_project_risk returns factors",
          rk.get("ok") and len(rk["risk"]["factors"]) == 5)
    dep = call("get_project_dependencies", {"project": ids["pid"]})
    check("get_project_dependencies returns nodes",
          dep.get("ok") and isinstance(dep["dependencies"]["nodes"], list))
    miss = call("get_project", {"project": "nope"})
    check("missing project is an error payload", miss.get("ok") is False)

    # Backward compatibility: ordinary tasks/courses still read fine.
    check("ordinary task reads still work",
          any(r["title"] == "Implement protocol" for r in c.db.tasks("active")))
    check("registry knows the project reads but the full server hides them",
          "get_project" in {t.mcp_name for t in build_mcp_registry(c).all()}
          and "get_project" not in full_names)


def main() -> int:
    test_migration()
    test_model()
    test_progress()
    test_workload()
    test_risk()
    test_dependencies()
    test_proposal()
    test_service()
    test_mcp()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
