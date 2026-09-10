"""Phase 3 acceptance tests for Butler (Course/Food/NAS/Context/Proactive).

Run:  .venv/bin/python tests/run_acceptance_p3.py

Creates an isolated sandbox and exercises the Phase 3 subsystems through the
real Container/Decider pipeline. Prints PASS/FAIL for each assertion.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
from datetime import datetime as _datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402

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


@contextlib.contextmanager
def frozen_clock(hour: int = 12, minute: int = 0):
    """Pin ``butler.proactive``'s wall clock to a fixed time-of-day.

    The proactive checks are about content/throttling, not about *when* the
    suite happens to run. Freezing the clock makes them independent of the
    machine's real time (quiet hours are 22:00-08:00) without weakening the
    production quiet-hours behaviour.
    """
    import butler.proactive as proactive

    class _Frozen(_datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            base = _datetime.now(tz) if tz else _datetime.now()
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    original = proactive.datetime
    proactive.datetime = _Frozen
    try:
        yield
    finally:
        proactive.datetime = original


def make_pdf(path: str, body: str) -> None:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), body)
    doc.set_metadata({"title": os.path.basename(path), "author": "Course Staff"})
    doc.save(path)
    doc.close()


def make_site(root: str, files: list[str]) -> str:
    """Build a local course feed: index.html linking asset files."""
    os.makedirs(root, exist_ok=True)
    for f in files:
        make_pdf(os.path.join(root, f), f"{f} content for the course.")
    hrefs = "\n".join(f'<a href="{f}">{f}</a>' for f in files)
    with open(os.path.join(root, "index.html"), "w") as fh:
        fh.write(f"<html><body><h1>CS168</h1>{hrefs}</body></html>")
    return root


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p3-")
    data = os.path.join(base, "ButlerStorage")
    university = os.path.join(base, "University")
    nas_dir = os.path.join(base, "NAS")
    nas_inbox = os.path.join(base, "NAS", "Inbox")
    site = os.path.join(base, "site")
    os.makedirs(data, exist_ok=True)
    os.makedirs(os.path.join(nas_dir), exist_ok=True)

    cfg = Config()
    cfg.data_dir = data
    cfg.course_dir = university
    cfg.courses = ["CS168"]
    cfg.nas_enabled = True
    cfg.nas_dir = nas_dir
    cfg.nas_inbox_dir = nas_inbox
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.ensure_dirs()

    c = Container(cfg)

    print("\n=== Phase 3: Course Intelligence ===")
    spec = os.path.join(site, "CS168_Project_Spec.pdf")
    lec3 = os.path.join(site, "CS168_Lecture_3.pdf")
    make_site(site, ["CS168_Project_Spec.pdf", "CS168_Lecture_3.pdf"])
    r = c.decider.resolve(c.decider.parse(f"add course CS168 {site}"))
    check("course added with url", r.get("ok") and not r.get("need_url"), r.get("code"))

    updates = c.courses.check_all()
    proj_dir = os.path.join(university, "CS168", "Projects")
    lect_dir = os.path.join(university, "CS168", "Lectures")
    check("first run registers + downloads spec",
          any(d.get("document_type") in ("project", "reading") for d in
              [dict(x) for cid in [int(c.courses.list_courses()[0]["id"])]
               for x in c.db.course_documents(cid)]))
    check("spec downloaded into University/CS168/Projects",
          os.path.exists(proj_dir) and any(f.endswith(".pdf") for f in os.listdir(proj_dir)))
    check("lecture downloaded into University/CS168/Lectures",
          os.path.exists(lect_dir) and any("Lecture_3" in f for f in os.listdir(lect_dir)))

    again = c.courses.check_all()
    check("deterministic no-op when unchanged", again == [], str(again))

    # publish a new lecture
    make_site(site, ["CS168_Project_Spec.pdf", "CS168_Lecture_3.pdf",
                     "CS168_Lecture_4.pdf"])
    updates = c.courses.check_all()
    check("detects + downloads newly published lecture",
          any(u.get("kind") == "new" and u.get("doc", {}).get("document_type") == "lecture"
              for u in updates), str(len(updates)))
    check("lecture 4 placed on disk",
          any("Lecture_4" in f for f in os.listdir(lect_dir)))

    print("\n=== Phase 3: Food / Chef ===")
    r = c.decider.resolve(c.decider.parse(
        "add to pantry 3 eggs and 1 carton of milk and 5 apples"))
    names = [a.get("name") for a in r.get("added", [])]
    check("parses 3 items (eggs/milk/apples)", sorted(names) == ["apples", "eggs", "milk"],
          str(names))

    # an item that expires tomorrow -> expiring
    from datetime import datetime
    soon = int(datetime.now().timestamp()) + 86400
    c.food.add("milk", quantity=1, unit="carton", expiration=soon)
    exp = c.decider.resolve(c.decider.parse("whats expiring soon"))["items"]
    check("expiring lists soon-item", any(i.get("name") == "milk" for i in exp),
          str([i.get("name") for i in exp]))

    budget = 60
    plan = c.chef.plan_meal(budget_minutes=budget)
    check("recipe plan fits budget", plan.get("recipe") and
          plan["time_minutes"] <= budget, str(plan.get("recipe")))
    if plan.get("missing"):
        glass = c.chef.grocery_list(meal_plan=plan)
        check("grocery list adds meal shortfalls",
              any(i.get("name") in plan["missing"] for i in glass),
              str([i.get("name") for i in glass]))
    else:
        glass = c.chef.grocery_list()
        check("grocery list returns list", isinstance(glass, list), str(glass))

    eat = c.decider.resolve(c.decider.parse("used some eggs"))
    check("consume removes eaten item", eat.get("removed") is True, str(eat))

    print("\n=== Phase 3: Recipe Library pipeline ===")
    lib = c.chef.library()
    check("library seeded with built-in recipes", len(lib) >= 5, len(lib))
    plan2 = c.chef.plan_meal(budget_minutes=30)
    rid = plan2.get("recipe_id")
    check("planned recipe has a library id", rid, str(plan2.get("recipe")))
    m = c.decider.resolve(c.decider.parse("meal history"))
    check("meal history records the plan", m.get("kind") == "recipe_history" and
          any(h.get("recipe_id") == rid for h in m.get("history", [])), str(m.get("count")))
    fav = c.decider.resolve(c.decider.parse(f"favorite {plan2.get('recipe')}"))
    check("favorite marks a recipe", fav.get("ok") and fav.get("favorite") is True, str(fav))
    rate = c.decider.resolve(c.decider.parse(f"rate {plan2.get('recipe')} 4"))
    check("rate sets a rating", rate.get("ok") and rate.get("rating") == 4.0, str(rate))
    favs = c.chef.favorites()
    check("favorites list includes plan", any(r.get("recipe_id") == rid for r in favs),
          len(favs))
    sr = c.chef.search("pasta")
    check("offline search returns local recipes", bool(sr), str([r["name"] for r in sr[:2]]))

    print("\n=== Phase 3: NAS file management ===")
    inbox_file = os.path.join(nas_inbox, "week3_report.pdf")
    make_pdf(inbox_file, "Weekly progress report body.")
    ing = c.decider.resolve(c.decider.parse("organize inbox"))
    check("inbox ingest moves at least one file", ing.get("count", 0) >= 1, str(ing))
    check("ingested file is absent from inbox", not os.path.exists(inbox_file))
    src, moved = ing.get("moved", [("", "")])[0]
    check("ingested file lands inside NAS root", moved and moved.startswith(nas_dir),
          moved)
    check("ingested file exists on disk", moved and os.path.exists(moved))

    print("\n=== Phase 3: Context + Proactive ===")
    snap = c.context.snapshot()
    for key in ("free_minutes_today", "events_today", "tasks", "courses", "food_expiring",
                "file_count"):
        check(f"context snapshot has {key}", key in snap)
    check("context free minutes is non-negative", snap["free_minutes_today"] >= 0,
          snap["free_minutes_today"])
    msgs = c.proactive.collect()
    check("proactive collect returns a list", isinstance(msgs, list))
    with frozen_clock():
        prun = c.proactive.run()
    check("proactive run completes", prun.get("ok") is True, str(prun))

    print("\n=== Phase 3: Decider wiring ===")
    for phrase, kind in [
        ("add course CS111", "course_add"),
        ("my courses", "course_list"),
        ("check courses", "course_check"),
        ("course CS168 materials", "course_docs"),
        ("add to pantry butter", "food_add"),
        ("what do i have", "food_list"),
        ("whats expiring soon", "food_expiring"),
        ("what can i cook", "recipe"),
        ("grocery list", "grocery"),
        ("briefing", "context"),
    ]:
        check(f"parse {phrase!r} -> {kind}", c.decider.parse(phrase).kind == kind,
              c.decider.parse(phrase).kind)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
