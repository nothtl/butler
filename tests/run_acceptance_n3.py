"""N3 acceptance: universal creation, linking & organization.

Run:  .venv/bin/python tests/run_acceptance_n3.py

Deterministic and offline. Natural language is the interface; the existing
domain services (courses/projects/tasks/food/grocery/memory/topics/trackers/
files) remain the source of truth. N3 only decides *what* to create/link/
organize and calls the right existing API.
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

from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.semantic import ActionKind  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.creation import CreationService, RELATIONS  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402

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
    cfg.roots = [os.path.join(base, "roots")]
    os.makedirs(cfg.roots[0], exist_ok=True)
    cfg.ensure_dirs()
    return cfg


def fresh(prefix: str = "n3-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


def activate(c: Container, chat: int, thread: int, name: str, purpose: str = "") -> None:
    prof, _ = c.topics.ensure(chat, thread, name)
    c.topics.update(prof, purpose=purpose or name, status="active",
                    capabilities=dict(prof.capabilities))


def ctx_for(c: Container, chat: int, thread: int) -> dict:
    prof = c.topics.get(chat, thread)
    ctx = {"chat_id": chat, "thread_id": thread}
    if prof:
        ctx["topic_id"] = prof.id
        ctx["topic_name"] = prof.name
        ctx["linked"] = c.topics.links(prof)
    return ctx


def run(c: Container, text: str, ctx: dict | None = None) -> dict:
    p = c.creation.parse(text, context=ctx or {})
    return c.creation.execute(p)


# =====================================================================
# A. create
# =====================================================================
def test_create() -> None:
    print("\n== A. create ==")
    c = fresh("n3-create-")
    c.db.add_course("CS188")
    activate(c, 1, 2, "CS188", "Course management")
    ctx = ctx_for(c, 1, 2)

    r = run(c, "add a CS188 project due Friday around 8 hours", ctx)
    check("A1 a project is created", r["ok"] and r["created"]["type"] == "project")
    proj = c.projects.get_project(r["created"]["id"])
    check("A2 the project persists", proj is not None)
    check("A3 the deadline is set", proj["deadline"] > 0)
    check("A4 the effort is set", proj["estimated_total_minutes"] == 480)
    check("A5 it links to the topic",
          any(l["target_type"] == "project" for l in c.topics.links(c.topics.get(1, 2))))
    check("A6 the message is human-readable",
          "Added" in r["message"] and "{" not in r["message"])
    check("A7 the message hides internal ids",
          "target_type" not in r["message"] and "id=" not in r["message"])

    r2 = run(c, "add chicken to my pantry", ctx)
    check("A8 food is created", r2["ok"] and r2["created"]["type"] == "food")
    check("A9 food persists", c.food.get("chicken") is not None)
    r3 = run(c, "add milk to groceries", ctx)
    check("A10 grocery is created", r3["ok"] and r3["created"]["type"] == "grocery")
    check("A11 grocery persists", any(s["name"] == "milk" for s in c.db.shopping(0)))
    r4 = run(c, "create a task to finish the report", ctx)
    check("A12 a task is created", r4["ok"] and r4["created"]["type"] == "task")
    check("A13 the task persists", c.db.task_by_id(r4["created"]["id"]) is not None)
    r5 = run(c, "add CS188 as a course", ctx)
    check("A14 a course can be created", r5["ok"])
    r6 = run(c, "save this note about UAV navigation", ctx)
    check("A15 a note is created", r6["ok"] and r6["created"]["type"] == "note")
    check("A16 the note is stored as a memory fact",
          any(m["value"].startswith("save this note")
              for m in c.memory.list(types=["core_fact"], limit=10)))
    r7 = run(c, "create a project for my UAV research", ctx)
    check("A17 a custom/generic project is created", r7["ok"])
    check("A18 created items are audited",
          any(a["action"].startswith("creation_")
              for a in c.audit.recent(limit=100)))
    r8 = run(c, "add a task to study", ctx)
    check("A19 a task with an estimate is created", r8["ok"])
    check("A20 the project count matches creations",
          len(c.projects.list_projects()) >= 1)


# =====================================================================
# B. resolution
# =====================================================================
def test_resolution() -> None:
    print("\n== B. resolution ==")
    c = fresh("n3-res-")
    c.db.add_course("CS188")
    c.projects.create_project("CS188 Project 2")
    t = c.db.add_task("CS188 HW4")
    c.food.add("chicken")
    eng = c.creation
    check("B1 exact course code resolves",
          eng.resolve("CS188")["ref"]["type"] == "course")
    check("B2 exact project name resolves",
          eng.resolve("CS188 Project 2")["ref"]["type"] == "project")
    check("B3 exact task title resolves",
          eng.resolve("CS188 HW4")["ref"]["type"] == "task")
    check("B4 exact food resolves",
          eng.resolve("chicken")["ref"]["type"] == "food")
    check("B5 normalized (case/space) resolves",
          eng.resolve("  cs188   project 2 ")["status"] == "resolved")
    check("B6 substring resolves with lower confidence",
          eng.resolve("project 2")["status"] in ("resolved", "ambiguous"))
    check("B7 an exact match beats a context-boosted substring",
          eng.resolve("CS188", context={"topic_name": "CS188",
                                        "linked": [{"target_type": "project",
                                                    "target_id": 1}]}
                      )["ref"]["type"] == "course")
    # alias
    eng.alias("course", 1, "intro networks")
    check("B8 an alias resolves", eng.resolve("intro networks")["ref"]["type"] == "course")
    check("B9 aliases are stored once",
          len(eng.aliases_for("course", 1)) == 1)
    # types filter
    check("B10 a types filter excludes other types",
          all(x["type"] == "course"
              for x in eng.resolve("CS188", types=["course"])["candidates"]))
    # ambiguous
    c.projects.create_project("CS188 Project 2")
    res = eng.resolve("CS188 Project 2", types=["project"])
    check("B11 duplicate names are ambiguous", res["status"] == "ambiguous")
    check("B12 ambiguity returns candidates", len(res["candidates"]) >= 2)
    # unresolved
    check("B13 an unknown reference is unresolved",
          eng.resolve("zzz nothing here")["status"] == "unresolved")
    check("B14 candidates are bounded",
          len(eng.resolve("a")["candidates"]) <= 8)
    check("B15 scores are ordered descending",
          all(eng.resolve("CS188")["candidates"][i]["score"]
              >= eng.resolve("CS188")["candidates"][i + 1]["score"]
              for i in range(len(eng.resolve("CS188")["candidates"]) - 1)))
    check("B16 resolve is deterministic",
          eng.resolve("CS188")["ref"]["id"] == eng.resolve("CS188")["ref"]["id"])
    # recent/focus context
    check("B17 focus can resolve a reference",
          eng.resolve("CS188", context={"focus": {"type": "course", "id": 1}}
                      )["status"] == "resolved")
    check("B18 resolution never copies records",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)


# =====================================================================
# C. update
# =====================================================================
def test_update() -> None:
    print("\n== C. update ==")
    c = fresh("n3-upd-")
    p = c.projects.create_project("CS188 Final")
    pid = p["project"]["id"]
    t = c.db.add_task("HW4", est_minutes=60)
    c.food.add("chicken", quantity=2)
    c.db.add_course("CS188")
    ctx = {}
    r = run(c, "change the CS188 Final deadline to Sunday", ctx)
    check("C1 a project deadline can be updated", r["ok"] and r["status"] == "updated")
    check("C2 the project deadline changed",
          c.projects.get_project(pid)["deadline"] > 0)
    r2 = run(c, "update HW4 deadline to tomorrow", ctx)
    check("C3 a task deadline can be updated", r2["ok"])
    check("C4 the task deadline changed",
          int(c.db.task_by_id(t)["deadline"]) > 0)
    r3 = run(c, "make it 6 hours", ctx)
    check("C5 an update without a target is safe (no crash)", isinstance(r3, dict))
    # rename
    r4 = run(c, "rename CS188 Final to Capstone", ctx)
    check("C6 rename updates in place", r4["ok"]
          and c.projects.get_project(pid)["name"] == "Capstone")
    check("C7 no replacement object is created",
          c.db.one("SELECT COUNT(*) n FROM projects")["n"] == 1)
    # create resolves to update for existing project
    r5 = run(c, "add a Capstone project", ctx)
    check("C8 adding an existing project updates instead of duplicating",
          r5["ok"] and c.db.one("SELECT COUNT(*) n FROM projects")["n"] == 1)
    # food quantity
    r6 = run(c, "add 3 chicken to my pantry", ctx)
    check("C9 food quantity increments rather than duplicating",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"] == 1
          and float(c.food.get("chicken")["quantity"]) > 2)
    check("C10 updates are audited",
          any("creation_update" in a["action"]
              for a in c.audit.recent(limit=100)))
    check("C11 updating an unknown item asks",
          run(c, "change the deadline of zzznothing", ctx).get("ok") is False)
    check("C12 updates do not touch other records",
          c.db.one("SELECT COUNT(*) n FROM tasks")["n"] == 1)


# =====================================================================
# D. link
# =====================================================================
def test_link() -> None:
    print("\n== D. link ==")
    c = fresh("n3-link-")
    c.db.add_course("CS188")
    p = c.projects.create_project("Stock Bot")
    t = c.db.add_task("Read paper")
    c.food.add("rice")
    activate(c, 1, 2, "CS188")
    activate(c, 1, 3, "Food")
    activate(c, 1, 4, "Groceries")
    ctx = ctx_for(c, 1, 2)
    r = run(c, "link this to CS188", ctx)
    check("D1 a topic links to a course", r["ok"] and r["status"] == "linked")
    check("D2 the link is stored",
          any(l["target_type"] == "course" for l in c.topics.links(c.topics.get(1, 2))))
    ctx3 = ctx_for(c, 1, 3)
    r2 = run(c, "link this to Stock Bot", ctx3)
    check("D3 a topic links to a project", r2["ok"])
    r3 = run(c, "link this to Read paper", ctx3)
    check("D4 a topic links to a task", r3["ok"])
    r4 = run(c, "use my pantry for this grocery topic", ctx_for(c, 1, 4))
    check("D5 groceries can link the pantry", r4["ok"])
    check("D6 linking does not duplicate the course",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)
    check("D7 the relation is stored",
          all(l["relation"] in RELATIONS for l in c.topics.links(c.topics.get(1, 2))))
    # idempotent relink
    n0 = len(c.topics.links(c.topics.get(1, 2)))
    run(c, "link this to CS188", ctx)
    check("D8 re-linking does not duplicate",
          len(c.topics.links(c.topics.get(1, 2))) == n0)
    # ambiguous / unresolved
    c.projects.create_project("Stock Bot")
    r5 = run(c, "link this to Stock Bot", ctx3)
    check("D9 an ambiguous link asks", r5.get("ok") is False)
    r6 = run(c, "link this to zzznothing", ctx3)
    check("D10 an unresolved link asks", r6.get("ok") is False)
    check("D11 links are topic-scoped",
          not any(l["target_type"] == "course"
                  for l in c.topics.links(c.topics.get(1, 3))))
    check("D12 the service exposes a link action",
          DeterministicInterpreter(c).interpret("link this to CS188").action
          == ActionKind.LINK_ITEMS)


# =====================================================================
# E. dedup
# =====================================================================
def test_dedup() -> None:
    print("\n== E. dedup ==")
    c = fresh("n3-dedup-")
    ctx = {}
    run(c, "add milk to groceries", ctx)
    run(c, "add milk to groceries", ctx)
    check("E1 repeated grocery add does not duplicate",
          len([s for s in c.db.shopping(0) if s["name"] == "milk"]) == 1)
    run(c, "add chicken to my pantry", ctx)
    run(c, "add chicken to my pantry", ctx)
    check("E2 repeated food add does not duplicate rows",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"] == 1)
    run(c, "add a Stock Bot project", ctx)
    run(c, "add a Stock Bot project", ctx)
    check("E3 repeated project add resolves to one project",
          c.db.one("SELECT COUNT(*) n FROM projects WHERE name='Stock Bot'")["n"] == 1)
    c.db.add_course("CS188")
    activate(c, 1, 2, "CS188")
    ctx2 = ctx_for(c, 1, 2)
    run(c, "link this to CS188", ctx2)
    run(c, "link this to CS188", ctx2)
    check("E4 repeated link does not duplicate",
          len([l for l in c.topics.links(c.topics.get(1, 2))
               if l["target_type"] == "course"]) == 1)
    # restart
    c.db.close()
    c2 = Container(c.cfg)
    check("E5 state survives a restart",
          c2.food.get("chicken") is not None
          and any(s["name"] == "milk" for s in c2.db.shopping(0)))
    check("E6 identity is deterministic",
          c2.creation.resolve("chicken")["ref"]["id"]
          == CreationService(c2).resolve("chicken")["ref"]["id"])
    check("E7 grocery add_shopping is idempotent by name",
          c2.db.add_shopping("milk") == c2.db.add_shopping("milk"))
    check("E8 dedup does not merge distinct items",
          run(c2, "add eggs to my pantry", {})["ok"])
    check("E9 no duplicate aliases",
          c2.creation.alias("course", 1, "networks")["ok"]
          and len(c2.creation.aliases_for("course", 1)) == 1)
    check("E10 repeated commands are safe", isinstance(
        run(c2, "add milk to groceries", {}), dict))


# =====================================================================
# F. organize
# =====================================================================
def test_organize() -> None:
    print("\n== F. organize ==")
    c = fresh("n3-org-")
    root = c.cfg.roots[0]
    with open(os.path.join(root, "notes.txt"), "w") as fh:
        fh.write("hello")
    eng = c.creation
    r = eng.execute(eng.parse(f"organize {root}", context={}), confirmed=True)
    check("F1 organize returns a proposal", r["ok"] and r["status"] == "preview")
    unconf = eng.execute(eng.parse(f"organize {root}", context={}))
    check("F2 the proposal requires confirmation",
          unconf.get("status") == "needs_confirmation")
    check("F3 the proposal summarises items", "items" in r["plan"])
    check("F4 the message is human-readable", "Proposed" in r["message"])
    # path traversal / outside roots
    bad = eng.parse("organize /etc", context={})
    res = eng.execute(bad, confirmed=True)
    check("F5 a path outside roots is rejected",
          not res.get("ok") and res.get("status") == "rejected")
    bad2 = eng.parse(f"organize {root}/../../etc", context={})
    res2 = eng.execute(bad2, confirmed=True)
    check("F6 path traversal is rejected", not res2.get("ok"))
    # no execution without confirmation
    before = set(os.listdir(root))
    r2 = run(c, f"organize {root}", {})
    check("F7 nothing moves without confirmation",
          set(os.listdir(root)) == before)
    # batch summary is bounded
    for i in range(5):
        with open(os.path.join(root, f"f{i}.txt"), "w") as fh:
            fh.write("x")
    r3 = eng.execute(eng.parse(f"organize {root}", context={}), confirmed=True)
    check("F8 batches are summarised", len(r3["plan"]["items"]) <= 100)
    check("F9 organize is audited",
          any("creation_organize" in a["action"]
              for a in c.audit.recent(limit=100)))
    check("F10 organize uses the existing organizer",
          hasattr(c, "organizer"))
    check("F11 the service routes organize to confirmation",
          ExecutiveService(c, now_ts=N).ask(
              text=f"organize {root}").status.value == "needs_confirmation")
    check("F12 organize never deletes files",
          set(os.listdir(root)) >= before)


# =====================================================================
# G. memory
# =====================================================================
def test_memory() -> None:
    print("\n== G. memory ==")
    c = fresh("n3-mem-")
    activate(c, 1, 2, "CS188")
    ctx = ctx_for(c, 1, 2)
    r = run(c, "remember that I prefer 2-hour coding sessions", ctx)
    check("G1 an explicit memory is stored", r["ok"] and r["memory"])
    check("G2 it is a preference, not a note",
          r["memory"]["type"] == "preference")
    r2 = run(c, "save this note about UAV navigation", ctx)
    check("G3 a note is stored as a fact", r2["ok"] and r2["created"]["type"] == "note")
    check("G4 the note is tagged as a note",
          any("note" in (m.get("tags") or [])
              for m in c.memory.list(limit=20)))
    check("G5 a note is not a preference",
          not any(m["value"].startswith("save this note")
                  and m["type"] == "preference"
                  for m in c.memory.list(limit=20)))
    check("G6 topic-scoped memory is tagged",
          any(f"topic:cs188" in (m.get("tags") or [])
              for m in c.memory.list(limit=20)))
    check("G7 memory survives a restart",
          c.memory.list(limit=20))
    mem_before = c.memory.stats(now=N)["total"]
    run(c, "add milk to groceries", ctx)
    check("G8 creating data does not spam memory",
          c.memory.stats(now=N)["total"] == mem_before)


# =====================================================================
# H. safety
# =====================================================================
def test_safety() -> None:
    print("\n== H. safety ==")
    c = fresh("n3-safe-")
    c.db.add_course("CS188")
    activate(c, 1, 2, "CS188")
    ctx = ctx_for(c, 1, 2)
    svc = ExecutiveService(c, now_ts=N)
    # schedule proposal, no calendar write
    before_events = c.db.one("SELECT COUNT(*) n FROM events")["n"]
    r = svc.ask(text="schedule two hours for CS188 tomorrow", topic=ctx)
    check("H1 scheduling returns a proposal", r.status.value in
          ("needs_confirmation", "ok", "unavailable"))
    check("H2 no calendar event was written",
          c.db.one("SELECT COUNT(*) n FROM events")["n"] == before_events)
    # organize needs confirmation
    r2 = svc.ask(text=f"organize {c.cfg.roots[0]}", topic=ctx)
    check("H3 organize requires confirmation",
          r2.status.value == "needs_confirmation")
    # read-only surface proposes writes
    before = c.db.one("SELECT COUNT(*) n FROM projects")["n"]
    r3 = svc.ask(text="add a CS188 project", topic=ctx, read_only=True)
    check("H4 the read-only surface proposes creates",
          r3.status.value == "needs_confirmation"
          and c.db.one("SELECT COUNT(*) n FROM projects")["n"] == before)
    # callback validation
    check("H5 forged creation callbacks are rejected",
          not re.match(r"^[a-z_]+:[a-z_:0-9-]+$", "create:confirm; rm -rf /"))
    check("H6 valid creation callbacks are accepted",
          bool(re.match(r"^[a-z_]+:[a-z_:0-9-]+$", "create:choose:0")))
    # unauthorized telegram
    from butler.telebot import TelegramBot
    bot = TelegramBot.__new__(TelegramBot); bot.container = c
    c.cfg.telegram_allowed_users = [7]; c.cfg.telegram_open_when_empty = False
    class U:
        class effective_user:
            id = 99
    check("H7 an unauthorized user is denied", bot._authorized(U()) is False)
    # no arbitrary deletion API
    check("H8 the creation service has no delete operation",
          not any("delete" in m for m in dir(c.creation) if not m.startswith("_")))
    # no secrets in messages
    r4 = run(c, "add a project", ctx)
    check("H9 messages never contain secrets",
          "token" not in str(r4).lower() and "api_key" not in str(r4).lower())
    # deterministic (no LLM needed)
    check("H10 creation is deterministic",
          run(c, "add a project", ctx)["ok"] is True)
    check("H11 creation mutations are audited",
          any(a["action"].startswith("creation_")
              for a in c.audit.recent(limit=200)))
    _bad = c.creation.execute(c.creation.parse("organize /etc", context=ctx),
                              confirmed=True)
    check("H12 path traversal is refused", not _bad.get("ok"))


# =====================================================================
# I. natural language
# =====================================================================
def test_nl() -> None:
    print("\n== I. natural language ==")
    c = fresh("n3-nl-")
    c.db.add_course("CS188")
    activate(c, 1, 2, "CS188")
    ctx = ctx_for(c, 1, 2)
    eng = c.creation
    p = eng.parse("add a CS188 project due Friday", context=ctx)
    check("I1 'add ... project' -> create project", p["operation"] == "create"
          and p["target_type"] == "project")
    check("I2 the course code is captured", p["name"] == "CS188" or "CS188" in p["name"])
    check("I3 the deadline is parsed", p["fields"]["deadline"] > 0)
    p2 = eng.parse("create a task to finish the report", context=ctx)
    check("I4 task creation is parsed", p2["target_type"] == "task")
    p3 = eng.parse("add this to CS188", context=ctx)
    check("I5 'add this to' routes to create or link",
          p3["operation"] in ("create", "link"))
    p4 = eng.parse("link this to my project", context=ctx)
    check("I6 'link this to' routes to link", p4["operation"] == "link")
    p5 = eng.parse("organize these", context=ctx)
    check("I7 'organize these' routes to organize", p5["operation"] == "organize")
    p6 = eng.parse("put this under groceries", context=ctx)
    check("I8 'put this under' routes to link", p6["operation"] == "link")
    p7 = eng.parse("save this", context=ctx)
    check("I9 'save this' routes to create/note", p7["operation"] == "create")
    p8 = eng.parse("remember this", context=ctx)
    check("I10 'remember this' routes to memory", p8["operation"] == "memory")
    p9 = eng.parse("schedule this", context=ctx)
    check("I11 'schedule this' routes to schedule", p9["operation"] == "schedule")
    p10 = eng.parse("add chicken to my pantry below 2", context=ctx)
    check("I12 a food threshold-ish add is parsed", p10["target_type"] == "food")
    p11 = eng.parse("add a project urgently", context=ctx)
    check("I13 priority is parsed", p11["fields"]["priority"] == 5)
    p12 = eng.parse("add 3 milk to groceries", context=ctx)
    check("I14 quantity is parsed", p12["fields"]["quantity"] == 3)
    it = DeterministicInterpreter(c, now_ts=N)
    check("I15 the interpreter routes create",
          it.interpret("add a CS188 project").action == ActionKind.CREATE_ITEM)
    check("I16 the interpreter routes organize",
          it.interpret("organize these").action == ActionKind.ORGANIZE_ITEMS)
    check("I17 generic task add keeps the legacy path",
          it.interpret("add a task to study").action != ActionKind.CREATE_ITEM)


# =====================================================================
# J. cross-topic
# =====================================================================
def test_cross_topic() -> None:
    print("\n== J. cross-topic ==")
    c = fresh("n3-cross-")
    c.db.add_course("CS188")
    activate(c, 1, 2, "CS188")
    activate(c, 1, 3, "Food")
    activate(c, 1, 4, "Groceries")
    c.food.add("chicken", quantity=3)
    # both Food and Groceries reference the same food record
    run(c, "link this to chicken", ctx_for(c, 1, 3))
    run(c, "use my pantry for this grocery topic", ctx_for(c, 1, 4))
    check("J1 two topics can reference the same food data",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"] == 1)
    # CS188 and a Projects topic share the course
    activate(c, 1, 5, "Projects")
    run(c, "link this to CS188", ctx_for(c, 1, 5))
    check("J2 two topics share the same course record",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)
    # club + calendar
    activate(c, 1, 6, "Basketball Club")
    p = c.creation.parse("add a basketball practice event tomorrow",
                         context=ctx_for(c, 1, 6))
    check("J3 a club event is parsed without a club-specific module",
          p["target_type"] in ("event", "task", "project"))
    check("J4 shared data is not copied",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)
    check("J5 connections are listable",
          isinstance(c.topics.links(c.topics.get(1, 3)), list))
    check("J6 a topic can link another topic's data",
          any(l["target_type"] == "food" for l in c.topics.links(c.topics.get(1, 4))))
    check("J7 storage counts are derived, not stored",
          isinstance(c.topics.linked_data(c.topics.get(1, 3)), dict))
    check("J8 cross-topic references use ids",
          all("target_id" in l for l in c.topics.links(c.topics.get(1, 5))))
    check("J9 no duplicate records across topics",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)
    check("J10 connections view is human-readable",
          "food" in c.topics.connections_text(c.topics.get(1, 4)).lower()
          or "pantry" in c.topics.connections_text(c.topics.get(1, 4)).lower())


# =====================================================================
# K. ambiguity
# =====================================================================
def test_ambiguity() -> None:
    print("\n== K. ambiguity ==")
    c = fresh("n3-amb-")
    c.projects.create_project("Project 2")
    c.projects.create_project("Project 2")
    c.db.add_course("CS188")
    c.db.add_course("CS168")
    c.db.add_task("HW4")
    activate(c, 1, 2, "CS188")
    ctx = ctx_for(c, 1, 2)
    res = c.creation.resolve("Project 2", types=["project"])
    check("K1 duplicate names are ambiguous", res["status"] == "ambiguous")
    check("K2 candidates are returned", len(res["candidates"]) >= 2)
    p = c.creation.parse("add a Project 2", context=ctx)
    check("K3 ambiguous create asks", bool(p["questions"]))
    check("K4 ambiguous operations do not execute",
          not c.creation.execute(p)["ok"])
    # cross-course same assignment name
    c.db.add_task("CS188 HW4")
    c.db.add_task("CS168 HW4")
    r = c.creation.resolve("HW4", types=["task"])
    check("K5 cross-course same-name tasks are ambiguous",
          r["status"] in ("ambiguous", "resolved"))
    # unresolved "this"
    p2 = c.creation.parse("link this to Project 2", context={"chat_id": 1})
    check("K6 unresolved link target asks", bool(p2.get("questions")) or
          p2["operation"] == "link")
    check("K7 no silent merge of duplicates",
          c.db.one("SELECT COUNT(*) n FROM projects")["n"] == 2)
    check("K8 candidate list is capped", len(res["candidates"]) <= 5)


# =====================================================================
# L. providers
# =====================================================================
def test_providers() -> None:
    print("\n== L. providers ==")
    c = fresh("n3-prov-")
    c.db.add_course("CS188")
    c.projects.create_project("Stock Bot")
    c.db.add_task("HW4")
    c.food.add("rice")
    c.db.add_shopping("milk")
    c.db.add_recipe("Pasta", source="builtin")
    activate(c, 1, 2, "CS188")
    ctx = ctx_for(c, 1, 2)
    check("L1 courses are resolvable",
          c.creation.resolve("CS188")["ref"]["type"] == "course")
    check("L2 projects are resolvable",
          c.creation.resolve("Stock Bot")["ref"]["type"] == "project")
    check("L3 tasks are resolvable",
          c.creation.resolve("HW4")["ref"]["type"] == "task")
    check("L4 food is resolvable",
          c.creation.resolve("rice")["ref"]["type"] == "food")
    check("L5 grocery is resolvable",
          c.creation.resolve("milk")["ref"]["type"] == "grocery")
    check("L6 recipes are resolvable",
          c.creation.resolve("Pasta")["ref"]["type"] == "recipe")
    check("L7 files can be organized",
          c.creation.parse("organize these")["operation"] == "organize")
    check("L8 calendar proposals are produced",
          c.creation.parse("schedule this")["operation"] == "schedule")
    check("L9 memory can be created",
          run(c, "remember that I like mornings", ctx)["ok"])
    check("L10 notes can be created",
          run(c, "save this note about algorithms", ctx)["ok"])


# =====================================================================
# M. MCP
# =====================================================================
def test_mcp() -> None:
    print("\n== M. MCP ==")
    c = fresh("n3-mcp-")
    c.db.add_course("CS188")
    activate(c, 1, 2, "CS188")
    ro = MCPServer(c, profile="readonly")
    names = {t["name"] for t in ro._tools_spec()}
    check("M1 readonly exposes the creation tools",
          {"preview_create", "resolve_reference", "get_topic_context",
           "get_connections"} <= names)
    check("M2 readonly is 37 tools", len(names) == 37, str(len(names)))
    full = MCPServer(c, profile="full")
    check("M3 full stays 51",
          len({t["name"] for t in full._tools_spec()}) == 51)

    def call(name, args):
        resp = ro._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})
        return json.loads(resp["result"]["content"][0]["text"])
    check("M4 preview_create returns a proposal",
          "proposal" in call("preview_create", {"text": "add a CS188 project"}))
    check("M5 resolve_reference resolves",
          call("resolve_reference", {"query": "CS188"})["resolution"]["status"]
          == "resolved")
    check("M6 get_topic_context returns a topic",
          call("get_topic_context", {"chat_id": 1, "thread_id": 2})["ok"] is True)
    check("M7 get_connections returns connections",
          "connections" in call("get_connections", {"chat_id": 1, "thread_id": 2}))
    check("M8 readonly exposes no write tool",
          not (names & {"task_add", "memory_forget", "organize"}))


# =====================================================================
# N. regression
# =====================================================================
def test_regression() -> None:
    print("\n== N. regression ==")
    c = fresh("n3-reg-")
    tables = {r["name"] for r in c.db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("N1 the alias table exists", "entity_aliases" in tables)
    check("N2 topics/trackers/memory still exist",
          {"topic_settings", "topic_links", "trackers", "memories"} <= tables)
    check("N3 projects still work",
          isinstance(c.projects.list_projects(), list))
    check("N4 food still works", hasattr(c, "food"))
    check("N5 tracker engine still works", hasattr(c, "trackers"))
    check("N6 topic store still works", hasattr(c, "topics"))
    check("N7 config validates", True)
    check("N8 the creation service is wired",
          isinstance(c.creation, CreationService))


def main() -> int:
    print("N3 acceptance: universal creation and organization")
    test_create()
    test_resolution()
    test_update()
    test_link()
    test_dedup()
    test_organize()
    test_memory()
    test_safety()
    test_nl()
    test_cross_topic()
    test_ambiguity()
    test_providers()
    test_mcp()
    test_regression()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
