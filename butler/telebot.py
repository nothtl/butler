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


async def reply_long(message: Any, text: str, **kw: Any) -> Any:
    """Send a possibly-long message in platform-safe chunks."""
    from .ux import chunk_text
    last = None
    for piece in chunk_text(text):
        last = await message.reply_text(piece, **kw)
    return last


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
        # N3: pending creation/link/organize proposals awaiting confirmation.
        self._pending_creation: dict[int, dict[str, Any]] = {}
        # N4 hotfix: pending topic setup proposals (keyed chat:thread).
        self._pending_topic: dict[str, dict[str, Any]] = {}

    async def _post_init(self, app: Any) -> None:
        """Reconcile topic control panels before polling starts (idempotent)."""
        try:
            await self.reconcile_topics(app.bot)
        except Exception as exc:  # noqa: BLE001 — never block startup
            log.warning("topic reconciliation failed: %s", exc)

    def build(self) -> Application:
        if not self.container.cfg.telegram_token:
            raise RuntimeError("Telegram token not configured (BUTLER_TELEGRAM_TOKEN / config.toml)")
        self.app = Application.builder().token(self.container.cfg.telegram_token).build()
        # N1: reconcile pinned topic panels once on startup (idempotent).
        self.app.post_init = self._post_init
        self.app.add_handler(CommandHandler("start", self.cmd_help))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("topics", self.cmd_topics))
        self.app.add_handler(CommandHandler("topic", self.cmd_topic))
        self.app.add_handler(CommandHandler("track", self.cmd_track))
        self.app.add_handler(CommandHandler("trackers", self.cmd_trackers))
        self.app.add_handler(CommandHandler("add", self.cmd_add))
        self.app.add_handler(CommandHandler("link", self.cmd_link))
        self.app.add_handler(CommandHandler("organize", self.cmd_organize))
        self.app.add_handler(CommandHandler("projects", self.cmd_projects))
        self.app.add_handler(CommandHandler("courses", self.cmd_courses))
        self.app.add_handler(CommandHandler("food", self.cmd_food))
        self.app.add_handler(CommandHandler("grocery", self.cmd_grocery))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("context", self.cmd_context))
        self.app.add_handler(CommandHandler("health", self.cmd_health))
        self.app.add_handler(CommandHandler("storage", self.cmd_storage))
        self.app.add_handler(CommandHandler("list", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("find", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("search", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("hybrid", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("ask", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("teach", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("resume", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("dupes", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("trash", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("recover", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("mkdir", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("index", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("backup", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("empty", self.cmd_slash_wrap))
        # Phase 2 planner commands
        self.app.add_handler(CommandHandler("day", self.cmd_slash_wrap))
        self.app.add_handler(CommandHandler("week", self.cmd_slash_wrap))
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
                f"Daily push: {on} · {s['push_time']} · {s['push_freq']}\n\n"
                + self._system_settings_text())

    def _system_settings_text(self) -> str:
        """A concise, read-only overview of the global configuration.

        Never shows secrets or dangerous low-level values — only whether a
        capability is on/off and the safe user-facing settings.
        """
        cfg = self.container.cfg
        def flag(v: bool) -> str:
            return "on" if v else "off"
        lines = ["System (read-only):"]
        ai = "on" if getattr(cfg, "llm_api_key", "") else "off (deterministic)"
        lines.append(f"  AI: {ai}")
        cal = "off"
        if getattr(cfg, "google_calendar_enabled", False):
            cal = "on"
        lines.append(f"  Calendar: {cal}")
        lines.append(f"  Web: {getattr(cfg, 'web_search_provider', 'offline')}")
        lines.append(f"  Memory: {flag(getattr(cfg, 'memory_enabled', True))}")
        lines.append(f"  Proactive: {flag(getattr(cfg, 'proactive_enabled', True))}")
        lines.append(f"  Scheduler: {flag(getattr(cfg, 'scheduler_enabled', True))}")
        qs = int(getattr(cfg, "notify_quiet_start", 0))
        qe = int(getattr(cfg, "notify_quiet_end", 0))
        if qs and qe:
            lines.append(f"  Quiet hours: {_hm(qs)}–{_hm(qe)}")
        else:
            lines.append("  Quiet hours: off")
        lines.append(f"  Work window: {_hm(int(getattr(cfg, 'sleep_end', 420)))}–"
                     f"{_hm(int(getattr(cfg, 'sleep_start', 1380)))}")
        try:
            overall = self.container.health.overall()
            lines.append(f"  Health: {overall}")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

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

    # ------------------------------------------------------- N1: topics
    def _topic_store(self) -> Any:
        return getattr(self.container, "topics", None)

    def _current_topic(self, update: Update) -> tuple[int, int]:
        chat_id = update.effective_chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", 0) or 0
        return int(chat_id), int(thread_id)

    def _topic_keyboard(self, thread_id: int) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        t = int(thread_id or 0)
        rows = [
            [InlineKeyboardButton("⚙ Settings", callback_data=f"topic:settings:{t}"),
             InlineKeyboardButton("🔗 Connections", callback_data=f"topic:connections:{t}"),
             InlineKeyboardButton("📋 Details", callback_data=f"topic:details:{t}")],
            [InlineKeyboardButton("📁 Storage", callback_data=f"topic:storage:{t}"),
             InlineKeyboardButton("🔄 Refresh", callback_data=f"topic:refresh:{t}")],
        ]
        return InlineKeyboardMarkup(rows)

    async def _publish_topic_panel(self, bot: Any, prof: Any,
                                   *, force: bool = False) -> dict[str, Any]:
        """Render + pin the control panel, editing in place when possible.

        The database is the source of truth; the Telegram pin is a projection.
        A content hash avoids unnecessary edits, and a missing/uneditable
        message is replaced (and re-pinned) without creating duplicates.
        """
        store = self._topic_store()
        lock = self._topic_lock(prof.chat_id, prof.thread_id)
        async with lock:
            fresh = store.get(prof.chat_id, prof.thread_id) or prof
            text, chash = store.render_panel(fresh)
            if not force and fresh.pin_content_hash == chash \
                    and fresh.pin_message_id:
                return {"ok": True, "skipped": True,
                        "message_id": fresh.pin_message_id}
            chat_id, thread_id = fresh.chat_id, fresh.thread_id
            msg_id = int(fresh.pin_message_id or 0)
            if msg_id:
                try:
                    await bot.edit_message_text(text=text, chat_id=chat_id,
                                                message_id=msg_id)
                except Exception as exc:  # noqa: BLE001 — replaced below
                    log.info("topic panel edit failed (%s); replacing", exc)
                    msg_id = 0
            if not msg_id:
                sent = await bot.send_message(chat_id=chat_id, text=text,
                                              message_thread_id=thread_id or None)
                msg_id = int(getattr(sent, "message_id", 0) or 0)
                if msg_id:
                    try:
                        await bot.pin_chat_message(chat_id=chat_id,
                                                   message_id=msg_id,
                                                   disable_notification=True)
                    except Exception as exc:  # noqa: BLE001 — pin is best effort
                        log.info("topic pin failed: %s", exc)
            store.update(fresh, pin_message_id=msg_id,
                         pin_content_hash=chash,
                         pin_message_version=int(fresh.pin_message_version or 0) + 1)
            return {"ok": True, "message_id": msg_id, "hash": chash}

    def _topic_lock(self, chat_id: int, thread_id: int) -> asyncio.Lock:
        if not hasattr(self, "_topic_locks"):
            self._topic_locks = {}
        key = self._pending_topic_key(chat_id, thread_id)
        lock = self._topic_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[key] = lock
        return lock

    def _pending_topic_key(self, chat_id: int, thread_id: int) -> str:
        return f"{int(chat_id)}:{int(thread_id)}"

    def _pending_topic_map(self) -> dict[str, dict[str, Any]]:
        if not hasattr(self, "_pending_topic"):
            self._pending_topic = {}
        return self._pending_topic

    def _setup_keyboard(self, thread_id: int) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        t = int(thread_id)
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Set up", callback_data=f"topic:setup:confirm:{t}"),
            InlineKeyboardButton("⚙ Customize",
                                 callback_data=f"topic:setup:customize:{t}"),
            InlineKeyboardButton("✕ Cancel", callback_data=f"topic:setup:cancel:{t}"),
        ]])

    def _setup_summary(self, pending: dict[str, Any]) -> str:
        from .topics import CAPABILITIES, CAP_LABELS, CAP_ICONS
        name = pending.get("name") or "New topic"
        lines = [f"Topic:\n  {name}", "",
                 "Purpose:", f"  {pending.get('purpose') or '(not set)'}"]
        desc = (pending.get("description") or "").strip()
        if desc and desc.lower() != (pending.get("purpose") or "").lower():
            lines += ["", "Description:", f"  {desc[:300]}"]
        caps = pending.get("capabilities") or {}
        enabled = [f"{CAP_ICONS.get('enabled')} {CAP_LABELS[c]}"
                   for c in CAPABILITIES if caps.get(c) == "enabled"]
        disabled = [f"{CAP_ICONS.get('disabled')} {CAP_LABELS[c]}"
                    for c in CAPABILITIES if caps.get(c) != "enabled"]
        lines += ["", "Suggested capabilities:"] + ["  " + x for x in enabled]
        if disabled:
            lines += ["  " + x for x in disabled]
        conns = pending.get("connection_lines") or []
        if conns:
            lines += ["", "Connected:"] + ["  " + x for x in conns]
        lines += ["", "Confirm to save and pin the control panel."]
        return "\n".join(lines)

    def _propose_topic_setup(self, update: Update, bot: Any, prof: Any,
                             text: str) -> dict[str, Any]:
        """Build a topic setup *proposal*; do not activate or pin yet."""
        store = self._topic_store()
        from .topics import interpret_purpose
        interp = interpret_purpose(text)
        # Name comes from the Telegram topic title, never the description.
        name = (prof.name or "").strip()
        links, ambiguous = store.resolve_links(name, interp["description"])
        conns = [f"• {l['target_type']}: {l.get('target_id') or ''}".strip()
                 for l in links]
        pending = {
            "chat_id": int(prof.chat_id), "thread_id": int(prof.thread_id),
            "name": name, "purpose": interp["purpose"],
            "description": interp["description"],
            "capabilities": interp["capabilities"], "links": links,
            "ambiguous": ambiguous, "connection_lines": conns,
        }
        self._pending_topic_map()[self._pending_topic_key(prof.chat_id,
                                                    prof.thread_id)] = pending
        return pending

    async def _apply_topic_setup(self, chat_id: int, thread_id: int,
                                 bot: Any) -> Any:
        store = self._topic_store()
        pending = self._pending_topic_map().get(self._pending_topic_key(chat_id,
                                                                  thread_id))
        prof = store.get(chat_id, thread_id)
        if prof is None:
            return None
        if pending:
            prof = store.update(
                prof, name=pending.get("name") or prof.name,
                purpose=pending.get("purpose") or prof.purpose,
                description=pending.get("description") or prof.description,
                capabilities=pending.get("capabilities") or prof.capabilities,
                status="active")
            for link in pending.get("links") or []:
                store.add_link(prof, link["target_type"], link["target_id"],
                               link["relation"], link["confidence"],
                               link["provenance"])
        else:
            prof = store.update(prof, status="active")
        prof = store.get(chat_id, thread_id)
        await self._publish_topic_panel(bot, prof, force=True)
        self._pending_topic_map().pop(self._pending_topic_key(chat_id, thread_id), None)
        return store.get(chat_id, thread_id)

    async def _configure_topic_from_text(self, update: Update, bot: Any,
                                         prof: Any, text: str) -> Any:
        """Legacy shim: propose (does not activate/pin)."""
        pending = self._propose_topic_setup(update, bot, prof, text)
        await update.effective_message.reply_text(
            self._setup_summary(pending),
            reply_markup=self._setup_keyboard(prof.thread_id))
        return prof

    async def cmd_topics(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            store = self._topic_store()
            profiles = store.list() if store is not None else []
            if not profiles:
                await update.effective_message.reply_text(
                    "No topics configured yet. Send a message in a forum topic "
                    "and tell me what it is for.")
                return
            lines = ["🗂 Topics:"]
            for p in profiles[:25]:
                icon = {"active": "🟢", "pending_setup": "🟡",
                        "paused": "⏸", "archived": "📦"}.get(p.status, "•")
                lines.append(f"{icon} {p.name or f'topic #{p.thread_id}'} — "
                             f"{p.purpose or 'needs setup'}")
            lines.append("\nUse /topic in a topic to open its control panel.")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as exc:
            log.warning("topics error: %s", exc)
            from .ux import friendly_error
            await update.effective_message.reply_text(
                f"⚠️ {friendly_error(exc)}")

    async def cmd_topic(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            chat_id, thread_id = self._current_topic(update)
            if not thread_id:
                await update.effective_message.reply_text(
                    "Open a forum topic and send /topic there.")
                return
            store = self._topic_store()
            prof, _ = store.ensure(chat_id, thread_id,
                                   await self._resolve_topic_title(
                                       context, chat_id, thread_id))
            if prof.status == "pending_setup":
                await update.effective_message.reply_text(
                    self._setup_prompt(prof))
                return
            text, _ = store.render_panel(prof)
            await update.effective_message.reply_text(
                text, reply_markup=self._topic_keyboard(thread_id))
        except Exception as exc:
            log.warning("topic error: %s", exc)
            from .ux import friendly_error
            await update.effective_message.reply_text(f"⚠️ {friendly_error(exc)}")

    def _setup_prompt(self, prof: Any) -> str:
        name = prof.name or "this topic"
        return (f"New topic detected: {name}.\n"
                "What is this topic for?\n\n"
                "Example: \"This is my CS188 course. Track homework, projects, "
                "deadlines and important announcements, and help me schedule "
                "the work.\"")

    async def reconcile_topics(self, bot: Any) -> dict[str, Any]:
        """Repair/refresh pinned panels on startup without duplicating them."""
        store = self._topic_store()
        if store is None:
            return {"ok": False, "reason": "topics unavailable"}
        repaired = updated = 0
        for prof in store.list(status="active"):
            if not prof.pin_message_id:
                res = await self._publish_topic_panel(bot, prof, force=True)
                repaired += 1 if res.get("message_id") else 0
                continue
            text, chash = store.render_panel(prof)
            if prof.pin_content_hash != chash:
                await self._publish_topic_panel(bot, prof, force=True)
                updated += 1
        return {"ok": True, "repaired": repaired, "updated": updated}

    async def on_topic_cb(self, update: Update,
                          context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(update):
            return
        data = query.data or ""
        if data.startswith("topic:setup:"):
            await self._on_topic_setup_cb(update, context)
            return
        if data.startswith("topic:capconfirm:") or \
                data.startswith("topic:capcancel:"):
            await self._on_topic_cap_cb(update, context)
            return
        parts = data.split(":")
        if len(parts) < 3:
            await query.edit_message_text("⚠️ Unrecognised action.")
            return
        _, action, thread_s = parts[0], parts[1], parts[2]
        try:
            thread_id = int(thread_s)
        except ValueError:
            await query.edit_message_text("⚠️ Invalid topic.")
            return
        chat_id = query.message.chat.id
        store = self._topic_store()
        prof = store.get(chat_id, thread_id)
        if prof is None:
            await query.edit_message_text("This topic is not configured yet.")
            return
        if action == "settings":
            await query.edit_message_text(
                store.settings_text(prof),
                reply_markup=self._topic_cap_keyboard(thread_id, prof))
        elif action == "connections":
            await query.edit_message_text(store.connections_text(prof))
        elif action == "details":
            await query.edit_message_text(store.details_text(prof))
        elif action == "storage":
            await query.edit_message_text(store.storage_text(prof))
        elif action == "refresh":
            await self._publish_topic_panel(context.bot, prof, force=True)
            await query.edit_message_text(store.render_panel(prof)[0])
        elif action == "cap":
            # topic:cap:<thread>:<capability>:<state>
            if len(parts) < 5:
                await query.edit_message_text("⚠️ Invalid capability.")
                return
            cap, state = parts[3], parts[4]
            prof = store.set_capability(prof, cap, state)
            await self._publish_topic_panel(context.bot, prof, force=True)
            await query.edit_message_text(
                store.settings_text(prof),
                reply_markup=self._topic_cap_keyboard(thread_id, prof))
        else:
            await query.edit_message_text("⚠️ Unrecognised action.")

    def _topic_cap_keyboard(self, thread_id: int, prof: Any) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from .topics import CAPABILITIES, CAP_LABELS, CAP_ICONS
        rows = []
        for cap in CAPABILITIES:
            state = prof.cap(cap)
            nxt = "disabled" if state == "enabled" else "enabled"
            rows.append([InlineKeyboardButton(
                f"{CAP_ICONS.get(state, '•')} {CAP_LABELS[cap]}",
                callback_data=f"topic:cap:{int(thread_id)}:{cap}:{nxt}")])
        rows.append([InlineKeyboardButton("🔄 Refresh panel",
                     callback_data=f"topic:refresh:{int(thread_id)}")])
        return InlineKeyboardMarkup(rows)

    def _setup_cap_keyboard(self, thread_id: int, pending: dict[str, Any]) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from .topics import CAPABILITIES, CAP_LABELS, CAP_ICONS
        caps = pending.get("capabilities") or {}
        rows = []
        for cap in CAPABILITIES:
            state = caps.get(cap, "disabled")
            nxt = "disabled" if state == "enabled" else "enabled"
            rows.append([InlineKeyboardButton(
                f"{CAP_ICONS.get(state, '•')} {CAP_LABELS[cap]}",
                callback_data=f"topic:setup:cap:{int(thread_id)}:{cap}:{nxt}")])
        rows.append([InlineKeyboardButton("💾 Save & set up",
                     callback_data=f"topic:setup:save:{int(thread_id)}"),
                     InlineKeyboardButton("✕ Cancel",
                     callback_data=f"topic:setup:cancel:{int(thread_id)}")])
        return InlineKeyboardMarkup(rows)

    def _pending_cap_map(self) -> dict[str, dict[str, Any]]:
        if not hasattr(self, "_pending_cap"):
            self._pending_cap = {}
        return self._pending_cap

    def _cap_proposal_keyboard(self, thread_id: int, cap: str, state: str) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        verb = "Enable" if state == "enabled" else "Disable"
        return InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ {verb}",
                callback_data=f"topic:capconfirm:{int(thread_id)}:{cap}:{state}"),
            InlineKeyboardButton(
                "✕ Cancel",
                callback_data=f"topic:capcancel:{int(thread_id)}"),
        ]])

    async def _on_topic_cap_cb(self, update: Update,
                               context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        data = query.data or ""
        parts = data.split(":")
        # topic:capconfirm:<thread>:<cap>:<state> | topic:capcancel:<thread>
        if len(parts) < 3:
            await query.edit_message_text("⚠️ Unrecognised action.")
            return
        sub = parts[1]
        try:
            thread_id = int(parts[2])
        except ValueError:
            await query.edit_message_text("⚠️ Invalid topic.")
            return
        chat_id = query.message.chat.id
        key = self._pending_topic_key(chat_id, thread_id)
        pending = self._pending_cap_map().pop(key, None)
        if pending is None:
            await query.edit_message_text(
                "That change expired. Please ask again.")
            return
        if sub == "capcancel":
            await query.edit_message_text("Cancelled — no changes made.")
            return
        if len(parts) < 5:
            await query.edit_message_text("⚠️ Invalid capability.")
            return
        cap, state = parts[3], parts[4]
        if (cap, state) != (pending.get("cap"), pending.get("state")):
            await query.edit_message_text(
                "That change expired. Please ask again.")
            return
        store = self._topic_store()
        prof = store.get(chat_id, thread_id)
        if prof is None or prof.status != "active":
            await query.edit_message_text("This topic isn't set up yet.")
            return
        prof = store.set_capability(prof, cap, state)
        await self._publish_topic_panel(context.bot, prof, force=True)
        from .topics import CAP_LABELS
        verb = "enabled" if state == "enabled" else "disabled"
        await query.edit_message_text(
            f"✅ {CAP_LABELS.get(cap, cap)} {verb} for "
            f"{prof.name or 'this topic'}. The control panel is updated.")

    async def _on_topic_setup_cb(self, update: Update,
                                 context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        data = query.data or ""
        parts = data.split(":")
        # topic:setup:<sub>:<thread>[:<cap>:<state>]
        if len(parts) < 4:
            await query.edit_message_text("⚠️ Unrecognised action.")
            return
        sub, thread_s = parts[2], parts[3]
        try:
            thread_id = int(thread_s)
        except ValueError:
            await query.edit_message_text("⚠️ Invalid topic.")
            return
        chat_id = query.message.chat.id
        store = self._topic_store()
        key = self._pending_topic_key(chat_id, thread_id)
        if sub == "cancel":
            prof = store.get(chat_id, thread_id)
            if prof is not None:
                store.update(prof, status="archived")
            self._pending_topic_map().pop(key, None)
            await query.edit_message_text("Cancelled — nothing was pinned.")
            return
        pending = self._pending_topic_map().get(key)
        if pending is None:
            await query.edit_message_text(
                "That setup expired. Send /topic to start again.")
            return
        if sub == "confirm" or sub == "save":
            prof = await self._apply_topic_setup(chat_id, thread_id, context.bot)
            name = (prof.name if prof else None) or "Topic"
            await query.edit_message_text(
                f"✅ {name} is set up and its control panel is pinned.")
            return
        if sub == "customize":
            await query.edit_message_text(
                self._setup_summary(pending),
                reply_markup=self._setup_cap_keyboard(thread_id, pending))
            return
        if sub == "cap":
            if len(parts) < 6:
                await query.edit_message_text("⚠️ Invalid capability.")
                return
            cap, state = parts[4], parts[5]
            caps = dict(pending.get("capabilities") or {})
            caps[cap] = state
            pending["capabilities"] = caps
            await query.edit_message_text(
                self._setup_summary(pending),
                reply_markup=self._setup_cap_keyboard(thread_id, pending))
            return
        await query.edit_message_text("⚠️ Unrecognised action.")

    # ------------------------------------------------------- N2: trackers
    def _tracker_ctx(self, update: Update) -> dict[str, Any]:
        chat_id = update.effective_chat.id
        thread_id = getattr(update.effective_message, "message_thread_id", 0) or 0
        return {"chat_id": int(chat_id), "thread_id": int(thread_id)}

    def _render_tracker_result(self, res: Any) -> str:
        from .agent.semantic import ResultStatus
        if res.status == ResultStatus.AMBIGUOUS:
            q = (res.missing_information or res.warnings
                 or ["What should I track?"])
            return "I need a bit more detail:\n• " + "\n• ".join(str(x) for x in q)
        if res.status == ResultStatus.NEEDS_CONFIRMATION:
            return "I've prepared that change — confirm it to apply."
        if res.status != ResultStatus.OK:
            return "I couldn't set that up: " + (res.error
                                                 or "; ".join(res.warnings)
                                                 or "unknown error")
        data = res.data or {}
        if "tracker" in data and "summary" in data:
            return "✅ Tracking created.\n" + str(data["summary"])
        if "trackers" in data:
            rows = data["trackers"]
            if not rows:
                return "You're not tracking anything yet."
            icons = {"active": "🟢", "paused": "⏸", "degraded": "🟡",
                     "error": "🔴", "pending": "🟡", "disabled": "⚪",
                     "archived": "📦"}
            lines = ["🔎 Trackers"]
            for t in rows[:25]:
                lines.append(f"{icons.get(t['state'], '•')} {t['name']} — "
                             f"{t['source']}")
            return "\n".join(lines)
        if "evaluation" in data:
            ev = data["evaluation"]
            verdict = "WOULD FIRE" if ev.get("fired") else "would not fire"
            return (f"Dry run: {verdict}.\nReason: {ev.get('reason', '')}\n"
                    "(no notification was sent)")
        if "explanation" in data:
            ex = data["explanation"]
            if isinstance(ex, dict) and ex.get("explanation"):
                return str(ex["explanation"])
            return str(ex)
        if "tracker" in data and "action" in data:
            return f"✅ Tracker {data['action']}."
        return "Done."

    async def _maybe_tracker_nl(self, update: Update, message: str) -> bool:
        """Route natural-language tracker requests through the executive layer."""
        try:
            from .agent.interpret import resolve_interpreter
            from .agent.semantic import ActionKind
            it = resolve_interpreter(self.container)
            req = it.interpret(message, topic=self._tracker_ctx(update))
            tracker_actions = {
                ActionKind.TRACKER_CREATE, ActionKind.TRACKER_LIST,
                ActionKind.TRACKER_QUERY, ActionKind.TRACKER_CONTROL,
                ActionKind.TRACKER_EVALUATE, ActionKind.TRACKER_EXPLAIN,
            }
            if req.action not in tracker_actions:
                return False
            from .agent.service import ExecutiveService
            svc = ExecutiveService(self.container)
            res = svc.ask(request=req, topic=self._tracker_ctx(update))
            await update.effective_message.reply_text(
                self._render_tracker_result(res))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("tracker NL dispatch failed: %s", exc)
            return False

    async def cmd_track(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        arg = (update.message.text or "").partition(" ")[2].strip()
        if not arg:
            await self.cmd_trackers(update, context)
            return
        await self._maybe_tracker_nl(update, f"track {arg}")

    async def cmd_trackers(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            from .agent.service import ExecutiveService
            svc = ExecutiveService(self.container)
            res = svc.ask(text="show my trackers",
                          topic=self._tracker_ctx(update))
            await update.effective_message.reply_text(
                self._render_tracker_result(res))
        except Exception as exc:
            log.warning("trackers error: %s", exc)
            from .ux import friendly_error
            await update.effective_message.reply_text(
                f"⚠️ {friendly_error(exc)}")


    # ------------------------------------------------------- N3: creation
    def _render_creation_result(self, res: Any) -> str:
        from .agent.semantic import ResultStatus
        data = res.data if isinstance(res.data, dict) else {}
        if res.status == ResultStatus.AMBIGUOUS:
            q = res.missing_information or res.warnings or ["Which one?"]
            return "I need a bit more detail:\n• " + "\n• ".join(str(x) for x in q)
        if res.status == ResultStatus.NEEDS_CONFIRMATION:
            prop = data.get("proposal") or {}
            lines = prop.get("preview") or []
            body = "\n".join("• " + str(x) for x in lines) if lines else \
                str(data.get("message", ""))
            return ("I'll:\n" + body + "\n\nConfirm?") if body else \
                "Prepared — confirm to apply."
        if res.status != ResultStatus.OK:
            return "I couldn't do that: " + (res.error
                                             or "; ".join(res.warnings)
                                             or "unknown error")
        return str(data.get("message") or data.get("summary") or "Done.")

    def _creation_buttons(self, res: Any) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from .agent.semantic import ResultStatus
        data = res.data if isinstance(res.data, dict) else {}
        if res.status == ResultStatus.NEEDS_CONFIRMATION:
            return InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Create", callback_data="create:confirm"),
                InlineKeyboardButton("✖️ Cancel", callback_data="create:cancel")]])
        if res.status == ResultStatus.AMBIGUOUS:
            cands = ((data.get("proposal") or {}).get("resolution") or {}).get(
                "candidates") or data.get("candidates") or []
            rows = [[InlineKeyboardButton(str(c.get("display") or c.get("name"))[:40],
                                          callback_data=f"create:choose:{i}")]
                    for i, c in enumerate(cands[:4])]
            return InlineKeyboardMarkup(rows) if rows else None
        return None

    async def _maybe_creation_nl(self, update: Update, message: str) -> bool:
        try:
            from .agent.interpret import resolve_interpreter
            from .agent.semantic import ActionKind
            it = resolve_interpreter(self.container)
            req = it.interpret(message, topic=self._tracker_ctx(update))
            creation_actions = {
                ActionKind.CREATE_ITEM, ActionKind.LINK_ITEMS,
                ActionKind.UPDATE_ITEM, ActionKind.ORGANIZE_ITEMS,
                ActionKind.PREVIEW_CREATION, ActionKind.RESOLVE_REFERENCE,
            }
            if req.action not in creation_actions:
                return False
            from .agent.service import ExecutiveService
            svc = ExecutiveService(self.container)
            res = svc.ask(request=req, topic=self._tracker_ctx(update))
            chat_id = update.effective_chat.id
            data = res.data if isinstance(res.data, dict) else {}
            if data.get("proposal") and (res.status.value in
                                         ("needs_confirmation", "ambiguous")):
                self._pending_creation[chat_id] = data["proposal"]
            await update.effective_message.reply_text(
                self._render_creation_result(res),
                reply_markup=self._creation_buttons(res))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("creation NL dispatch failed: %s", exc)
            return False

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        arg = (update.message.text or "").partition(" ")[2].strip()
        if not arg:
            await update.effective_message.reply_text(
                "What should I add? e.g. /add a CS188 project due Friday")
            return
        await self._maybe_creation_nl(update, f"add {arg}")

    async def cmd_link(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        arg = (update.message.text or "").partition(" ")[2].strip()
        if not arg:
            await update.effective_message.reply_text(
                "What should I link? e.g. /link this to CS188")
            return
        await self._maybe_creation_nl(update, f"link this to {arg}")

    async def cmd_organize(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        arg = (update.message.text or "").partition(" ")[2].strip()
        await self._maybe_creation_nl(update, f"organize {arg}".strip())

    # --------------------------------------------------- N4 NL routing
    async def _maybe_memory_nl(self, update: Update, message: str) -> bool:
        try:
            from .agent.interpret import resolve_interpreter
            from .agent.semantic import ActionKind
            it = resolve_interpreter(self.container)
            req = it.interpret(message, topic=self._tracker_ctx(update))
            memory_actions = {
                ActionKind.MEMORY_QUERY, ActionKind.MEMORY_SEARCH,
                ActionKind.MEMORY_EXPLAIN, ActionKind.MEMORY_LEARN,
                ActionKind.MEMORY_FORGET, ActionKind.MEMORY_CONFIRM,
                ActionKind.MEMORY_CORRECT,
            }
            if req.action not in memory_actions:
                return False
            from .agent.service import ExecutiveService
            svc = ExecutiveService(self.container)
            res = svc.ask(request=req, topic=self._tracker_ctx(update))
            await update.effective_message.reply_text(self._render_memory_result(res))
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("memory NL dispatch failed: %s", exc)
            return False

    def _render_memory_result(self, res: Any) -> str:
        from .agent.semantic import ResultStatus
        data = res.data if isinstance(res.data, dict) else {}
        if res.status == ResultStatus.AMBIGUOUS:
            return ("Which memory do you mean? " +
                    ", ".join(str(c.get("value", ""))[:40]
                              for c in (data.get("candidates") or [])[:4]))
        if res.status == ResultStatus.NEEDS_CONFIRMATION:
            return "Prepared — confirm to apply."
        if res.status != ResultStatus.OK:
            return "I couldn't do that: " + (res.error or "; ".join(res.warnings)
                                             or "unknown error")
        if "stored" in data:
            return "Remembered."
        if "forgotten" in data:
            return "Forgotten."
        rows = data.get("memories") or []
        if rows:
            lines = ["🧠 What I remember"]
            for m in rows[:10]:
                lines.append(f"• {m.get('value')}")
            return "\n".join(lines)
        if data.get("explanation"):
            return str(data["explanation"])
        return "Done."

    def _capability_change(self, low: str) -> tuple[str, str] | None:
        """Detect a capability + desired state in a settings phrase."""
        from .topics import CAPABILITIES
        words = {
            "web": "web", "internet": "web", "online": "web",
            "track": "tracking", "tracking": "tracking",
            "proactive": "proactive", "alert": "proactive",
            "notif": "proactive", "reminder": "reminders", "remind": "reminders",
            "schedul": "scheduling", "schedule": "scheduling",
            "memory": "memory", "remember": "memory",
            "file": "file_organization", "organis": "file_organization",
            "organiz": "file_organization",
            "plan": "planning", "know": "knowledge",
        }
        cap = next((v for k, v in words.items() if k in low), None)
        if cap is None or cap not in CAPABILITIES:
            return None
        off = bool(re.search(
            r"\b(disable|disabled|turn off|switch off|stop|don'?t|do not|no|off)\b",
            low))
        on = bool(re.search(
            r"\b(enable|enabled|turn on|switch on|allow|start|on)\b", low))
        if off:
            return cap, "disabled"
        if on:
            return cap, "enabled"
        return None

    async def _refresh_topic_panel(self, bot: Any, chat_id: int,
                                   thread_id: int) -> bool:
        store = self._topic_store()
        prof = store.get(chat_id, thread_id) if store else None
        if prof is None:
            return False
        await self._publish_topic_panel(bot, prof, force=True)
        return True

    async def _maybe_settings_nl(self, update: Update, message: str,
                                 context: Any = None) -> bool:
        try:
            from .agent.interpret import resolve_interpreter
            from .agent.semantic import ActionKind
            low = (message or "").lower()
            chat_id = update.effective_chat.id
            thread_id = getattr(update.effective_message,
                                "message_thread_id", 0) or 0
            bot = context.bot if context is not None \
                else getattr(update, "get_bot", lambda: None)()
            # Panel ownership: the bot updates its own pinned message.
            if thread_id and re.search(r"\b(pin|pinned|panel)\b", low) and \
                    re.search(r"\b(update|refresh|regenerate|edit)\b", low):
                ok = await self._refresh_topic_panel(bot, chat_id, thread_id)
                await update.effective_message.reply_text(
                    "✅ Control panel refreshed." if ok
                    else "There's no topic panel here yet.")
                return True
            it = resolve_interpreter(self.container)
            req = it.interpret(message, topic=self._tracker_ctx(update))
            change = self._capability_change(low)
            params = dict(getattr(req, "parameters", {}) or {})
            if not change and params.get("capability"):
                from .topics import CAPABILITIES, CAP_STATES
                cap = str(params.get("capability"))
                state = str(params.get("state") or "enabled")
                if cap in CAPABILITIES and state in CAP_STATES:
                    change = (cap, state)
            if req.action not in (ActionKind.SETTINGS_UPDATE,
                                  ActionKind.SETTINGS_VIEW) \
                    and not (req.action == ActionKind.UNKNOWN and change):
                return False
            if req.action == ActionKind.SETTINGS_VIEW:
                from .agent.service import ExecutiveService
                res = ExecutiveService(self.container).ask(
                    request=req, topic=self._tracker_ctx(update))
                data = res.data if isinstance(res.data, dict) else {}
                await update.effective_message.reply_text(
                    data.get("text") or self.container.settings.render())
                return True
            # SETTINGS_UPDATE: scope-aware (topic-first, global only when clear).
            scope_hint = str(params.get("scope") or "").lower()
            global_intent = scope_hint in ("global", "system", "account") or bool(
                re.search(
                    r"\b(global|globally|system|system-wide|everywhere|all topics|"
                    r"account|for butler)\b", low))
            if scope_hint in ("topic", "current_topic", "here"):
                global_intent = False
            if thread_id and change and not global_intent:
                store = self._topic_store()
                prof = store.get(chat_id, thread_id) if store else None
                if prof is None or prof.status != "active":
                    await update.effective_message.reply_text(
                        "This topic isn't set up yet — use /topic first.")
                    return True
                cap, state = change
                if cap == "web" and state == "enabled" \
                        and not getattr(self.container.cfg, "web_enabled", True):
                    await update.effective_message.reply_text(
                        "Web is disabled system-wide, so I can't enable it just "
                        "for this topic. Say \"enable web globally\" first.")
                    return True
                key = self._pending_topic_key(chat_id, thread_id)
                self._pending_cap_map()[key] = {"cap": cap, "state": state}
                from .topics import CAP_LABELS
                verb = "Enable" if state == "enabled" else "Disable"
                await update.effective_message.reply_text(
                    f"{verb} {CAP_LABELS.get(cap, cap)} for "
                    f"{prof.name or 'this topic'}?",
                    reply_markup=self._cap_proposal_keyboard(thread_id, cap, state))
                return True
            if req.action != ActionKind.SETTINGS_UPDATE:
                return False
            # global settings
            from .agent.service import ExecutiveService
            res = ExecutiveService(self.container).ask(
                request=req, topic=self._tracker_ctx(update))
            data = res.data if isinstance(res.data, dict) else {}
            if res.status.value == "needs_confirmation":
                await update.effective_message.reply_text(
                    data.get("summary") or "Prepared — confirm to apply.")
            else:
                await update.effective_message.reply_text(
                    data.get("summary") or "Settings updated.")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("settings NL dispatch failed: %s", exc)
            return False

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            text = self.container.settings.render()
            thread_id = getattr(update.effective_message,
                                "message_thread_id", 0)
            if thread_id:
                text += ("\n\nFor this topic's settings, open /topic and tap "
                         "⚙ Settings.")
            await update.effective_message.reply_text(text)
        except Exception as exc:
            from .ux import friendly_error
            await update.effective_message.reply_text(f"⚠️ {friendly_error(exc)}")

    async def on_creation_cb(self, update: Update,
                             context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self._authorized(update):
            return
        data = query.data or ""
        chat_id = query.message.chat.id
        prop = self._pending_creation.get(chat_id)
        if data == "create:cancel":
            self._pending_creation.pop(chat_id, None)
            await query.edit_message_text("Cancelled — nothing was changed.")
            return
        if prop is None:
            await query.edit_message_text("That proposal expired.")
            return
        from .creation import CreationService
        eng = getattr(self.container, "creation", None) or CreationService(
            self.container)
        if data == "create:confirm":
            res = eng.execute(prop, confirmed=True)
            self._pending_creation.pop(chat_id, None)
            await query.edit_message_text(str(res.get("message") or "Done."))
            return
        if data.startswith("create:choose:"):
            try:
                idx = int(data.split(":")[-1])
            except ValueError:
                await query.edit_message_text("⚠️ Invalid choice.")
                return
            cands = ((prop.get("resolution") or {}).get("candidates")
                     or prop.get("candidates") or [])
            if idx < 0 or idx >= len(cands):
                await query.edit_message_text("⚠️ Invalid choice.")
                return
            chosen = cands[idx]
            prop = dict(prop)
            prop["name"] = chosen.get("display") or chosen.get("name")
            prop["questions"] = []
            res = eng.execute(prop, confirmed=True)
            self._pending_creation.pop(chat_id, None)
            await query.edit_message_text(str(res.get("message") or "Done."))
            return
        await query.edit_message_text("⚠️ Unrecognised action.")

    # ------------------------------------------------------------ handlers
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await update.effective_message.reply_text(self._help_text())

    def _help_text(self) -> str:
        return (
            "BUTLER HELP\n\n"
            "Try saying:\n"
            "  \"What should I do today?\"\n"
            "  \"Track CS188 assignments.\"\n"
            "  \"Add this to my project.\"\n"
            "  \"Remember I prefer 2-hour work blocks.\"\n"
            "  \"What's low in my pantry?\"\n"
            "  \"Add milk to groceries.\"\n"
            "  \"Replan my week.\"\n\n"
            "Planning\n"
            "  /day      today's plan\n"
            "  /week     the week ahead\n"
            "  /now      what to do now\n"
            "  /tasks    your tasks\n"
            "  /projects your projects\n\n"
            "Knowledge\n"
            "  /add      add something\n"
            "  /link     connect information\n"
            "  /organize organize files\n\n"
            "Tracking\n"
            "  /track    tell me to watch something\n"
            "  /trackers what I'm watching\n\n"
            "Topics\n"
            "  /topics   your topics\n"
            "  /topic    this topic's panel\n\n"
            "Personal\n"
            "  /memory   what I remember\n"
            "  /settings how I behave\n"
            "  /context  what I know right now\n\n"
            "System\n"
            "  /health   subsystem status\n"
            "  /undo     undo the last change"
        )

    async def cmd_projects(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            rows = self.container.projects.list_projects()
            if not rows:
                await update.effective_message.reply_text("No projects yet.")
                return
            lines = ["📁 Projects"]
            for p in rows[:20]:
                lines.append(f"• {p['name']} — {p.get('status', '')} "
                             f"({p.get('remaining_minutes', 0)}m left)")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as exc:
            from .ux import friendly_error
            await update.effective_message.reply_text(f"⚠️ {friendly_error(exc)}")

    async def cmd_courses(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._dispatch(update, "show courses",
                             user=update.effective_user.username or "telegram")

    async def cmd_food(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._dispatch(update, "what do i have",
                             user=update.effective_user.username or "telegram")

    async def cmd_grocery(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._dispatch(update, "grocery list",
                             user=update.effective_user.username or "telegram")

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._maybe_memory_nl(update, "what do you remember about me")

    async def cmd_context(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._dispatch(update, "context",
                             user=update.effective_user.username or "telegram")

    async def cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            subs = self.container.health.subsystems()
            icons = {"HEALTHY": "🟢", "DEGRADED": "🟡", "UNAVAILABLE": "🔴",
                     "DISABLED": "⚪"}
            lines = [f"Butler {self.container.health.report().get('version', '')} — "
                     f"{self.container.health.overall()}", ""]
            for name, info in subs.items():
                lines.append(f"{icons.get(info['state'], '•')} "
                             f"{name.replace('_', ' ').title()}")
            await update.effective_message.reply_text("\n".join(lines))
        except Exception as exc:
            from .ux import friendly_error
            await update.effective_message.reply_text(f"⚠️ {friendly_error(exc)}")

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
        # --- N1: topic discovery / setup. A topic is a context/view, so the
        # first message in a new forum topic asks what it is for; the reply is
        # turned into a durable TopicProfile and a pinned control panel.
        if thread_id:
            store = self._topic_store()
            if store is not None:
                title = self._topic_title(chat_id, thread_id) or \
                    await self._resolve_topic_title(context, chat_id, thread_id)
                prof, created = store.ensure(chat_id, thread_id, title or "")
                is_command = message.lstrip().startswith("/")
                if not is_command and created:
                    await update.effective_message.reply_text(
                        self._setup_prompt(prof))
                    return
                if not is_command and prof.status == "pending_setup" \
                        and message.strip():
                    await self._configure_topic_from_text(
                        update, context.bot, prof, message)
                    return
                if prof.status == "active":
                    self.container._topic_context = store.context_text(prof)
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
        # Unmatched free text is answered naturally from live state. The topic
        # provides *context* (a relevance hint), not a hardcoded route. The old
        # per-topic default-routing remains only as a legacy fallback.
        if await self._maybe_tracker_nl(update, message):
            return
        if await self._maybe_creation_nl(update, message):
            return
        if await self._maybe_memory_nl(update, message):
            return
        if await self._maybe_settings_nl(update, message, context):
            return
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
        try:
            await self._dispatch(update, message,
                                 user=update.effective_user.username or "telegram",
                                 intent=intent, via_agent=True)
        finally:
            try:
                self.container._topic_context = ""
            except Exception:  # noqa: BLE001
                pass

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
            log.warning("dispatch failed: %s", exc, exc_info=True)
            from .ux import friendly_error
            await update.effective_message.reply_text(f"⚠️ {friendly_error(exc)}")
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
            await reply_long(msg, result["text"])
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
            await reply_long(msg, "📅 Today\n```\n" + result.get("text", "") + "\n```")
        elif kind == "week":
            lines = ["🗓 Week ahead"]
            for d in result.get("days", []):
                lines.append(f"\n{d.get('weekday', '')} {d.get('date', '')}")
                lines.append(d.get("text", ""))
            await reply_long(msg, "\n".join(lines))
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
            await reply_long(msg, result.get("text", "Nothing to review yet."))
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

    async def on_proactive_cb(self, update: Update,
                              context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle proactive notification buttons (validated shapes only).

        The callback carries only ``accept|snooze|dismiss|details`` plus the
        candidate key. Accepting records consent but never auto-executes a
        consequential action; the proposed action still goes through the normal
        safety/confirmation path.
        """
        query = update.callback_query
        data = query.data or ""
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.edit_message_text("⚠️ Unrecognised action.")
            return
        _, action, key = parts
        eng = getattr(self.container, "proactive_engine", None)
        if eng is None:
            await query.edit_message_text("⚠️ Proactive engine unavailable.")
            return
        if action == "details":
            ex = eng.explain(key)
            if not ex:
                await query.edit_message_text("This reminder has expired.")
                return
            ev = "\n".join(f"  • {e.get('kind')}: {e.get('value')}"
                           for e in ex.get("evidence", []))
            await query.edit_message_text(
                f"{ex['title']}\n\n{ex.get('summary', '')}\n\n"
                f"Evidence:\n{ev}\n\n{ex.get('why', '')}")
            return
        if action == "snooze":
            eng.snooze(key, 180)
            await query.edit_message_text("⏰ Snoozed for 3 hours.")
            return
        if action == "dismiss":
            eng.respond(key, "dismissed")
            await query.edit_message_text("✖️ Dismissed — I won't repeat it.")
            return
        if action == "accept":
            res = eng.respond(key, "accepted")
            cand = (res.get("candidate") or {})
            pa = cand.get("proposed_action") or {}
            desc = pa.get("action", "the recommendation")
            await query.edit_message_text(
                f"✅ Noted: {desc}.\nThis is a recommendation — apply it with "
                f"the normal confirmation flow so nothing changes silently.")
            return
        await query.edit_message_text("⚠️ Unrecognised action.")

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
        if data.startswith("pro:"):
            await self.on_proactive_cb(update, context)
            return
        if data.startswith("topic:"):
            await self.on_topic_cb(update, context)
            return
        if data.startswith("create:"):
            await self.on_creation_cb(update, context)
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
                from .ux import friendly_error
                await query.edit_message_text(
                    f"⚠️ {friendly_error(exc)}")
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
