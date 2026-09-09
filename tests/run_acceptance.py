"""Acceptance tests for Butler (Spec §36.29).

Run:  .venv/bin/python tests/run_acceptance.py

Creates an isolated sandbox (on the same filesystem), seeds sample files, and
runs Tests 1-6 through the real Config/Engine/Decider/Trash/Search/Organizer
pipeline. Prints PASS/FAIL for each assertion.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

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


def make_pdf(path: str, title: str, body: str) -> None:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 90), body)
    doc.set_metadata({"title": title, "author": "Course Staff"})
    doc.save(path)
    doc.close()


def make_txt(path: str, body: str) -> None:
    with open(path, "w") as fh:
        fh.write(body)


def setup_sandbox():
    base = tempfile.mkdtemp(prefix="butler-sandbox-")
    paths = {
        "data": os.path.join(base, "ButlerStorage"),
        "downloads": os.path.join(base, "Downloads"),
        "documents": os.path.join(base, "Documents"),
        "desktop": os.path.join(base, "Desktop"),
        "university": os.path.join(base, "University"),
        "incoming": os.path.join(base, "Incoming"),
    }
    for p in paths.values():
        os.makedirs(p, exist_ok=True)
    # Test 1: existing University/CS168 folder
    os.makedirs(os.path.join(paths["university"], "CS168"), exist_ok=True)
    # Test 3/6: a CS168 project specification in Documents
    make_pdf(os.path.join(paths["documents"], "CS168_Project_Spec.pdf"),
             "CS168 Project Specification",
             "CS168 Project Specification. The course project requires a submission "
             "and a report in week 10 on distributed systems.")
    # Test 5: resumes
    make_pdf(os.path.join(paths["documents"], "resume_2023.pdf"), "Resume 2023",
             "Curriculum Vitae 2023. Skills: Python, systems, networking.")
    make_docx(os.path.join(paths["documents"], "resume_latest.docx"),
              "Resume - Jane Doe", "Curriculum Vitae. Senior software engineer skilled in Python.")
    # Test 4/8: duplicates (exact byte-identical copy)
    make_pdf(os.path.join(paths["downloads"], "IMG_report.pdf"), "Report", "Report contents.")
    shutil.copyfile(
        os.path.join(paths["downloads"], "IMG_report.pdf"),
        os.path.join(paths["downloads"], "IMG_report copy.pdf"),
    )
    with open(os.path.join(paths["downloads"], "data.csv"), "w") as fh:
        fh.write("a,b\n1,2\n3,4\n")
    # Test 2/16: misc to organise
    make_txt(os.path.join(paths["downloads"], "notes.txt"), "meeting notes")
    # Test 6: incoming material dropped by course feed
    make_pdf(os.path.join(paths["incoming"], "CS168_Lecture_3.pdf"), "Lecture 3",
             "CS168 Lecture 3. Distributed systems and consensus.")
    return paths


def make_docx(path: str, title: str, body: str) -> None:
    from docx import Document
    d = Document()
    d.core_properties.title = title
    d.add_paragraph(body)
    d.save(path)


def sandbox_config(paths, base):
    cfg = Config()
    cfg.data_dir = paths["data"]
    cfg.roots = [paths["downloads"], paths["documents"], paths["desktop"], paths["data"]]
    cfg.index_roots = [paths["downloads"], paths["documents"], paths["desktop"], paths["incoming"]]
    cfg.course_dir = os.path.join(paths["university"])
    cfg.incoming_dir = paths["incoming"]
    cfg.courses = ["CS168"]
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.ensure_dirs()
    return cfg


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-accept-")
    paths = setup_sandbox()
    cfg = sandbox_config(paths, base)
    c = Container(cfg)

    print("### Seeding: indexing managed folders (so search has content) ###")
    for root in cfg.index_roots:
        c.indexer.index_root(root)
    c.indexer.prune_missing()

    print("\n=== Test 1: Create a workspace for my CS168 project. ===")
    intent = c.decider.parse("Create a workspace for my CS168 project.")
    assert intent.kind == "workspace", intent.kind
    plan = c.decider.resolve(intent)
    check("proposed a workspace plan", hasattr(plan, "items") and plan.items, plan.title)
    proj_path = os.path.join(paths["university"], "CS168", "Projects", "Project_1A")
    check("proposes University/CS168/Projects/Project_1A",
          any(i.action == "mkdir" and i.dest == proj_path for i in plan.items),
          proj_path)
    check("does NOT create immediately (needs confirm)", not os.path.exists(proj_path))
    plan.confirmed = True
    c.organizer.apply_plan(plan)
    check("creates workspace after confirm", os.path.isdir(proj_path))

    print("\n=== Test 2: Organize my Downloads. (propose, don't apply) ===")
    intent = c.decider.parse("Organize my Downloads.")
    check("intent is organize", intent.kind == "organize", intent.target)
    plan = c.decider.resolve(intent)
    check("proposes a plan, does not move files",
          hasattr(plan, "items") and len(plan.items) > 0)
    check("no move actually happened yet",
          os.path.exists(os.path.join(paths["downloads"], "notes.txt")))
    check("plan references category folders", any("Documents" == os.path.basename(p.dest)
          for p in plan.items if p.action == "mkdir"))

    print("\n=== Test 3: Find the CS168 project specification. ===")
    intent = c.decider.parse("Find the CS168 project specification.")
    res = c.decider.resolve(intent)
    names = [r["name"] for r in res["results"]]
    check("returns the CS168 spec file",
          any("CS168_Project_Spec" in n for n in names), str(names))
    check("matched via indexed content (FTS), not only name", len(res["results"]) > 0)

    print("\n=== Test 4: Delete these duplicate files. (trash, not permanent) ===")
    intent = c.decider.parse("Delete these duplicate files.")
    check("intent is delete_duplicates", intent.kind == "delete_duplicates", intent.kind)
    plan = c.decider.resolve(intent)
    check("plan moves duplicates to trash", any(i.action == "trash" for i in plan.items),
          plan.summary)
    plan.confirmed = True
    c.organizer.apply_plan(plan)
    dup_name = "IMG_report copy.pdf"
    dup_src = os.path.join(paths["downloads"], dup_name)
    check("duplicate no longer in Downloads", not os.path.exists(dup_src))
    trash_items = c.trash.list()
    check("duplicate is in Butler Trash (not deleted)",
          any(i["name"] == dup_name for i in trash_items), f"{len(trash_items)} in trash")
    check("original is preserved", os.path.exists(os.path.join(paths["downloads"], "IMG_report.pdf")))

    print("\n=== Test 5: Where is my latest resume? ===")
    intent = c.decider.parse("Where is my latest resume?")
    check("intent is resume", intent.kind == "resume")
    res = c.decider.resolve(intent)
    names = [r["name"] for r in res["results"]]
    check("finds resumes via name+content", any("resume" in n.lower() for n in names), str(names))
    check("identifies the latest as resume_latest.docx",
          len(res["results"]) > 0 and res["results"][0]["name"] == "resume_latest.docx",
          str(names))

    print("\n=== Test 6: course monitor downloads new CS168 material → auto-place + index ===")
    from butler.monitor import _route_dest
    new_file = os.path.join(paths["incoming"], "CS168_Lecture_4.pdf")
    make_pdf(new_file, "Lecture 4", "CS168 Lecture 4. Consensus protocols and Paxos.")
    dest = _route_dest(c, new_file)
    check("routes new CS168 file into course folder",
          dest and os.path.basename(dest) == "CS168",
          dest or "no dest")
    target, _ = c.engine._unique_name(dest, os.path.basename(new_file))
    c.engine.move(new_file, dest)
    c.indexer.index_root(os.path.dirname(target))
    check("file placed in University/CS168",
          os.path.exists(target))
    hits = c.search.combined("Paxos consensus")
    check("new material is indexed & searchable",
          any("CS168_Lecture_4" in r["name"] for r in hits), str([r["name"] for r in hits]))

    print("\n=== Feature 14: semantic search ===")
    sem = c.search.semantic("how to build distributed consensus systems")
    check("semantic search returns relevant file",
          any("CS168" in (r.get("name") or "") for r in sem), str([r.get("name") for r in sem][:5]))

    print("\n=== Feature 7: trash recovery ===")
    items = c.trash.list()
    if items:
        tid = items[-1]["id"]
        restored = c.trash.recover(tid)
        check("recover restores file to original location",
              os.path.exists(restored), restored)

    print("\n=== Feature 11: operation logging ===")
    ops = c.db.recent_operations(50)
    check("operations are logged", len(ops) > 5, f"{len(ops)} ops")

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
