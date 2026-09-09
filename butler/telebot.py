"""Telegram interface (feature 9, 10, 12).

Wraps the decider + engine behind a Telegram bot. Supports:

  * natural-language messages (handled by :class:`Decider`)
  * explicit slash commands
  * inline confirmation buttons for every *bulk* mutation plan (feature 12),
    and for individual destructive ops.

Bulk mutations (organize / create workspace / trash-duplicates) are never
applied until the user taps Confirm.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from .core import Container

log = logging.getLogger("butler.telebot")


def _fmt_results(items: list[dict[str, Any]], kind: str) -> str:
    if not items:
        return "No matches."
    lines = []
    for i, r in enumerate(items, 1):
        when = ctime(r.get("mtime"))
        lines.append(f"{i}. {r.get('name')} · {r.get('path')}")
        lines.append(f"   {human_size(r.get('size'))} · {when}")
        if r.get("score") is not None:
            lines.append(f"   score {r['score']}")
    return "\n".join(lines)


def ctime(ts: int | None) -> str:
    if not ts:
        return "?"
    try:
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def human_size(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class TelegramBot:
    def __init__(self, container: Container):
        self.container = container
        self.app: Application | None = None
        # keep pending plans in memory: plan_id -> (plan, user)
        self.pending: dict[str, dict[str, Any]] = {}

    def build(self) -> Application:
        if not self.container.cfg.telegram_token:
            raise RuntimeError("Telegram token not configured (BUTLER_TELEGRAM_TOKEN / config.toml)")
        self.app = Application.builder().token(self.container.cfg.telegram_token).build()
        self.app.add_handler(CommandHandler("start", self.cmd_help))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("storage", self.cmd_storage))
        self.app.add_handler(CommandHandler("list", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("find", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("search", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("hybrid", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("ask", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("teach", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("resume", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("organize", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("dupes", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("trash", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("recover", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("mkdir", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("index", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("backup", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("empty", self.cmd_slash_wrap))
        # Phase 2 planner commands
        self.app.add_handler(CommandHandler("day", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("now", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("tasks", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("task", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("done", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("skip", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("begin", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("go", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("cameup", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("why", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("undo", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("calendar", self.cmd_slash_wrap))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_message))
        # send-file ingestion (feature 21): documents, photos, videos, audio
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.on_document))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        self.app.add_handler(MessageHandler(filters.VIDEO, self.on_video))
        self.app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, self.on_audio))
        self.app.add_handler(CallbackQueryHandler(self.on_callback))
        return self.app

    async def run(self) -> None:
        self.build()
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        log.info("Telegram bot running")

    def run_forever(self) -> None:
        self.build()
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    # ------------------------------------------------------------ auth
    def _authorized(self, update: Update) -> bool:
        users = self.container.cfg.telegram_allowed_users
        if not users:
            return True  # open when no allow-list configured
        uid = update.effective_user.id if update.effective_user else None
        return uid in users

    # ------------------------------------------------------------ handlers
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text(self.container.decider._help()["text"])

    async def cmd_storage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        cfg = self.container.cfg
        text = (
            "━━ Butler storage ━━\n"
            f"Base  : {cfg.data_dir}\n"
            f"Trash : {cfg.trash_dir}\n"
            f"Roots : {', '.join(cfg.roots)}\n"
            f"Course: {cfg.course_dir}\n"
            f"Inbox : {cfg.incoming_dir}\n"
            f"Courses: {', '.join(cfg.courses)}\n"
            f"NAS   : {len(cfg.smb_shares)} share(s)\n"
            f"Model : {cfg.embed_model}\n"
            f"Backup: {cfg.backup_dir or 'not configured'}"
        )
        await update.effective_message.reply_text(text)

    async def cmd_slash_wrap(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        cmd = (update.message.text or "").split()[0].lstrip("/").lower()
        arg = (update.message.text or "").partition(" ")[2].strip()
        await self._dispatch(update, f"/{cmd} {arg}", user=update.effective_user.username or "telegram")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._dispatch(update, update.message.text or "",
                             user=update.effective_user.username or "telegram")

    # ------------------------------------------------------------ ingestion
    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._ingest_media(
            update,
            update.message.document.get_file(),
            update.message.document.file_name or "document.bin",
        )

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        photo = update.message.photo[-1]
        await self._ingest_media(
            update, photo.get_file(), f"photo_{int(time.time())}.jpg")

    async def on_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        v = update.message.video
        ext = os.path.splitext(v.file_name or "")[1] or ".mp4"
        await self._ingest_media(
            update, v.get_file(), f"video_{int(time.time())}{ext}")

    async def on_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        a = update.message.audio or update.message.voice
        ext = os.path.splitext(a.file_name or "")[1] or (".ogg" if update.message.voice else ".mp3")
        await self._ingest_media(
            update, a.get_file(), f"audio_{int(time.time())}{ext}")

    async def _ingest_media(self, update: Update, file_future, name: str) -> None:
        if not self._authorized(update):
            return
        chat_id = update.effective_chat.id
        cfg = self.container.cfg
        # stage into the incoming drop-box (or the managed data dir)
        dest = cfg.incoming_dir or cfg.data_dir
        os.makedirs(dest, exist_ok=True)
        tfile = await file_future
        target, _ = self.container.engine._unique_name(dest, name)
        try:
            await tfile.download_to_drive(target)
        except Exception as exc:
            await update.effective_message.reply_text(f"⚠️ download failed: {exc}")
            return
        placed = self._route_media(target)
        # index the newly placed file so it is searchable immediately
        try:
            self.container.indexer._index_file(placed, {}, with_embeddings=True)
        except Exception as exc:
            log.debug("ingest index error: %s", exc)
        await update.effective_message.reply_text(
            f"📥 Received `{name}`\n→ {placed}")

    def _route_media(self, path: str) -> str:
        """Move a staged file into its course/category directory, safe."""
        dest = self.container.organizer.route_file(path)
        os.makedirs(dest, exist_ok=True)
        target, _last = self.container.engine._unique_name(dest, os.path.basename(path))
        if os.path.abspath(target) != os.path.abspath(path):
            shutil.move(path, target)
        return target

    async def _dispatch(self, update: Update, message: str, user: str) -> None:
        chat_id = update.effective_chat.id
        intent = self.container.decider.parse(message)
        try:
            result = self.container.decider.resolve(intent, user)
        except Exception as exc:
            await update.effective_message.reply_text(f"⚠️ {exc}")
            return
        await self._render(update, intent, result)

    async def _render(self, update: Update, intent: Any, result: Any) -> None:
        msg = update.effective_message
        kind = getattr(intent, "kind", None) or result.get("kind", "")
        if kind == "help":
            await msg.reply_text(result["text"])
        elif hasattr(result, "items"):  # a Plan — ask for confirmation (feature 12)
            plan = result
            self._store_plan(plan)
            await msg.reply_text(
                plan.describe(),
                reply_markup=self._confirm_buttons(plan.plan_id),
            )
        elif kind == "resume":
            await msg.reply_text(_fmt_results(result["results"], "resume"))
        elif kind == "find":
            await msg.reply_text(
                f"🔍 {result.get('query')} ({result.get('mode')})\n\n"
                + _fmt_results(result["results"], "find"))
        elif kind == "chat":
            icon = "🎓" if result.get("mode") == "teach" else "💬"
            await msg.reply_text(f"{icon} {result['answer']}")
        elif kind == "dupes":
            groups = result["groups"]
            if not groups:
                await msg.reply_text("No duplicates found.")
                return
            lines = [f"🗂 {len(groups)} duplicate group(s) in {result['root']}:"]
            for i, g in enumerate(groups, 1):
                paths = ", ".join(os.path.basename(p) for p, _s, _m in g["files"])
                lines.append(f"{i}. [{human_size(g['size'])}] {paths}")
            await msg.reply_text("\n".join(lines))
        elif kind == "trash_list":
            items = result["items"]
            if not items:
                await msg.reply_text("Trash is empty.")
                return
            lines = ["🗑 Trash:"]
            for it in items:
                lines.append(f" #{it['id']} {it['name']} · {it['orig_path']}")
            await msg.reply_text("\n".join(lines))
        elif kind == "recover":
            await msg.reply_text("\n".join(f"restored {p}" for p in result.get("results", []))
                                 or "Nothing to restore.")
        elif kind == "empty_trash":
            await msg.reply_text(f"Removed {result.get('removed', 0)} item(s) from trash.")
        elif kind == "list":
            lines = [f"📁 {result['path']} ({result['count']})"]
            for e in result["entries"]:
                tag = "📁" if e["dir"] else f"({e['size']})"
                lines.append(f"  {tag} {e['name']}")
            await msg.reply_text("\n".join(lines[:60]))
        elif kind == "index":
            txt = "\n".join(f"{k}: {v}" for k, v in result["stats"].items())
            await msg.reply_text(f"Indexed:\n{txt}")
        elif kind == "backup":
            await msg.reply_text(f"Backup status:\n{self._backup_text(result)}")
        elif kind == "status":
            await msg.reply_text(str(result["config"]))
        elif kind == "day":
            await msg.reply_text("📅 Today\n```\n" + result.get("text", "") + "\n```")
        elif kind == "now":
            await msg.reply_text("▶️ " + result.get("answer", "Nothing to do."))
        elif kind == "plan_tasks":
            if result.get("error"):
                await msg.reply_text("⚠️ " + result["error"])
                return
            if "restored" in result:
                await msg.reply_text("↩️ Undone.\n```\n" +
                                     result.get("restored", {}).get("text", "") + "\n```")
                return
            if "added" in result:
                a = result["added"]
                await msg.reply_text(f"✅ Added task #{a['task_id']}: {a['title']}")
                return
            if "moved" in result:
                await msg.reply_text(f"✅ '{result.get('title')}' added — schedule adapted, "
                                     f"{len(result.get('moved', []))} block(s) moved.")
                return
            if result.get("task_id"):
                await msg.reply_text("✅ Task updated.")
                return
            tasks = result.get("tasks")
            if tasks is not None:
                if not tasks:
                    await msg.reply_text("No active tasks. Add one with /task.")
                    return
                lines = ["🗂 Active tasks:"]
                for t in tasks:
                    lines.append(f" #{t['id']} {t['title']} "
                                 f"(est {t['est_minutes']}m, p{t['priority']})")
                await msg.reply_text("\n".join(lines))
                return
            await msg.reply_text(str(result.get("text", result)))
        elif kind == "plan_why":
            reason = result.get("reason", "")
            cause = result.get("cause")
            suffix = f"\n\n⛔ hard commitment: {cause}" if cause else ""
            await msg.reply_text("🤔 " + reason + suffix)
        elif kind == "connect":
                await msg.reply_text("To connect Google Calendar:\n"
                         "1. Create creds as in the docs\n"
                         "2. `butler calendar connect`\n"
                         "3. add the client_secret.json")
        elif kind == "course_add":
            code = result.get("code", "")
            if result.get("need_url"):
                await msg.reply_text(
                    f"✅ Tracked {code}.\nSend me the course website URL (e.g. "
                    f"`add course {code} https://...`) and I'll monitor it.")
            else:
                await msg.reply_text(f"✅ Tracking course {code}.")
        elif kind == "course_list":
            courses = result.get("courses", [])
            if not courses:
                await msg.reply_text("No courses tracked yet. `add course CS168 https://...`")
                return
            lines = []
            for c in courses:
                url = c.get("url") or "—no URL—"
                lines.append(f"• {c['code']} — {c.get('name','(unnamed)')}\n   {url}")
            await msg.reply_text("Courses I'm monitoring:\n" + "\n".join(lines))
        elif kind == "course_check":
            updates = result.get("updates", [])
            if not updates:
                await msg.reply_text("No new course activity. 👍")
                return
            lines = [f"• {u.get('course_code','')} — {u.get('title','')} ({u.get('doc_type','')})"
                     for u in updates]
            await msg.reply_text("📚 New course activity:\n" + "\n".join(lines))
        elif kind == "course_docs":
            docs = result.get("documents", [])
            if not docs:
                await msg.reply_text(f"No materials for {result.get('code','')} yet.")
                return
            lines = [f"• {d.get('title','')} ({d.get('doc_type','')}) "
                     f"@ {d.get('local_path','')}" for d in docs]
            await msg.reply_text(f"📂 {result.get('code','')} materials:\n" + "\n".join(lines))
        elif kind == "food_add":
            added = result.get("added", [])
            names = ", ".join(a.get("name", "") for a in added) or "nothing"
            await msg.reply_text(f"🥫 Stored: {names}")
        elif kind == "food_list":
            items = result.get("items", [])
            if not items:
                await msg.reply_text("Your pantry is empty.")
                return
            lines = [f"• {i['name']} — {i.get('quantity','')}{i.get('unit','')}"
                     for i in items]
            await msg.reply_text("🥕 Pantry:\n" + "\n".join(lines))
        elif kind == "food_expiring":
            items = result.get("items", [])
            if not items:
                await msg.reply_text("Nothing expiring soon. 👍")
                return
            lines = [f"• {i['name']} — {i.get('days_left','')}d remaining"
                     for i in items]
            await msg.reply_text("⏳ Expiring soon:\n" + "\n".join(lines))
        elif kind == "food_consume":
            await msg.reply_text("✅ " + ("Removed " + result['name'] if result.get("removed")
                                          else f"Updated {result['name']} to {result.get('quantity')}"))
        elif kind == "recipe":
            p = result.get("plan", {})
            if not p.get("recipe"):
                await msg.reply_text(p.get("message", "No recipe fits."))
                return
            ms = f"\n     get: {', '.join(p['missing'])}" if p.get("missing") else ""
            await msg.reply_text(
                f"🍳 {p['recipe']} (~{p['time_minutes']}min, {p['difficulty']}, ¥{p['cost']})\n"
                f"have {int(p['have_ratio']*100)}% of ingredients{ms}")
        elif kind == "meal_plan":
            p = result.get("plan", {})
            if not p.get("recipe"):
                await msg.reply_text(p.get("message", "No recipe fits."))
                return
            ms = f"\n     get: {', '.join(p['missing'])}" if p.get("missing") else ""
            await msg.reply_text(
                f"🍽 {p['meal']}: {p['recipe']} (~{p['time_minutes']}min, "
                f"{p['difficulty']}, ¥{p['cost']})\n"
                f"have {int(p['have_ratio']*100)}% of ingredients{ms}")
        elif kind == "recipe_search":
            results = result.get("results", [])
            if not results:
                await msg.reply_text(f"Nothing found for '{result.get('query','')}'.")
                return
            lines = [f"• {r['name']} ({r['time_minutes']}min, "
                     f"have {int(r.get('rating',0) and 0 or 0)} fav={r.get('favorite')})"
                     for r in results[:8]]
            await msg.reply_text(f"🔍 {len(results)} result(s):\n" + "\n".join(lines))
        elif kind == "recipe_library":
            recipes = result.get("recipes", [])
            lines = [f"{'⭐' if r.get('favorite') else '•'} {r['name']} "
                     f"({r['time_minutes']}min, used {r.get('times_used',0)})"
                     for r in recipes[:20]]
            await msg.reply_text(f"📚 Recipe Library ({result.get('count', len(recipes))}):\n"
                                 + ("\n".join(lines) or "(empty)"))
        elif kind == "favorites":
            recipes = result.get("recipes", [])
            lines = [f"⭐ {r['name']} ({r['time_minutes']}min, "
                     f"rating {float(r.get('rating',0)):.1f})" for r in recipes]
            await msg.reply_text("⭐ Favorites:\n"
                                 + ("\n".join(lines) or "(none yet)"))
        elif kind == "recipe_history":
            hist = result.get("history", [])
            if not hist:
                await msg.reply_text("No meals planned yet.")
                return
            lines = [f"• {h.get('recipe_name','?')} — {h.get('meal','')}"
                     for h in hist[:20]]
            await msg.reply_text("🕘 Recent meals:\n" + "\n".join(lines))
        elif kind == "recipe_mark":
            await msg.reply_text(
                f"{'⭐ starred' if result.get('favorite') else 'unstarred'} #"
                f"{result.get('recipe_id')} rating={result.get('rating')} 👍")
        elif kind == "grocery":
            items = result.get("items", [])
            if not items:
                await msg.reply_text("Nothing to buy. 👍")
                return
            lines = [f"• {i['name']} — {i.get('quantity','')}{i.get('unit','')}"
                     for i in items]
            await msg.reply_text("🛒 Shopping list:\n" + "\n".join(lines))
        elif kind == "nas_ingest":
            moved = result.get("moved", [])
            pending = result.get("pending", [])
            if not moved and not pending:
                await msg.reply_text("📥 Inbox is empty (or NAS not enabled).")
                return
            lines = [f"  {os.path.basename(a)} → {b}" for a, b in moved]
            reply = f"📦 filed {result.get('count', len(moved))} item(s):\n" + "\n".join(lines)
            if pending:
                hold = [f"  {os.path.basename(p.get('path',''))} → "
                        f"{p.get('route',{}).get('dest','')} (needs confirmation)"
                        for p in pending]
                reply += "\n⏳ held for confirmation:\n" + "\n".join(hold)
            await msg.reply_text(reply)
        elif kind == "course_assignments":
            if not result.get("ok", True):
                await msg.reply_text(str(result.get("error", "no assignments")))
                return
            lines = []
            for code, res in (result.get("results") or {}).items():
                for item in res.get("created", []):
                    lines.append(f"{code} → task #{item['task_id']} {item['title']}")
            await msg.reply_text("\n".join(lines) or "No un-understood assignments found.")
        elif kind == "context":
            await msg.reply_text(self._context_text(result.get("snapshot", {})))
        elif kind == "proactive":
            messages = result.get("messages", [])
            if not messages:
                await msg.reply_text("All clear. 👍")
            else:
                await msg.reply_text("\n".join(messages))
        else:
            await msg.reply_text(str(result.get("text", result)))

    def _context_text(self, s: dict) -> str:
        out = []
        free = s.get("free_minutes_today", 0)
        hard = s.get("events_today", [])
        out.append(f"🕐 Free today: {free} min")
        if hard:
            fmt = time.strftime("%H:%M", time.localtime(h["start_ts"])) \
                if isinstance(h.get("start_ts", 0), (int, float)) and h.get("start_ts") \
                else h.get("start_ts", "")
            hard_txt = ", ".join(f"{h.get('title','')}@{fmt}" for h in hard)
            out.append(f"📅 Hard events: {hard_txt}")
        for c in s.get("courses", []):
            for d in c.get("upcoming_deadlines", []):
                out.append(f"📚 {c.get('code','')} — deadline {time.strftime('%a %d %b', time.localtime(d))}")
        returning = s.get("food_expiring", [])
        if returning:
            out.append("⏳ Expiring food:")
            out += [f"  • {f.get('name','')} ({f.get('days_left','')}d)" for f in returning]
        tasks = s.get("tasks", [])
        if tasks:
            out.append(f"✅ Active tasks: {len(tasks)}")
        return "\n".join(out) or "Nothing on my radar."

    def _backup_text(self, r: dict) -> str:
        last = r["last_backup"]
        return (
            f"last: {last['ts'] if last else 'never'}\n"
            f"dir : {r['backup_dir']}\n"
            f"free: {r['free_gb']}GB of {r['total_gb']}GB\n"
            f"trash: {r['trash_gb']}GB"
        )

    # ------------------------------------------------------------ confirm flow
    def _confirm_buttons(self, plan_id: str) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{plan_id}"),
             InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{plan_id}")]
        ])
        return kb

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        action, _, plan_id = data.partition(":")
        if action == "confirm":
            plan = self._pop_plan(plan_id)
            if not plan:
                await query.edit_message_text("Plan expired — please trigger again.")
                return
            try:
                plan.confirmed = True
                applied = self.container.organizer.apply_plan(
                    plan, user=update.effective_user.username or "user")
                text = self._apply_text(applied)
                await query.edit_message_text(f"✅ Applied.\n{text}")
            except Exception as exc:
                await query.edit_message_text(f"⚠️ {exc}")
        elif action == "cancel":
            self.pending.pop(plan_id, None)
            await query.edit_message_text("Cancelled — nothing was changed.")

    def _apply_text(self, applied: dict) -> str:
        lines = []
        for a, b in applied.get("moves", []):
            lines.append(f"  moved {os.path.basename(a)} → {b}")
        for p in applied.get("created", []):
            lines.append(f"  created {p}")
        for t in applied.get("trash", []):
            lines.append(f"  trashed {os.path.basename(t)}")
        return "\n".join(lines) or "nothing to report"

    def _pop_plan(self, plan_id: str):
        return self.pending.pop(plan_id, None)

    def _store_plan(self, plan: Any) -> str:
        key = plan.plan_id
        self.pending[key] = {"plan": plan}
        return key
