"""Butler — your filesystem assistant.

Usage:
    python -m butler.cli help
    python -m butler.cli index
    python -m butler.cli find "CS168 project specification"
    python -m butler.cli resume
    python -m butler.cli organize ~/Downloads
    python -m butler.cli status
    python -m butler.cli bot             # run the Telegram bot
    python -m butler.cli monitor         # run the course/auto-organise watcher
    python -m butler.cli remote          # run the HTTP remote API
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    p = argparse.ArgumentParser(prog="butler", description="Butler file assistant")
    p.add_argument("command", choices=[
        "help", "status", "storage", "index", "list", "find", "search", "hybrid",
        "ask", "chat", "teach", "resume", "digest",
        "organize", "dupes", "dupes-trash", "trash", "trash-list", "recover",
        "mkdir", "move", "rename", "apply", "backup", "bot", "monitor", "remote",
        "mcp", "daemon",
        "task", "tasks", "day", "now", "done", "skip", "start", "cameup", "why",
        "whythis", "undo", "reschedule", "calendar",
        "course", "courses", "checkcourses", "materials", "ingest",
        "pantry", "food", "addfood", "expiring", "used", "grocery",
        "meal", "recipe", "searchrecipes", "favorites", "recipelibrary",
        "mealhistory", "rate", "context", "where", "around",
    ])
    p.add_argument("args", nargs="*")
    p.add_argument("--yes", action="store_true", help="auto-confirm bulk plans")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--course", action="store_true", help="course-aware organise")
    ns = p.parse_args(argv)

    if ns.command == "help":
        print(p.format_help())
        return 0

    from .core import Container
    container = Container()

    try:
        return dispatch(container, ns)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def dispatch(container: Container, ns: argparse.Namespace) -> int:
    cmd = ns.command
    args = ns.args
    from .decider import Decider
    from .engine import EngineError

    if cmd == "status":
        return emit(container, {"kind": "status", "config": container.cfg.to_dict()}, ns)
    if cmd == "storage":
        return emit(container, {"kind": "status", "config": container.cfg.to_dict()}, ns)
    if cmd == "index":
        stats = {}
        for root in container.cfg.roots + container.cfg.index_roots:
            stats[root] = container.indexer.index_root(root)
        stats["pruned"] = container.indexer.prune_missing()
        return emit(container, {"kind": "index", "stats": stats}, ns)
    if cmd == "list":
        path = args[0] if args else "~/Downloads"
        return emit(container, {"kind": "list", **container.engine.list_dir(path)}, ns)
    if cmd in ("find", "search"):
        q = " ".join(args)
        if cmd == "search":
            return emit(container, {"kind": "find", "mode": "semantic",
                                    "query": q, "results": container.search.semantic(q)}, ns)
        return emit(container, {"kind": "find", "mode": "combined", "query": q,
                                "results": container.search.combined(q)}, ns)
    if cmd == "hybrid":
        q = " ".join(args)
        return emit(container, {"kind": "find", "mode": "hybrid",
                                "query": q, "results": container.search.hybrid(q)}, ns)
    if cmd in ("ask", "chat", "teach"):
        q = " ".join(args)
        out = container.chat.teach(q) if cmd == "teach" else container.chat.answer(q)
        return emit(container, {"kind": "chat", "mode": "teach" if cmd == "teach" else "ask",
                                "answer": out}, ns)
    if cmd == "digest":
        from .scheduler import Scheduler
        return emit(container, {"kind": "text", "text": Scheduler.build_digest(container)}, ns)
    if cmd == "resume":
        return emit(container, {"kind": "resume", "results": container.search.latest_resume()}, ns)
    if cmd == "dupes":
        root = args[0] if args else "~/Downloads"
        return emit(container, {"kind": "dupes", "root": root,
                                "groups": container.engine.detect_duplicates(root)}, ns)
    if cmd == "dupes-trash":
        return plan_prompt(container, container.decider.parse(
            f"delete duplicate files in {args[0] if args else '~/Downloads'}"), ns)
    if cmd == "organize":
        if args[:2] == ["--apply"] or args[:1] == ["--apply"]:
            pass
        folder = _last(arg(args))
        return plan_prompt(container, container.decider.parse(
            f"organize {folder}"), ns)
    if cmd == "mkdir":
        path = args[0] if args else None
        if not path:
            print("usage: butler mkdir <path>", file=sys.stderr)
            return 1
        return emit(container, {"kind": "mkdir", "path": container.engine.mkdir(path)}, ns)
    if cmd == "move":
        if len(args) < 2:
            print("usage: butler move <src> <dest-dir>", file=sys.stderr)
            return 1
        return emit(container, {"kind": "move",
                                "dest": container.engine.move(args[0], args[1])}, ns)
    if cmd == "rename":
        if len(args) < 2:
            print("usage: butler rename <src> <new-name>", file=sys.stderr)
            return 1
        return emit(container, {"kind": "rename",
                                "dest": container.engine.rename(args[0], args[1])}, ns)
    if cmd in ("trash", "trash-list"):
        items = container.trash.list()
        return emit(container, {"kind": "trash", "items": items}, ns)
    if cmd == "recover":
        ids = [int(x) for x in args if x.isdigit()]
        results = [container.trash.recover(i) for i in ids]
        return emit(container, {"kind": "recover", "results": results}, ns)
    if cmd == "apply":
        # apply a saved plan identified by plan_id or just re-parse with --yes
        plan = container.decider.parse(" ".join(args) if args else "")
        return apply_plan(container, plan, ns)
    if cmd == "backup":
        return emit(container, {"kind": "backup", **container.backup.status()}, ns)
    if cmd == "monitor":
        from .monitor import CourseMonitor
        mon = CourseMonitor(container)
        mon.start()
        print("watching:", mon.watch_paths(), "(ctrl-c to stop)")
        try:
            import time as _t
            while True:
                _t.sleep(3600)
        except KeyboardInterrupt:
            mon.stop()
        return 0
    if cmd == "remote":
        from .remote import RemoteServer
        srv = RemoteServer(container)
        srv.start()
        print(f"remote server on :{container.cfg.remote_port} (token auth)")
        try:
            import time as _t
            while True:
                _t.sleep(3600)
        except KeyboardInterrupt:
            srv.stop()
        return 0
    if cmd == "bot":
        from .telebot import TelegramBot
        bot = TelegramBot(container)
        bot.run_forever()
        return 0
    if cmd == "mcp":
        from .mcp import MCPServer
        return MCPServer(container).run()
    if cmd == "task":
        title = " ".join(args).strip()
        title = re.sub(r"^--[\w-]+(\s+\d+)?\s*", "", title)
        est = _kwint(args, "est", 60)
        prio = _kwint(args, "priority", 3)
        due = _kwdue(args)
        tid = container.planner.add_task(title, est_minutes=est, priority=prio,
                                         deadline=due)
        container.planner.reschedule()
        return emit(container, {"kind": "text", "text": f"Added task #{tid}: {title}"}, ns)
    if cmd == "tasks":
        return emit(container, {"kind": "plan_tasks",
                                "tasks": [dict(r) for r in container.db.tasks("active")]}, ns)
    if cmd == "day":
        return emit(container, {"kind": "day", **container.planner.plan_day()}, ns)
    if cmd == "now":
        return emit(container, {"kind": "now",
                                **container.planner.what_now(" ".join(args))}, ns)
    if cmd in ("done", "skip", "start"):
        tid = _taskid(container, args)
        if tid is None:
            print("couldn't find that task; use `butler tasks`", file=sys.stderr)
            return 1
        out = {"done": container.planner.done,
               "skip": container.planner.skip,
               "start": container.planner.start}[cmd](tid)
        return emit(container, {"kind": "text", "text": str(out)}, ns)
    if cmd == "cameup":
        title = " ".join(args).strip()
        est = _kwint(args, "est", 60)
        out = container.planner.came_up(title, est_minutes=est, priority=4)
        return emit(container, {"kind": "text", "text": str(out.get("title", title)) +
                                " scheduled; moved " + str(len(out.get("moved", []))) +
                                " block(s)"}, ns)
    if cmd == "why":
        return emit(container, {"kind": "plan_why", **container.planner.why()}, ns)
    if cmd == "whythis":
        return emit(container, {"kind": "why_this", **container.planner.explain_now(
            " ".join(args))}, ns)
    if cmd == "undo":
        return emit(container, {"kind": "day", **container.planner.undo()}, ns)
    if cmd == "reschedule":
        return emit(container, {"kind": "day", **container.planner.reschedule()}, ns)
    # ------------------------------------------------ Phase 3: course / food
    if cmd == "course":
        text = " ".join(args)
        if not text:
            print("usage: butler course <code> [url]", file=sys.stderr)
            return 1
        return emit(container, container.decider.resolve(
            container.decider.parse(f"add course {text}")), ns)
    if cmd == "courses":
        return emit(container, container.decider.resolve(
            container.decider.parse("show courses")), ns)
    if cmd == "checkcourses":
        return emit(container, container.decider.resolve(
            container.decider.parse("check courses")), ns)
    if cmd == "materials":
        code = args[0] if args else ""
        return emit(container, container.decider.resolve(
            container.decider.parse(f"course {code} materials" if code else "course materials")), ns)
    if cmd == "ingest":
        return emit(container, container.decider.resolve(
            container.decider.parse("organize inbox")), ns)
    if cmd == "food":
        return emit(container, container.decider.resolve(
            container.decider.parse("what do i have")), ns)
    if cmd in ("pantry", "inventory"):
        return emit(container, container.decider.resolve(
            container.decider.parse("what do i have")), ns)
    if cmd == "addfood":
        return emit(container, container.decider.resolve(
            container.decider.parse("add to pantry " + " ".join(args))), ns)
    if cmd == "expiring":
        return emit(container, container.decider.resolve(
            container.decider.parse("what's expiring soon")), ns)
    if cmd == "used":
        return emit(container, container.decider.resolve(
            container.decider.parse("used " + " ".join(args))), ns)
    if cmd == "grocery":
        return emit(container, container.decider.resolve(
            container.decider.parse("grocery list")), ns)
    if cmd == "recipe":
        return emit(container, container.decider.resolve(
            container.decider.parse("what can i cook")), ns)
    if cmd == "meal":
        return emit(container, container.decider.resolve(
            container.decider.parse("plan " + " ".join(args) + " meal")), ns)
    if cmd == "searchrecipes":
        return emit(container, container.decider.resolve(
            container.decider.parse("search recipes " + " ".join(args))), ns)
    if cmd == "favorites":
        return emit(container, container.decider.resolve(
            container.decider.parse("favorites")), ns)
    if cmd == "recipelibrary":
        return emit(container, container.decider.resolve(
            container.decider.parse("recipe library")), ns)
    if cmd == "mealhistory":
        return emit(container, container.decider.resolve(
            container.decider.parse("meal history")), ns)
    if cmd == "rate":
        name = " ".join(args)
        return emit(container, container.decider.resolve(
            container.decider.parse("favorite " + name)), ns)
    if cmd == "context":
        return emit(container, container.decider.resolve(
            container.decider.parse("briefing")), ns)
    if cmd == "where":
        return emit(container, container.decider.resolve(
            container.decider.parse("where am i")), ns)
    if cmd == "around":
        return emit(container, container.decider.resolve(
            container.decider.parse("around me")), ns)
    if cmd == "calendar":
        sub = args[0] if args else ""
        if sub == "connect":
            from .gcal import connect
            return emit(container, {"kind": "text",
                                    "text": connect(container.cfg).get("message", "")}, ns)
        if sub in ("sync", "update"):
            return emit(container, {"kind": "text",
                                    "text": "synced " +
                                    str(container.planner.sync_events().get("count", 0)) +
                                    " event(s)"}, ns)
        return emit(container, {"kind": "text",
                                "text": "usage: butler calendar connect|sync"}, ns)
    if cmd == "daemon":
        # run course monitor + scheduler together in the foreground
        from .monitor import CourseMonitor
        from .scheduler import Scheduler
        mon = CourseMonitor(container)
        sched = Scheduler(container)
        mon.start()
        sched.start()
        print("butler daemon: monitoring", mon.watch_paths(), "| scheduler on")
        try:
            import time as _t
            while True:
                _t.sleep(3600)
        except KeyboardInterrupt:
            mon.stop()
            sched.stop()
        return 0
    return 1


def plan_prompt(container: Container, intent, ns) -> int:
    """Build a plan, print it, then either apply (--yes) or wait."""
    plan = container.decider.resolve(intent)
    if not hasattr(plan, "items"):
        return emit(container, plan, ns)
    print(plan.describe())
    if ns.yes:
        plan.confirmed = True
        applied = container.organizer.apply_plan(plan, user="cli")
        print("\n✅ Applied:")
        for a, b in applied.get("moves", []):
            print(f"  {a} -> {b}")
        for p in applied.get("created", []):
            print(f"  created {p}")
        for t in applied.get("trash", []):
            print(f"  trashed {t}")
        return 0
    print("\n(use --yes to apply, or confirm via the Telegram bot)")
    return 0


def apply_plan(container: Container, plan, ns) -> int:
    plan.confirmed = True
    applied = container.organizer.apply_plan(plan, user="cli")
    return emit(container, {"kind": "applied", "applied": applied}, ns)


def emit(container: Container, result: dict, ns) -> int:
    if ns.json:
        print(json.dumps(result, indent=2, default=default_json))
    else:
        print(render(container, result))
    return 0


def arg(a: list[str]) -> list[str]:
    return a


def _kwint(args: list[str], key: str, default: int) -> int:
    m = re.search(rf"--{key}\s+(\d+)", " ".join(args))
    return int(m.group(1)) if m else default


def _kwdue(args: list[str]) -> int:
    from datetime import datetime
    m = re.search(r"--due(?:line)?\s+(\d{1,2}):(\d{2})", " ".join(args))
    if not m:
        return 0
    h, mi = int(m.group(1)), int(m.group(2))
    now_dt = datetime.now()
    dl = now_dt.replace(hour=h, minute=mi, second=0, microsecond=0)
    if dl.timestamp() < now_dt.timestamp():
        dl = dl.replace(day=dl.day + 1)
    return int(dl.timestamp())


def _taskid(container: Container, args: list[str]) -> int | None:
    joined = " ".join(args).strip()
    digits = re.findall(r"\b(\d+)\b", joined)
    if len(digits) == 1:
        tid = int(digits[0])
        if container.db.task_by_id(tid):
            return tid
    low = joined.lower()
    for r in container.db.tasks("active"):
        t = (r["title"] or "").lower()
        if t and (t in low or all(w in low for w in t.split())):
            return int(r["id"])
    return None


def _last(a: list[str]) -> str:
    return a[-1] if a else "~/Downloads"


def render(c: Container, r: dict) -> str:
    from .telebot import human_size
    k = r.get("kind")
    if k == "status":
        cfg = r["config"]
        return (
            "storage : " + cfg["data_dir"] + "\n"
            "trash   : " + cfg["trash_dir"] + "\n"
            "roots   : " + ", ".join(cfg["roots"]) + "\n"
            "course  : " + cfg["course_dir"] + "\n"
            "courses : " + ", ".join(cfg["courses"]) + "\n"
            "model   : " + cfg["embed_model"]
        )
    if k == "index":
        return "\n".join(f"{root}: {stats}" for root, stats in r["stats"].items())
    if k == "list":
        lines = [f"{r['path']} ({r['count']})"]
        for e in r["entries"]:
            tag = "d" if e["dir"] else human_size(e["size"])
            lines.append(f"  {tag}  {e['name']}")
        return "\n".join(lines)
    if k == "find":
        return "\n".join(_fmt_res(r.get("results", []))) or "No results."
    if k == "resume":
        return "\n".join(_fmt_res(r.get("results", []))) or "No resume found."
    if k == "dupes":
        groups = r["groups"]
        if not groups:
            return "No duplicates."
        out = [f"{len(groups)} group(s) in {r['root']}:"]
        for i, g in enumerate(groups, 1):
            files = ", ".join(os.path.basename(p) for p, _s, _m in g["files"])
            out.append(f"{i}. [{human_size(g['size'])}] {files}")
        return "\n".join(out)
    if k == "trash":
        items = r["items"]
        if not items:
            return "Trash is empty."
        return "\n".join(f"#{it['id']} {it['name']} ({it['orig_path']})" for it in items)
    if k == "recover":
        return "\n".join(f"restored {p}" for p in r["results"]) or "nothing"
    if k == "mkdir":
        return f"created {r['path']}"
    if k in ("move", "rename"):
        return f"-> {r['dest']}"
    if k == "chat":
        return str(r["answer"])
    if k == "text":
        return str(r["text"])
    if k == "backup":
        return f"last={r['last_backup']} free={r['free_gb']}GB trash={r['trash_gb']}GB dir={r['backup_dir']}"
    if k == "day":
        return r.get("text", "")
    if k == "now":
        return r.get("answer", "")
    if k == "plan_tasks":
        if "tasks" in r:
            tasks = r["tasks"]
            if not tasks:
                return "No active tasks."
            return "\n".join(f"#{t['id']} {t['title']}  (est {t['est_minutes']}m, "
                             f"p{t['priority']}, {t['status']})" for t in tasks)
        return str(r)
    if k == "plan_why":
        return r.get("reason", "no explanation")
    # ------------------------------------------------ Phase 3 renderers
    if k == "course_add":
        if r.get("need_url"):
            return f"Tracked {r.get('code')}; need a URL — `butler course {r.get('code')} <url>`"
        return f"Tracking course {r.get('code')}."
    if k == "course_list":
        cs = r.get("courses", [])
        return "\n".join(f"{c['code']} — {c.get('name','')} ({c.get('url') or 'no URL'})"
                         for c in cs) or "No courses tracked."
    if k == "course_check":
        us = r.get("updates", [])
        return "\n".join(f"{u.get('course_code','')}: {u.get('title','')} "
                         f"({u.get('doc_type','')})" for u in us) or "No new course activity."
    if k == "course_docs":
        ds = r.get("documents", [])
        return "\n".join(f"{d.get('title','')} ({d.get('doc_type','')}) @ {d.get('local_path','')}"
                         for d in ds) or f"No materials for {r.get('code','')}."
    if k == "course_assignments":
        if not r.get("ok", True):
            return str(r.get("error", "no assignments"))
        lines = []
        for code, res in (r.get("results") or {}).items():
            for item in res.get("created", []) + res.get("updated", []):
                action = "updated" if item in res.get("updated", []) else "created"
                sched = item.get("schedule") or {}
                slots = ", ".join(
                    f"{_hm(s['start_min'])}-{_hm(s['end_min'])}" for s in sched.get("slots", []))
                conflict = sched.get("conflict")
                line = (f"{code} → {action} task #{item['task_id']} {item['title']} "
                        f"(due {item['deadline']}, ~{item['est_hours']}h")
                if slots:
                    line += f", study {slots}"
                line += ")"
                if conflict:
                    line += f" | ⚠ conflict: {sched.get('deficit')}m short before deadline"
                lines.append(line)
        return "\n".join(lines) or "No un-understood assignments found."
    if k == "food_add":
        return "Stored: " + ", ".join(a.get("name", "") for a in r.get("added", [])) or "nothing"
    if k == "food_list":
        return "\n".join(f"{i['name']} — {i.get('quantity','')}{i.get('unit','')}"
                         for i in r.get("items", [])) or "Pantry is empty."
    if k == "food_expiring":
        return "\n".join(f"{i['name']} ({i.get('days_left','')}d)" for i in r.get("items", [])) \
            or "Nothing expiring soon."
    if k == "food_consume":
        name = r.get("name", "")
        return f"Removed {name}." if r.get("removed") else f"{name} updated to {r.get('quantity')}"
    if k in ("recipe", "meal_plan"):
        p = r.get("plan", {})
        if not p.get("recipe"):
            return p.get("message", "No recipe fits.")
        tag = "🍳 " + p["recipe"] if k == "recipe" else f"🍽 {p['meal']}: {p['recipe']}"
        ms = f"\n  get: {', '.join(p['missing'])}" if p.get("missing") else ""
        return f"{tag} (~{p['time_minutes']}min, {p['difficulty']}, ¥{p['cost']})\n" \
               f"have {int(p['have_ratio']*100)}% ingredients{ms}".replace("'", "'")
    if k == "recipe_search":
        res = r.get("results", [])
        return "\n".join(f"• {x['name']} ({x['time_minutes']}min, "
                         f"source={x.get('source')})" for x in res[:8]) \
            or f"Nothing found for '{r.get('query','')}'"
    if k == "recipe_library":
        rec = r.get("recipes", [])
        return "\n".join(f"{'⭐' if x.get('favorite') else '•'} {x['name']} "
                         f"({x['time_minutes']}min, used {x.get('times_used',0)})"
                         for x in rec[:20]) or "(empty)"
    if k == "favorites":
        rec = r.get("recipes", [])
        return "\n".join(f"⭐ {x['name']} ({x['time_minutes']}min, "
                         f"rating {float(x.get('rating',0)):.1f})" for x in rec) or "(none yet)"
    if k == "recipe_history":
        h = r.get("history", [])
        return "\n".join(f"• {x.get('recipe_name','?')} — {x.get('meal','')}"
                         for x in h[:20]) or "No meals planned yet."
    if k == "recipe_mark":
        return f"{'⭐ starred' if r.get('favorite') else 'unstarred'} #{r.get('recipe_id')} " \
               f"rating={r.get('rating')}"
    if k == "grocery":
        return "\n".join(f"{i['name']} — {i.get('quantity','')}{i.get('unit','')}"
                         for i in r.get("items", [])) or "Nothing to buy."
    if k == "nas_ingest":
        moved = r.get("moved", [])
        return "\n".join(f"  {os.path.basename(a)} -> {b}" for a, b in moved) \
            or "Inbox is empty."
    if k == "context":
        return r.get("snapshot_text", "") or str(r.get("snapshot"))
    if k == "proactive":
        return "\n".join(r.get("messages", [])) or "All clear."
    if k == "where_am_i":
        from .context import describe_location, presence_battery
        p = r.get("presence", {})
        return describe_location(p) + presence_battery(p) + "."
    if k == "around_me":
        from .context import describe_location, presence_battery
        p = r.get("presence", {})
        line = describe_location(p) + presence_battery(p) + "."
        events = r.get("events_today", [])
        if events:
            nearby = ", ".join(str(e.get("title", "event")) for e in events[:5])
            line += f" · today: {nearby}"
        return line
    # ------------------------------------------------ Phase 4.2 renderers
    if k == "why_this":
        cand = r.get("candidate") or {}
        head = f"Why {cand.get('title', 'this')}?" if cand.get("title") else "Why this?"
        return head + (r.get("reason", "") or "")
    if k == "location_change":
        return r.get("answer", str(r))
    if k == "move_block":
        return r.get("answer", str(r))
    return json.dumps(r, indent=2, default=default_json)


def _fmt_res(results) -> list[str]:
    from .telebot import ctime, human_size
    return [
        f"{r.get('name')}  ({human_size(r.get('size'))}, {ctime(r.get('mtime'))})\n"
        f"  {r.get('path')}"
        for r in results
    ]


def _hm(minutes: int) -> str:
    minutes = max(0, min(minutes, 1439))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def default_json(o):
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
