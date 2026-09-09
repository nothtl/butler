"""Phase 3.5 hardening acceptance tests for Butler.

Run:  .venv/bin/python tests/run_acceptance_p35.py

Covers the Phase 3.5 hardening pass:
  1. Course URL hierarchy join (absolute/root/relative/query/protocol-relative).
  2. NAS routing-base path binding (preserve full hierarchy, never escape).
  3. Recipe estimate / data-quality fields.
  4. Search vs rank split (rating, favourite, prior usage, expiration pressure).
  5. Course -> Assignment -> Task pipeline (LLM proposes, scheduler places).
  6. Two-stage semantic classifier + low-confidence confirmation.
  7. Cross-domain e2e (detect -> download -> store -> index -> understand ->
     workload estimate -> create task -> visible to scheduler).
  8. Safety assertions (no LLM fs/secret, bulk requires confirmation, dedupe).
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
from butler.course import CourseIntelligence  # noqa: E402
from butler.food import Recipe  # noqa: E402
from butler.organizer import Plan, PlanItem  # noqa: E402
from butler.engine import EngineError  # noqa: E402

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
        make_pdf(os.path.join(root, f), f"{f} content for the course.")
    hrefs = "\n".join(f'<a href="{f}">{f}</a>' for f in files)
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write(f"<html><body><h1>CS168</h1>{hrefs}</body></html>")
    return root


class FakeChat:
    """Deterministic LLM double. Routes on the system prompt so the
    assignment-understanding path and the semantic-classifier path are both
    exercised without touching the network. Records every prompt for the
    secrets-safety assertion."""

    def __init__(self, secret: str):
        self.secret = secret
        self.prompts: list[str] = []

    def _llm_ready(self) -> bool:
        return True

    def _llm(self, prompt: tuple[str, str]) -> str:
        system, user = (prompt[0], prompt[1])
        self.prompts.append(system + "\n" + user)
        if "Classify a document" in system:
            return json.dumps({"code": "STAT220", "subfolder": "Projects",
                               "confidence": 0.93})
        if "assignment specification" in system:
            deadline = int(datetime.now().timestamp()) + 3 * 86400
            return json.dumps({"title": "Project 2: Chatbot",
                               "deadline": datetime.fromtimestamp(deadline).isoformat(),
                               "est_hours": 6,
                               "requirements": ["design an agent", "write report"],
                               "dependencies": ["week 3 lecture"],
                               "milestones": [{"day": 1, "task": "design"}]})
        return json.dumps({})


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p35-")
    data = os.path.join(base, "ButlerStorage")
    university = os.path.join(base, "University")
    nas_dir = os.path.join(base, "NAS")
    nas_inbox = os.path.join(base, "NAS", "Inbox")
    site = os.path.join(base, "site")
    os.makedirs(data, exist_ok=True)
    os.makedirs(nas_dir, exist_ok=True)

    cfg = Config()
    cfg.data_dir = data
    cfg.course_dir = university
    cfg.courses = ["CS168"]
    cfg.nas_enabled = True
    cfg.nas_dir = nas_dir
    cfg.nas_inbox_dir = nas_inbox
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.llm_api_key = "sk-test-secret"
    cfg.ensure_dirs()

    c = Container(cfg)

    print("\n=== 3.5.1 Course URL hierarchy join ===")
    html = ('<a href="/files/spec.pdf">a</a>'
            '<a href="../files/spec.pdf">b</a>'
            '<a href="spec.pdf?v=2">c</a>'
            '<a href="//cdn.school.edu/a.pdf">d</a>')
    got = CourseIntelligence._asset_links(html, "https://school.edu/cs168/index.html")
    expect = ["https://school.edu/files/spec.pdf",
              "https://school.edu/cs168/spec.pdf?v=2",
              "https://cdn.school.edu/a.pdf"]
    check("absolute + root-relative resolve to site",
          got[0] == expect[0], got[0])
    check("query URL stays in-thread", got[1] == expect[1], got[1])
    check("protocol-relative resolves to https", got[2] == expect[2], got[2])
    check("dedupe (../ and /files collide) preserves order", len(got) == 3, str(len(got)))

    print("\n=== 3.5.2 NAS routing-base path binding ===")
    proj = os.path.join(university, "CS168", "Projects")
    bound = c.nas._bind_into_nas(proj)
    want = os.path.join(nas_dir, "University", "CS168", "Projects")
    check("course tree preserved under NAS root", os.path.normpath(bound) == os.path.normpath(want), bound)
    check("binding stays strictly inside NAS", bound.startswith(os.path.realpath(nas_dir)), bound)
    # a path outside the course root must never escape NAS
    escape = c.nas._bind_into_nas(os.path.join(base, "etc", "passwd"))
    check("foreign path cannot escape NAS root", os.path.normpath(escape).startswith(os.path.normpath(nas_dir)),
          escape)

    print("\n=== 3.5.3 Recipe estimate / data-quality fields ===")
    rid = c.db.add_recipe("Test Curry", source="themealdb", source_url="https://t.me/curry",
                          ingredients=["chicken"], tags=["quick"],
                          time_estimated=1, cost_estimated=1, nutrition_source="author")
    row = c.db.recipe_by_id(rid)
    check("add_recipe stores time_estimated", int(row["time_estimated"]) == 1, row["time_estimated"])
    check("add_recipe stores cost_estimated", int(row["cost_estimated"]) == 1, row["cost_estimated"])
    check("add_recipe stores nutrition_source", row["nutrition_source"] == "author", row["nutrition_source"])
    again = c.db.add_recipe("Test Curry", source="themealdb",
                            source_url="https://t.me/curry", ingredients=["chicken"])
    check("duplicate import dedupes by (name,source_url)", int(again) == rid, f"{again} vs {rid}")
    n = len([r for r in c.db.recipes() if r["name"] == "Test Curry"])
    check("no silent duplicate rows for a recipe", n == 1, n)

    print("\n=== 3.5.4 Search vs rank split ===")
    r_low = Recipe("LowScore", ["banana"], 30, 2, cost=2.0, rating=2.0,
                   favorite=False, times_used=0)
    r_high = Recipe("HighScore", ["banana"], 30, 2, cost=2.0, rating=5.0,
                    favorite=True, times_used=3)
    ranked = c.chef.rank([r_low, r_high], available={})
    check("rating+favourite+usage lift a recipe", ranked[0]["recipe"].name == "HighScore",
          str([(r["recipe"].name, r["score"]) for r in ranked]))
    c.food.add("banana", quantity=2, unit="pcs",
               expiration=int(datetime.now().timestamp()) + 86400)
    r_uses = Recipe("UsesBanana", ["banana"], 30, 2, cost=2.0)
    r_plain = Recipe("Plain", ["rice"], 30, 2, cost=2.0)
    ranked2 = c.chef.rank([r_uses, r_plain], available={"banana": 2, "rice": 1})
    check("expiration pressure prioritises soon-expiring use",
          ranked2[0]["recipe"].name == "UsesBanana",
          str([(r["recipe"].name, r["score"]) for r in ranked2]))

    print("\n=== 3.5.5 Two-stage semantic classifier ===")
    c.chat = FakeChat(cfg.llm_api_key)  # install LLM double for stage-2 tests
    # Stage 1b: code only in *content* -> refined into course subfolder, but
    # it is content-derived, so it is low-confidence and must be confirmed.
    subj = os.path.join(nas_inbox, "document123.pdf")
    make_pdf(subj, "CS168 Project 2: build a chatbot agent. Due in two weeks.")
    route = c.nas._route_dest(subj)
    check("content-routed into course subfolder",
          route["dest"] == os.path.join(nas_dir, "University", "CS168", "Projects"), route["dest"])
    check("content-derived route requires confirmation", route.get("confirm") is True,
          route.get("method"))
    # Stage 2: no code anywhere, category "Other" -> LLM guesses a course.
    subj2 = os.path.join(nas_inbox, "document456.txt")
    with open(subj2, "w") as fh:
        fh.write("Probability and statistics assignment deck notes.")
    route2 = c.nas._route_dest(subj2)
    check("LLM semantic classifier resolves ambiguous doc",
          route2["dest"] == os.path.join(nas_dir, "University", "STAT220", "Projects"),
          route2["dest"])
    check("high-confidence semantic route moves without confirm", route2.get("confirm") is False,
          route2.get("method"))
    ing = c.decider.resolve(c.decider.parse("organize inbox"))
    moved = [m[1] for m in ing.get("moved", [])]
    pending = ing.get("pending", [])
    check("confirmed route was moved, ambiguous was held",
          any("document456.txt" in x for x in moved), str(moved))
    check("ambiguous content-derived doc held for confirmation",
          any("document123.pdf" in str(p.get("path")) for p in pending), str(pending))

    print("\n=== 3.5.6/7 Assignment pipeline + cross-domain e2e ===")
    e2e_site = os.path.join(base, "site2")
    make_site(e2e_site, ["CS168_Project2.pdf"])
    r = c.decider.resolve(c.decider.parse(f"add course CS168 {e2e_site}"))
    check("e2e course added", r.get("ok") and not r.get("need_url"), r.get("code"))
    updates = c.courses.check_all()
    proj_dir = os.path.join(university, "CS168", "Projects")
    check("Project2 detected + downloaded into Projects", os.path.exists(proj_dir) and
          any("CS168_Project2.pdf" in f for f in os.listdir(proj_dir)), proj_dir)
    check("assignment understanding produced a task update",
          any(u.get("kind") == "assignment" and u.get("task", {}).get("task_id") for u in updates),
          str([u.get("kind") for u in updates]))
    tasks = [dict(x) for x in c.db.tasks("active")]
    t = next((x for x in tasks if "Project 2" in x["title"]), None)
    check("task created by the pipeline", t is not None, str([x["title"] for x in tasks]))
    if t:
        check("workload estimate materialised", int(t["est_minutes"]) == 360, t["est_minutes"])
        check("priority derived from deadline proximity (deterministic)",
              int(t["priority"]) == 4, t["priority"])
        check("assignment source recorded in detail", "Source:" in str(t["detail"]), str(t["detail"]))
    sync_again = c.courses.sync_assignments("CS168")
    check("re-sync is deduped (no duplicate task)", sync_again.get("count") == 0,
          str(sync_again.get("count")))

    print("\n=== 3.5.8 Safety assertions ===")
    # Low-confidence / bulk organisation must not apply without confirmation.
    plan = Plan(plan_id="safe1", title="test", confirmed=False)
    plan.items.append(PlanItem("move", subj, os.path.join(university, "CS168", "Projects", "x.pdf"),
                               "test", {}))
    try:
        c.organizer.apply_plan(plan, user="tester")
        check("bulk plan refuses to apply without confirmation", False, "applied anyway")
    except EngineError:
        check("bulk plan refuses to apply without confirmation", True, "EngineError raised")
    # LLM is never handed filesystem/shell authority; prompts must not leak secrets.
    leaked = any(cfg.llm_api_key in p for p in c.chat.prompts)
    check("llm_api_key never appears in any prompt", not leaked, f"{len(c.chat.prompts)} prompts")
    check("prompts contain only description text (no shell/fs ctl)",
          all(cfg.llm_api_key not in p and "rm " not in p and "shutil" not in p
              for p in c.chat.prompts), True)
    # The LLM never writes to the filesystem: no extra unknown files appeared
    # in the course tree beyond the downloaded asset.
    extra = [f for f in os.listdir(proj_dir) if f != "CS168_Project2.pdf"]
    check("LLM did not write files (only the downloaded asset present)", extra == [], str(extra))
    # Understanding persisted to DB, not to disk.
    d = [dict(x) for cid in [c.db.course_by_code("CS168")["id"]]
         for x in c.db.course_documents(cid)]
    check("understanding + task link persisted to DB",
          any(int(x.get("task_id", 0)) > 0 and x.get("understanding") for x in d),
          str([(x.get("title"), x.get("document_type"), x.get("task_id")) for x in d]))

    print("\n=== 3.5.9 Decider wiring ===")
    for phrase, kind in [
        ("sync assignments", "course_assignments"),
        ("digest CS168 assignments", "course_assignments"),
        ("whats due for the assignment", "course_assignments"),
        ("what can i cook", "recipe"),
    ]:
        check(f"parse {phrase!r} -> {kind}", c.decider.parse(phrase).kind == kind,
              c.decider.parse(phrase).kind)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
