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
import re
import shutil
import time
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from .core import Container
from .context import describe_location, presence_battery
from .motivation import (parse_push_time, pending_push_topics, reward_line,
                         routing_for, suggest_reward, task_importance,
                         topic_key)
from .organizer import Plan

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


def _hm(minutes: int) -> str:
    minutes = max(0, min(minutes, 1439))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def msg_reply(message: Any, text: str, **kw: Any) -> Any:
    return await message.reply_text(text, **kw)


class TelegramBot:
    def __init__(self, container: Container):
        self.container = container
        self.app: Application | None = None
        # keep pending plans in memory: plan_id -> (plan, user)
        self.pending: dict[str, dict[str, Any]] = {}
        # topic awareness: ("?chat_id:thread_id") -> {"thread_id","chat_id",
        # "title","count","last_ts","last_user","preview"}
        self.topics: dict[str, dict[str, Any]] = {}
        # pending /settings edits, in-memory: topic_key -> {"routing","push_on",...}
        self._settings_store: dict[str, dict[str, Any]] = {}
        # pending course-URL suggestions (for inline verify buttons):
        # uid -> {"code","suggestions"}; plus a per-chat "await a typed URL"
        self._pending_urls: dict[str, dict[str, Any]] = {}
        self._url_seed = 0
        self._await_url: dict[int, str] = {}

    def build(self) -> Application:
        if not self.container.cfg.telegram_token:
            raise RuntimeError("Telegram token not configured (BUTLER_TELEGRAM_TOKEN / config.toml)")
        self.app = Application.builder().token(self.container.cfg.telegram_token).build()
        self.app.add_handler(CommandHandler("start", self.cmd_help))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("topics", self.cmd_topics))
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
        self.app.add_handler(CommandHandler("defer", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("block", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("cancel", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("resume", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("cameup", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("why", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("undo", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("calendar", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("where", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("around", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("briefing", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("brief", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("review", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("course", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("routine", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("settings", self.cmd_settings))
        self.app.add_handler(CommandHandler("rewards", self.cmd_rewards))
        self.app.add_handler(CommandHandler("reward", self.cmd_reward))
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
        cfg = self.container.cfg
        users = cfg.telegram_allowed_users
        uid = update.effective_user.id if update.effective_user else None
        if not users:
            # Deny-by-default: an empty allow-list means the bot is not
            # open. ``telegram_open_when_empty`` restores the legacy open mode.
            ok = bool(cfg.telegram_open_when_empty)
            log.info("telegram auth: uid=%s open_when_empty=%s -> %s",
                     uid, cfg.telegram_open_when_empty, ok)
            return ok
        ok = uid in users
        if not ok:
            log.warning("telegram auth DENY: uid=%s not in allowed_users=%s",
                        uid, users)
        return ok

    # ------------------------------------------------------------ topics
    def _track_topic(self, update: Update) -> None:
        """Record which forum topic this message came from."""
        try:
            msg = update.effective_message
            chat = update.effective_chat
            if not chat:
                return
            thread_id = getattr(msg, "message_thread_id", None)
            forum = bool(getattr(chat, "is_forum", False)) or \
                bool(getattr(msg, "is_topic_message", False))
            if not forum:
                return
            key = f"{chat.id}:{thread_id}"
            preview = (getattr(msg, "text", "") or "").strip().replace("\n", " ")[:60]
            rec = self.topics.setdefault(key, {
                "chat_id": chat.id, "thread_id": thread_id, "title": None,
                "count": 0, "last_ts": 0, "last_user": "", "preview": "",
            })
            rec["count"] += 1
            rec["last_ts"] = int(time.time())
            rec["last_user"] = getattr(
                update.effective_user, "username", None) or "user"
            rec["preview"] = preview
        except Exception as exc:
            log.warning("topic track error: %s", exc)

    def _topic_title(self, chat_id: int, thread_id: int | None) -> str:
        rec = self.topics.get(f"{int(chat_id)}:{int(thread_id or 0)}", {})
        return rec.get("title") or ""

    async def _resolve_topic_title(self, context: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, thread_id: int) -> str:
        try:
            info = await context.bot.get_forum_topic(chat_id, thread_id)
            return info.name if info else ""
        except Exception:
            return self._topic_title(chat_id, thread_id)

    # ------------------------------------------------- settings / rewards
    _ROUTING = ["", "chat", "day", "now", "tasks", "grocery", "food_list",
                "meal_plan", "find"]
    _ROUTING_LABEL = {"": "default (chat)", "chat": "chat", "day": "schedule",
                      "now": "what's now", "tasks": "task list",
                      "grocery": "groceries", "food_list": "pantry",
                      "meal_plan": "meal plan", "find": "find files"}
    _TIMES = ["06:30", "07:00", "07:30", "08:00", "09:00", "18:00", "20:00"]
    _FREQ = ["daily", "weekdays", "weekly"]

    # Kinds whose text reply should be composed by the AI from fresh data.
    # Kinds that need interactive inline buttons (course-url verify, recipe /
    # meal pickers, task quick-actions, plan confirm) are intentionally left out
    # so their buttons keep working; their *text* can be AI-composed separately.
    _AI_KINDS = {
        "course_list", "course_docs", "course_drop", "course_help",
        "course_check", "course_assignments",
        "now", "day", "review", "cameup", "briefing", "context",
        "plan_why",
    }

    def _settings_state(self, key: str) -> dict[str, Any]:
        return self._settings_store.get(key, {"routing": "", "push_on": 0,
                                              "push_time": "07:00",
                                              "push_freq": "daily"})

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        chat_id = update.effective_chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", 0)
        key = topic_key(chat_id, thread_id)
        title = await self._resolve_topic_title(context, chat_id, thread_id)
        row = self.container.db.topic_setting(chat_id, thread_id)
        state = {"routing": row["routing"] if row else "",
                 "push_on": int(row["push_on"]) if row else 0,
                 "push_time": (row["push_time"] or "07:00") if row else "07:00",
                 "push_freq": (row["push_freq"] or "daily") if row else "daily"}
        self._settings_store[key] = state
        self._set_topic_title(chat_id, thread_id, title)
        await msg_reply(update.effective_message, self._settings_text(key, title),
                        reply_markup=self._settings_markup(key))

    def _set_topic_title(self, chat_id: int, thread_id: int, title: str) -> None:
        if title:
            self.container.db.upsert_topic_setting(
                chat_id, thread_id, topic=title,
                routing=self._settings_state(topic_key(chat_id, thread_id)).get(
                    "routing", ""))

    def _settings_text(self, key: str, title: str = "") -> str:
        s = self._settings_state(key)
        lbl = self._ROUTING_LABEL.get(s["routing"], s["routing"] or "default")
        name = title or "Main chat"
        on = "on" if s["push_on"] else "off"
        return (f"⚙️ Settings for {name}\n\n"
                f"Free-text routing: {lbl}\n"
                f"Daily push: {on} · {s['push_time']} · {s['push_freq']}")

    def _settings_markup(self, key: str) -> InlineKeyboardMarkup:
        s = self._settings_state(key)
        rows = [
            [InlineKeyboardButton(
                f"🔁 Routing: {self._ROUTING_LABEL.get(s['routing'], 'default')}",
                callback_data=f"settings:routing")],
            [InlineKeyboardButton(
                f"🔔 Push: {'ON' if s['push_on'] else 'off'}",
                callback_data="settings:push"),
             InlineKeyboardButton(f"⏰ {s['push_time']}",
                                  callback_data="settings:time"),
             InlineKeyboardButton(s["push_freq"].title(),
                                  callback_data="settings:push_freq")],
            [InlineKeyboardButton("💾 Save & close",
                                  callback_data="settings:save")],
        ]
        return InlineKeyboardMarkup(rows)

    async def on_settings_cb(self, update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        chat_id = q.message.chat.id
        thread_id = getattr(q.message, "message_thread_id", 0)
        key = topic_key(chat_id, thread_id)
        s = self._settings_state(key)
        action = (q.data or "").split(":", 1)[-1]
        if action == "routing":
            i = self._ROUTING.index(s["routing"]) if s["routing"] in self._ROUTING else -1
            s["routing"] = self._ROUTING[(i + 1) % len(self._ROUTING)]
        elif action == "push":
            s["push_on"] = 0 if s["push_on"] else 1
        elif action == "time":
            i = self._TIMES.index(s["push_time"]) if s["push_time"] in self._TIMES else 1
            s["push_time"] = self._TIMES[(i + 1) % len(self._TIMES)]
        elif action == "push_freq":
            i = self._FREQ.index(s["push_freq"]) if s["push_freq"] in self._FREQ else 0
            s["push_freq"] = self._FREQ[(i + 1) % len(self._FREQ)]
        elif action == "save":
            ct, ti = topic_key(chat_id, thread_id).split(":")
            self.container.db.upsert_topic_setting(
                int(ct), int(ti), routing=s["routing"], push_on=s["push_on"],
                push_time=parse_push_time(s["push_time"]), push_freq=s["push_freq"],
                topic=self._topic_title(int(ct), int(ti)))
            await q.answer("Saved ✅")
            await q.message.edit_text("Saved. Routing + push updated for this topic.")
            return
        self._settings_store[key] = s
        await q.answer()
        title = await self._resolve_topic_title(context, chat_id, thread_id)
        await q.message.edit_text(self._settings_text(key, title),
                                  reply_markup=self._settings_markup(key))

    async def cmd_rewards(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        rows = list(self.container.db.rewards())
        if not rows:
            await msg_reply(update.effective_message, "No rewards in the library yet. "
                            "Add one with `/reward add <name>` or `/reward <kind> <name>`.")
            return
        lines = ["🎁 Reward library:"]
        for r in rows:
            badge = "🍬" if r["kind"] == "treat" else "🌿"
            lines.append(f"{badge} #{r['id']} {r['name']} (x{r['weight']}, used {r['times_used']})")
        lines.append("")
        lines.append("/reward add <name> [treat|rest]   ·   /reward <id> to claim   ·   /reward del <id>")
        await msg_reply(update.effective_message, "\n".join(lines))

    async def cmd_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        db = self.container.db
        args = (context.args or [])
        text = (update.message.text or "").strip()
        if not args:
            return await self.cmd_rewards(update, context)
        head = args[0].lower()
        if head == "add":
            rest = text.split(None, 2)[-1] if len(text.split(None, 2)) > 2 else ""
            kind = "rest"
            words = rest.split()
            if words and words[-1].lower() in ("treat", "rest", "treats", "snack", "food"):
                kind = "treat" if words[-1].lower() in ("treat", "treats", "snack", "food") else "rest"
                words = words[:-1]
            name = " ".join(words).strip() or rest
            if not name:
                await msg_reply(update.effective_message, "Usage: `/reward add <name> [treat|rest]`")
                return
            rid = db.add_reward(name, kind, weight=1 if kind == "rest" else 2)
            await msg_reply(update.effective_message,
                            f"🎁 Added reward #{rid}: {name} ({kind}).")
            return
        if head == "del":
            if len(args) < 2 or not args[1].isdigit():
                await msg_reply(update.effective_message, "Usage: `/reward del <id>`")
                return
            db.delete_reward(int(args[1]))
            await msg_reply(update.effective_message, f"🗑 Removed reward #{args[1]}.")
            return
        if args[0].isdigit():
            rid = int(args[0])
            r = db.reward_by_id(rid)
            if not r:
                await msg_reply(update.effective_message, f"No reward #{rid}.")
                return
            db.touch_reward(rid)
            msg = f"🎉 Enjoy: {r['name']} (#{rid})"
            if r["kind"] == "treat" and r["stock_food"]:
                stock = r["stock_food"]
                in_pantry = any(stock.lower() in str(f["name"]).lower()
                                for f in db.food())
                in_shopping = any(stock.lower() in str(s["name"]).lower()
                                  for s in db.shopping())
                if not in_pantry and not in_shopping:
                    db.add_shopping(stock)
                    msg += f" → added “{stock}” to your shopping list 🛒"
            await msg_reply(update.effective_message, msg)
            return
        await msg_reply(update.effective_message, "Usage: `/reward add <name> [treat|rest]` · `/reward <id>` · `/reward del <id>`")

    async def cmd_topics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            order = sorted(self.topics.values(), key=lambda t: -t["last_ts"])
            if not order:
                await update.effective_message.reply_text(
                    "No forum topics seen yet. Send a message in a topic.")
                return
            lines = ["🗂 Topics I've seen:"]
            for t in order[:25]:
                title = t.get("title")
                if title is None and t.get("chat_id") and t.get("thread_id"):
                    try:
                        info = await context.bot.get_forum_topic(
                            t["chat_id"], t["thread_id"])
                        title = info.name if info else None
                    except Exception:
                        title = None
                t["title"] = title
                name = title or f"topic #{t['thread_id']}"
                lines.append(f"• {name} — {t['count']} msg(s) · last: {t['last_user']}")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as exc:
            log.warning("topics error: %s", exc)
            await update.effective_message.reply_text(f"⚠️ {exc}")

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
        self._track_topic(update)
        cmd = (update.message.text or "").split()[0].lstrip("/").lower()
        arg = (update.message.text or "").partition(" ")[2].strip()
        await self._dispatch(update, f"/{cmd} {arg}", user=update.effective_user.username or "telegram")

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self._track_topic(update)
        message = update.message.text or ""
        chat_id = update.effective_chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", None)
        # Resolve (and cache) the topic title once so routing by topic name
        # works on free text without requiring a /settings visit.
        if thread_id and not self._topic_title(chat_id, thread_id):
            title = await self._resolve_topic_title(context, chat_id, thread_id)
            if title:
                rec = self.topics.get(f"{chat_id}:{thread_id}")
                if rec:
                    rec["title"] = title
        # If we're waiting for the user to supply a course website URL (via the
        # inline "✏️ Enter URL" button), capture a URL typed as free text.
        awaiting = self._await_url.get(chat_id)
        if awaiting:
            urlm = re.search(r"https?://\S+", message)
            if urlm and not message.lstrip().lower().startswith("/"):
                self._await_url.pop(chat_id, None)
                courses = getattr(self.container, "courses", None)
                res = (courses.set_url(awaiting, urlm.group(0))
                       if courses is not None
                       else {"ok": False, "error": "courses not configured"})
                if res.get("ok"):
                    await update.effective_message.reply_text(
                        f"✅ Tracking {awaiting} — I'll monitor {urlm.group(0)}.")
                else:
                    await update.effective_message.reply_text(
                        f"⚠️ {res.get('error', 'could not save that URL')}")
                return
        # Unmatched free text should be answered naturally, not with the help
        # menu. If the current topic has a configured routing command, answer
        # with that; otherwise fall back to natural chat.
        intent = self.container.decider.parse(message)
        if intent.kind == "help":
            from .decider import Intent as _I
            route = routing_for(self.container.db, chat_id, thread_id,
                                self._topic_title(chat_id, thread_id))
            if route == "settings":
                await self.cmd_settings(update, context)
                return
            if route:
                intent = _I(route, query=message, raw=message)
            else:
                intent = _I("chat", query=message, raw=message)
        await self._dispatch(update, message,
                             user=update.effective_user.username or "telegram",
                             intent=intent, via_agent=True)

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

    async def _dispatch(self, update: Update, message: str, user: str,
                        intent: Any = None, *, via_agent: bool = False) -> None:
        chat_id = update.effective_chat.id
        if intent is None:
            intent = self.container.decider.parse(message, channel="telegram")
        try:
            result = await self._resolve(intent, user, via_agent=via_agent)
        except Exception as exc:
            await update.effective_message.reply_text(f"⚠️ {exc}")
            return
        await self._render(update, intent, result)

    async def _resolve(self, intent: Any, user: str, *,
                       via_agent: bool = False) -> Any:
        """Resolve an intent, routing NL through the agent runtime when present.

        Slash commands keep using the decider directly; free text goes through
        the single agent control loop, whose decider bridge returns the exact
        same result object the old direct call did.
        """
        agent = getattr(self.container, "agent", None) if via_agent else None
        if agent is not None:
            try:
                reply = agent.run_intent(intent, user)
                return reply.data
            except Exception as exc:  # noqa: BLE001 — fall back, don't drop
                log.warning("agent run_intent failed, using decider: %s", exc)
        return self.container.decider.resolve(intent, user)

    async def _render(self, update: Update, intent: Any, result: Any) -> None:
        msg = update.effective_message
        kind = getattr(intent, "kind", None) or result.get("kind", "")
        if kind == "help":
            await msg.reply_text(result["text"])
            return
        if isinstance(result, Plan):  # a Plan — ask for confirmation (feature 12)
            plan = result
            self._store_plan(plan)
            await msg.reply_text(
                plan.describe(),
                reply_markup=self._confirm_buttons(plan.plan_id),
            )
            return
        if (kind in self._AI_KINDS and isinstance(result, dict)
                and not (kind == "course_add" and result.get("need_url"))):
            text = self.container.chat.respond_kind(kind, result)
            if text:
                await msg.reply_text(text)
                return
            # no LLM -> fall through to the deterministic template branch below
        if kind == "resume":
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
        elif kind == "week":
            lines = ["🗓 Week ahead"]
            for d in result.get("days", []):
                lines.append(f"\n{d.get('weekday', '')} {d.get('date', '')}")
                lines.append(d.get("text", ""))
            await msg.reply_text("\n".join(lines)[:3900])
        elif kind == "now":
            await msg.reply_text("▶️ " + result.get("answer", "Nothing to do."))
        elif kind == "plan_tasks":
            if result.get("error"):
                await msg.reply_text("⚠️ " + result["error"])
                return
            if getattr(intent, "kind", None) == "done" and result.get("ok"):
                await msg.reply_text(
                    f"✅ {result.get('title', 'Task')} done. "
                    f"{self._maybe_reward(result.get('task_id'))}")
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
                await msg.reply_text("\n".join(lines),
                                     reply_markup=self._task_buttons(tasks))
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
        elif kind == "course_help":
            await msg.reply_text(result.get("message", "I can help with courses."))
        elif kind == "course_add":
            code = result.get("code", "")
            if result.get("need_url"):
                name = ""
                courses = getattr(self.container, "courses", None)
                if courses is not None:
                    row = courses.course(code)
                    if row:
                        name = row.get("name", "") or ""
                await self._offer_course_urls(msg, code, name=name)
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
        elif kind == "course_drop":
            if result.get("ok") is False:
                await msg.reply_text("⚠️ " + (result.get("error", "I couldn't drop that course.")))
            else:
                await msg.reply_text(result.get("text", "Done."))
        elif kind == "course_check":
            updates = result.get("updates", [])
            if not updates:
                await msg.reply_text("No new course activity. 👍")
                return
            lines = [f"• {u.get('course_code','')} — {u.get('title','')} ({u.get('doc_type','')})"
                     for u in updates]
            await msg.reply_text("📚 New course activity:\n" + "\n".join(lines))
        elif kind == "course_docs":
            if result.get("ok") is False:
                await msg.reply_text("⚠️ " + result.get("error", "No materials yet."))
                return
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
                f"have {int(p['have_ratio']*100)}% of ingredients{ms}",
                reply_markup=self._food_buttons(result.get("recipe_id") or p.get("recipe_id")))
        elif kind == "meal_plan":
            p = result.get("plan", {})
            if not p.get("recipe"):
                await msg.reply_text(p.get("message", "No recipe fits."))
                return
            ms = f"\n     get: {', '.join(p['missing'])}" if p.get("missing") else ""
            await msg.reply_text(
                f"🍽 {p['meal']}: {p['recipe']} (~{p['time_minutes']}min, "
                f"{p['difficulty']}, ¥{p['cost']})\n"
                f"have {int(p['have_ratio']*100)}% of ingredients{ms}",
                reply_markup=self._food_buttons(result.get("recipe_id") or p.get("recipe_id")))
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
        elif kind == "meal_cook":
            if result.get("ok") is False:
                await msg.reply_text("⚠️ " + str(result.get("error", "couldn't do that.")))
                return
            await msg.reply_text(
                f"👨‍🍳 Cooking {result.get('meal', 'dinner')} later. "
                f"I'll keep a slot free if I can — nothing else changed. 👍")
        elif kind == "meal_another":
            p = result.get("plan", {})
            if not p.get("recipe"):
                await msg.reply_text(p.get("message", "No other recipe fits."))
                return
            ms = f"\n     get: {', '.join(p['missing'])}" if p.get("missing") else ""
            await msg.reply_text(
                f"🔁 {p['recipe']} (~{p['time_minutes']}min, {p['difficulty']}, ¥{p['cost']})\n"
                f"have {int(p['have_ratio']*100)}% of ingredients{ms}",
                reply_markup=self._food_buttons(result.get("recipe_id") or p.get("recipe_id")))
        elif kind == "meal_add_missing":
            if result.get("ok") is False:
                await msg.reply_text("⚠️ " + str(result.get("error", "couldn't do that.")))
                return
            added = result.get("added", [])
            if not added:
                await msg.reply_text("🛒 Nothing to add — all ingredients are already on the list. 👍")
                return
            await msg.reply_text("🛒 Added to shopping list:\n• "
                                 + "\n• ".join(added))
        elif kind == "meal_not_tonight":
            if result.get("ok") is False:
                await msg.reply_text("⚠️ " + str(result.get("error", "couldn't do that.")))
                return
            await msg.reply_text("🌙 Noted — I won't suggest that tonight. Nothing was changed.")
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
                for item in res.get("created", []) + res.get("updated", []):
                    action = "updated" if item in res.get("updated", []) else "created"
                    sched = item.get("schedule") or {}
                    slots = ", ".join(
                        f"{_hm(s['start_min'])}-{_hm(s['end_min'])}" for s in sched.get("slots", []))
                    line = f"{code} → {action} task #{item['task_id']} {item['title']}"
                    if slots:
                        line += f" · study {slots}"
                    if sched.get("conflict"):
                        line += (f" · ⚠ conflict: {sched.get('deficit')}m "
                                 f"short before deadline")
                    lines.append(line)
            await msg.reply_text("\n".join(lines) or "No un-understood assignments found.")
        elif kind == "context":
            await msg.reply_text(self._context_text(result.get("snapshot", {})))
        elif kind == "briefing":
            await self._reply_briefing(msg, result)
        elif kind == "review":
            await msg.reply_text(result.get("text", "Nothing to review yet."))
        elif kind == "proactive":
            messages = result.get("messages", [])
            if not messages:
                await msg.reply_text("All clear. 👍")
            else:
                await msg.reply_text("\n".join(messages))
        elif kind == "where_am_i":
            p = result.get("presence", {})
            await msg.reply_text("📍 " + describe_location(p) + presence_battery(p) + ".")
        elif kind == "around_me":
            p = result.get("presence", {})
            line = "📍 " + describe_location(p) + presence_battery(p) + "."
            events = result.get("events_today", [])
            if events:
                nearby = ", ".join(str(e.get("title", "event")) for e in events[:5])
                line += f"\n📅 today: {nearby}"
            await msg.reply_text(line)
        elif kind == "why_this":
            cand = result.get("candidate") or {}
            head = f"🤔 Why {cand.get('title', 'this')}?" if cand.get("title") else "🤔 Why this?"
            await msg.reply_text(head + "\n" + (result.get("reason", "") or ""))
        elif kind == "location_change":
            await msg.reply_text("📍 " + (result.get("answer", str(result))))
        elif kind == "move_block":
            await msg.reply_text("🔀 " + (result.get("answer", str(result))))
        elif kind == "timeline":
            await msg.reply_text(result.get("text", "Nothing recorded yet.") or
                                 "Nothing recorded yet.")
        elif kind == "routines":
            if result.get("ok") is False:
                await msg.reply_text("⚠️ " + (result.get("error", "I couldn't do that.")))
            else:
                await msg.reply_text(result.get("text", "Done."))
        else:
            await msg.reply_text(str(result.get("text", result)))

    def _context_text(self, s: dict) -> str:
        out = []
        free = s.get("free_minutes_today", 0)
        hard = s.get("events_today", [])
        out.append(f"🕐 Free today: {free} min")
        pres = s.get("presence", {})
        if pres.get("known"):
            zone = pres.get("zone") or ("home" if pres.get("status") == "home" else "away")
            bat = presence_battery(pres)
            out.append(f"📍 Presence: {pres.get('status', 'unknown')} ({zone}){bat}")
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
    def _task_buttons(self, tasks: list[dict]) -> InlineKeyboardMarkup:
        """Per-task lifecycle buttons behind the active-task list (Phase 5.0)."""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton("▶️ Start", callback_data=f"tact:start:{t['id']}"),
                InlineKeyboardButton("✅ Done", callback_data=f"tact:done:{t['id']}"),
                InlineKeyboardButton("⏸ Defer", callback_data=f"tact:defer:{t['id']}"),
                InlineKeyboardButton("⏭ Skip", callback_data=f"tact:skip:{t['id']}"),
                InlineKeyboardButton("✓ Cancel", callback_data=f"tact:cancel:{t['id']}"),
            ] for t in tasks
        ])
        return kb

    async def _reply_briefing(self, msg, result) -> None:
        """Render a daily briefing, attaching Block/Resume controls for the
        tasks it surfaced (Start/Done/Defer already exist on /tasks)."""
        text = result.get("text", "")
        if not text:
            await msg.reply_text("No briefing available yet.")
            return
        kb = None
        rec = result.get("recommendation")
        if rec and rec.get("title"):
            t = self._find_task(rec["title"])
            if t:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton("▶️ Start",
                                         callback_data=f"tact:start:{t['id']}"),
                    InlineKeyboardButton("⏸ Defer",
                                         callback_data=f"tact:defer:{t['id']}"),
                    InlineKeyboardButton("🪫 Block",
                                         callback_data=f"tact:block:{t['id']}"),
                ]])
        await msg.reply_text("📋 " + text, reply_markup=kb)

    def _find_task(self, title: str) -> dict | None:
        try:
            for r in self.container.db.tasks("active"):
                if (r["title"] or "").strip().lower() == title.strip().lower():
                    return dict(r)
        except Exception:  # noqa: BLE001
            return None
        return None

    def _confirm_buttons(self, plan_id: str) -> InlineKeyboardMarkup:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{plan_id}"),
             InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{plan_id}")]
        ])
        return kb

    def _food_buttons(self, recipe_id: Any) -> InlineKeyboardMarkup:
        rid = str(recipe_id or "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton("🍳 Cook this",
                                     callback_data=f"meal_cook:{rid}"),
                InlineKeyboardButton("🔁 Another option",
                                     callback_data=f"meal_another:{rid}"),
            ],
            [
                InlineKeyboardButton("🛒 Add missing",
                                     callback_data=f"meal_add_missing:{rid}"),
                InlineKeyboardButton("🌙 Not tonight",
                                     callback_data=f"meal_not_tonight:{rid}"),
            ],
        ])
        return kb

    def _maybe_reward(self, task_id: int | None) -> str:
        try:
            db = self.container.db
            if db.get_setting("rewards", "on") == "off":
                return ""
            minutes, priority = 0, 0
            if task_id:
                t = db.task_by_id(int(task_id))
                if t:
                    minutes = int(t["est_minutes"] or 0)
                    priority = int(t["priority"] or 0)
            r = suggest_reward(db, task_importance(minutes, priority))
            return reward_line(r)
        except Exception as exc:
            log.warning("reward suggestion error: %s", exc)
            return ""

    def _uid(self) -> str:
        self._url_seed += 1
        return f"c{self._url_seed}"

    def _course_url_kb(self, uid: str, sugg: list[dict[str, Any]]) -> InlineKeyboardMarkup:
        rows = []
        for i, s in enumerate(sugg):
            host = str(s["url"]).split("//")[-1].split("/")[0] or s["url"]
            dot = "🟢" if s.get("reachable") else "⚪"
            rows.append([InlineKeyboardButton(f"{dot} {host}",
                                              callback_data=f"courseurl:{uid}:{i}")])
        rows.append([
            InlineKeyboardButton("🔍 Search again", callback_data=f"courseurl:{uid}:r"),
            InlineKeyboardButton("✏️ Enter URL", callback_data=f"courseurl:{uid}:e"),
        ])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"courseurl:{uid}:x")])
        return InlineKeyboardMarkup(rows)

    async def _offer_course_urls(self, msg, code: str, name: str = "") -> None:
        courses = getattr(self.container, "courses", None)
        sugg: list[dict[str, Any]] = []
        if courses is not None:
            try:
                sugg = await asyncio.to_thread(courses.suggest_url, code, name)
            except Exception as exc:  # noqa: BLE001
                log.warning("course suggest failed: %s", exc)
                sugg = []
        if not sugg:
            await msg.reply_text(
                f"✅ Tracked {code}.\nSend me the course website URL (e.g. "
                f"`add course {code} https://...`) and I'll monitor it.")
            return
        uid = self._uid()
        self._pending_urls[uid] = {"code": code, "name": name, "suggestions": sugg}
        await msg.reply_text(
            f"✅ Tracked {code}. I found a few candidate sites — pick one:",
            reply_markup=self._course_url_kb(uid, sugg))

    async def on_course_url_cb(self, update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        parts = (query.data or "").split(":")
        if len(parts) < 3:
            await query.edit_message_text("⚠️ Unrecognised action.")
            return
        _, action, op = parts[0], parts[1], parts[2]
        # action is the "c<seed>" uid; parts[2] is the op (idx | r | e | x).
        uid = action
        pending = self._pending_urls.pop(uid, None)
        if not pending:
            await query.edit_message_text(
                "That suggestion expired — send `course add <code>` again.")
            return
        code = pending["code"]
        if op == "r":
            await self._offer_course_urls(update.effective_message, code,
                                          name=pending.get("name", ""))
            return
        if op == "e":
            self._await_url[update.effective_chat.id] = code
            await query.edit_message_text(
                f"Send the URL for {code}, e.g.: `course add {code} https://...`")
            return
        if op == "x":
            self._await_url.pop(update.effective_chat.id, None)
            await query.edit_message_text(
                f"OK, no URL saved. Say `course add {code}` whenever you're ready.")
            return
        try:
            s = pending["suggestions"][int(op)]
        except Exception:  # noqa: BLE001
            await query.edit_message_text("⚠️ Invalid option.")
            return
        courses = getattr(self.container, "courses", None)
        res = courses.set_url(code, s["url"]) if courses else {"ok": False,
                                                               "error": "courses not configured"}
        if res.get("ok"):
            await query.edit_message_text(f"✅ Tracking {code} — I'll monitor {s['url']}.")
        else:
            await query.edit_message_text(f"⚠️ {res.get('error', 'could not save that URL')}")

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        if not self._authorized(update):
            return
        data = query.data or ""
        # Callback validation: refuse anything that isn't a known shape, so a
        # forged/foreign callback_data can never trigger an action.
        if not re.match(r"^[a-z_]+:[a-z_:0-9-]+$", data):
            await query.edit_message_text("⚠️ Unrecognised action.")
            return
        if data.startswith("settings:"):
            await self.on_settings_cb(update, context)
            return
        if data.startswith("courseurl:"):
            await self.on_course_url_cb(update, context)
            return
        if data.startswith("tact:"):
            _pre, act, tid = data.split(":", 2)
            if not tid.isdigit():
                await query.edit_message_text("⚠️ Invalid task id.")
                return
            _slash = {"start": "begin", "done": "done", "defer": "defer",
                      "skip": "skip", "cancel": "cancel",
                      "block": "block", "resume": "resume"}.get(act, act)
            await self._dispatch(update, f"/{_slash} {tid}",
                                 user=update.effective_user.username or "telegram")
            return
        action, _, plan_id = data.partition(":")
        if action == "confirm":
            plan = self._pop_plan(plan_id)
            if not plan:
                await query.edit_message_text("Plan expired — please trigger again.")
                return
            user = update.effective_user.username or "user"
            # Safety gate: applying a consequential plan is a destructive
            # external effect; it must pass the deterministic policy before the
            # files move. The user tapping Confirm is the recorded consent.
            safety = getattr(self.container, "safety", None)
            if safety is not None:
                decision = safety.check("organize", actor=user,
                                        confirmed=True, target=plan.title)
                if not decision.allow:
                    await query.edit_message_text(
                        f"⛔ Refused: {decision.reason}")
                    return
            try:
                plan.confirmed = True
                applied = self.container.organizer.apply_plan(
                    plan, user=user)
                text = self._apply_text(applied)
                await query.edit_message_text(f"✅ Applied.\n{text}")
            except Exception as exc:
                await query.edit_message_text(f"⚠️ {exc}")
        elif action == "cancel":
            self.pending.pop(plan_id, None)
            await query.edit_message_text("Cancelled — nothing was changed.")
        elif action in ("meal_cook", "meal_another", "meal_add_missing",
                        "meal_not_tonight"):
            cmd = {"meal_cook": "cookthis", "meal_another": "another",
                   "meal_add_missing": "addmissing",
                   "meal_not_tonight": "nottonight"}[action]
            await self._dispatch(update, f"/{cmd} {plan_id}",
                                 user=update.effective_user.username or "telegram")

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
