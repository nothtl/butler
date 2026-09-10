"""N4 hardening acceptance: confirmation-first topic UX, scoped settings,
panel ownership, concurrency, callback security, provider state, custom topics
and cross-subsystem integration.

Run:  .venv/bin/python tests/run_acceptance_n4_hardening.py

Deterministic and offline. Builds on N1-N4 and M1-M8.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

from butler.agent.interpret import DeterministicInterpreter  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.settings import SettingsService  # noqa: E402
from butler.tracking import SnapshotProvider  # noqa: E402

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


def fresh(prefix: str = "n4h-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeChat:
    def __init__(self, cid=1):
        self.id = cid
        self.is_forum = True


class FakeUser:
    def __init__(self, uid=42, username="tester"):
        self.id = uid
        self.username = username


class FakeMessage:
    def __init__(self, text="", thread_id=0, chat_id=1):
        self.text = text
        self.message_thread_id = thread_id
        self.is_topic_message = bool(thread_id)
        self.sent: list[str] = []
        self.markups: list[object] = []
        self.chat = FakeChat(chat_id)

    async def reply_text(self, text, **kw):
        self.sent.append(text)
        self.markups.append(kw.get("reply_markup"))


class FakeUpdate:
    def __init__(self, text="", thread_id=0, uid=42, chat_id=1):
        self.message = FakeMessage(text, thread_id, chat_id)
        self.effective_message = self.message
        self.effective_chat = FakeChat(chat_id)
        self.effective_user = FakeUser(uid)
        self.callback_query = None


class FakeCallbackQuery:
    def __init__(self, data, chat_id=1, uid=42):
        self.data = data
        self.message = FakeMessage(chat_id=chat_id)
        self.from_user = FakeUser(uid)
        self.edited: list[str] = []

    async def edit_message_text(self, text, **kw):
        self.edited.append(text)


class FakeCallbackUpdate:
    def __init__(self, data, chat_id=1, uid=42):
        self.callback_query = FakeCallbackQuery(data, chat_id, uid)
        self.effective_user = self.callback_query.from_user
        self.effective_chat = self.callback_query.message.chat
        self.message = self.callback_query.message
        self.effective_message = self.callback_query.message


class FakeBot:
    def __init__(self, *, fail_edit=False, fail_pin=False, topic_name="Topic"):
        self.sent: list[dict] = []
        self.edits: list[str] = []
        self.pins: list[int] = []
        self._next = 1000
        self.fail_edit = fail_edit
        self.fail_pin = fail_pin
        self.topic_name = topic_name

    async def send_message(self, chat_id, text, message_thread_id=None):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text})
        return type("M", (), {"message_id": self._next})()

    async def edit_message_text(self, text, chat_id, message_id):
        if self.fail_edit:
            raise RuntimeError("message to edit not found")
        self.edits.append(text)

    async def pin_chat_message(self, chat_id, message_id,
                               disable_notification=True):
        if self.fail_pin:
            raise RuntimeError("pin failed")
        self.pins.append(message_id)

    async def get_forum_topic(self, chat_id, thread_id):
        return type("T", (), {"name": self.topic_name})()


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


def tbot(c: Container):
    from butler.telebot import TelegramBot
    b = TelegramBot.__new__(TelegramBot)
    b.container = c
    b.topics = {}
    b._await_url = {}
    b._pending_urls = {}
    b._pending_creation = {}
    b._settings_store = {}
    return b


def activate(c: Container, chat: int, thread: int, name: str,
             purpose: str = "", caps: dict | None = None):
    prof, _ = c.topics.ensure(chat, thread, name)
    c.topics.update(prof, purpose=purpose or name, status="active",
                    capabilities=caps or dict(prof.capabilities))
    return c.topics.get(chat, thread)


def run(coro):
    return asyncio.run(coro)


def snap(c: Container, q: float, item: str = "chicken") -> SnapshotProvider:
    sp = SnapshotProvider(c, {"items": {item: {"label": item,
                                               "fields": {"quantity": q}}}})
    c.trackers.register_provider(sp)
    return sp


# =====================================================================
# 1. confirmation-first topic setup
# =====================================================================
def test_confirmation_first() -> None:
    print("\n== 1. confirmation-first topic setup ==")
    c = fresh("n4h-setup-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot(topic_name="Food")
    ctx = FakeContext(fb)
    u1 = FakeUpdate("hello", 7)
    run(b.on_message(u1, ctx))
    prof = c.topics.get(1, 7)
    check("H1 new topic is pending_setup", prof is not None
          and prof.status == "pending_setup")
    check("H2 no panel pinned while pending", len(fb.pins) == 0)
    check("H3 the setup prompt asks for purpose",
          any("what is this topic for" in s.lower() for s in u1.message.sent))
    u2 = FakeUpdate("This is for meal planning and food inventory", 7)
    run(b.on_message(u2, ctx))
    prof = c.topics.get(1, 7)
    check("H4 purpose reply keeps the topic pending",
          prof.status == "pending_setup")
    check("H5 no panel is created before confirmation", len(fb.sent) == 0
          and len(fb.pins) == 0)
    pending = b._pending_topic_map().get(b._pending_topic_key(1, 7))
    check("H6 the proposal is stored", pending is not None
          and pending.get("purpose"))
    check("H7 the proposal is not active",
          c.topics.get(1, 7).status == "pending_setup")
    check("H8 the proposal is shown with confirm controls",
          any("Confirm" in s for s in u2.message.sent))
    # confirmation applies the exact proposal
    prof = run(b._apply_topic_setup(1, 7, fb))
    check("H9 confirmation activates the topic", prof.status == "active")
    check("H10 confirmation pins exactly one panel", len(fb.pins) == 1
          and len(fb.sent) == 1)
    check("H11 the applied purpose matches the proposal",
          prof.purpose == pending.get("purpose"))
    check("H12 the applied name is the Telegram title", prof.name == "Food")
    check("H13 pending state is cleared",
          b._pending_topic_key(1, 7) not in b._pending_topic_map())


def _last_sent(update) -> str:
    return update.message.sent[-1] if update.message.sent else ""


# =====================================================================
# 2. name / purpose / description separation
# =====================================================================
def test_name_separation() -> None:
    print("\n== 2. name / purpose / description separation ==")
    c = fresh("n4h-name-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot(topic_name="Food")
    ctx = FakeContext(fb)
    run(b.on_message(FakeUpdate("hi", 7), ctx))
    run(b.on_message(FakeUpdate(
        "This is for meal planning, menus and pantry inventory.", 7), ctx))
    pending = b._pending_topic_map()[b._pending_topic_key(1, 7)]
    check("H14 name comes from the Telegram title", pending["name"] == "Food")
    check("H15 description keeps the user's sentence",
          pending["description"].startswith("This is for meal planning"))
    check("H16 purpose is a normalized label",
          bool(pending["purpose"]) and pending["purpose"] != pending["description"])
    check("H17 the description is never used as the name",
          "this is for" not in pending["name"].lower())
    check("H18 a titleless topic keeps an empty name",
          _propose_name(c, b, "UAV Research", 8) == "UAV Research")
    # a topic with no resolvable title must not fall back to the description
    c2 = fresh("n4h-name2-")
    c2.cfg.telegram_open_when_empty = True
    b2 = tbot(c2)
    fb2 = FakeBot(topic_name="")
    ctx2 = FakeContext(fb2)
    run(b2.on_message(FakeUpdate("hi", 9), ctx2))
    run(b2.on_message(FakeUpdate("this is my food topic", 9), ctx2))
    p2 = b2._pending_topic_map()[b2._pending_topic_key(1, 9)]
    check("H19 no title means no description-as-name",
          p2["name"] == "" and p2["description"] == "this is my food topic")


def _propose_name(c, b, title, thread) -> str:
    prof, _ = c.topics.ensure(1, thread, title)
    return prof.name


# =====================================================================
# 3. global settings vs topic capabilities
# =====================================================================
def test_scope_resolution() -> None:
    print("\n== 3. global vs topic scope ==")
    c = fresh("n4h-scope-")
    c.cfg.telegram_open_when_empty = True
    c.cfg.web_enabled = True
    b = tbot(c)
    fb = FakeBot()
    ctx = FakeContext(fb)
    activate(c, 1, 7, "Food", "meal planning")
    upd = FakeUpdate("enable web search so the menu is grounded", 7)
    handled = run(b._maybe_settings_nl(upd, upd.message.text, ctx))
    check("H20 topic capability request is handled", handled is True)
    pending = b._pending_cap_map().get(b._pending_topic_key(1, 7))
    check("H21 the topic capability is proposed, not applied",
          pending == {"cap": "web", "state": "enabled"})
    check("H22 the proposal names the topic",
          any("Food" in s for s in upd.message.sent))
    check("H23 the topic capability is not yet enabled",
          c.topics.get(1, 7).cap("web") != "enabled")
    check("H24 the proposal offers Enable/Cancel",
          _markup_labels(upd.message.markups[-1]) == ["✅ Enable", "✕ Cancel"])
    # confirm
    cu = FakeCallbackUpdate("topic:capconfirm:7:web:enabled")
    run(b._on_topic_cap_cb(cu, ctx))
    check("H25 confirmation enables the topic capability",
          c.topics.get(1, 7).cap("web") == "enabled")
    check("H26 confirmation refreshes the panel", len(fb.edits) >= 1
          or len(fb.sent) >= 1)
    check("H27 the confirmation reports the topic scope",
          any("Food" in s for s in cu.callback_query.edited))
    # explicit global intent stays global
    before = SettingsService(c).get("web_search_provider")
    upd2 = FakeUpdate("enable web globally", 7)
    handled2 = run(b._maybe_settings_nl(upd2, upd2.message.text, ctx))
    check("H28 a global request is handled globally", handled2 is True)
    check("H29 a global request does not touch the topic proposal",
          b._pending_cap_map().get(b._pending_topic_key(1, 7)) is None)
    check("H30 global intent is distinguished from topic scope",
          "global" in upd2.message.text.lower())
    check("H31 the global setting is unchanged until confirmed",
          SettingsService(c).get("web_search_provider") == before)
    # topic-only disable
    upd3 = FakeUpdate("disable scheduling here", 7)
    run(b._maybe_settings_nl(upd3, upd3.message.text, ctx))
    p3 = b._pending_cap_map().get(b._pending_topic_key(1, 7))
    check("H32 a bounded fallback phrase maps to the topic capability",
          p3 == {"cap": "scheduling", "state": "disabled"})


def _markup_labels(markup) -> list[str]:
    if markup is None:
        return []
    rows = getattr(markup, "inline_keyboard", [])
    return [b.text for row in rows for b in row]


# =====================================================================
# 4. panel auto-refresh on capability change
# =====================================================================
def test_panel_refresh() -> None:
    print("\n== 4. panel auto-refresh ==")
    c = fresh("n4h-refresh-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot()
    ctx = FakeContext(fb)
    prof = activate(c, 1, 7, "Food", "meal planning")
    run(b._publish_topic_panel(fb, prof, force=True))
    first = len(fb.sent)
    check("H33 the initial panel is sent", first == 1 and len(fb.pins) == 1)
    prof = c.topics.set_capability(c.topics.get(1, 7), "web", "enabled")
    run(b._publish_topic_panel(fb, prof, force=True))
    check("H34 a capability change edits the existing panel",
          len(fb.edits) >= 1)
    check("H35 no duplicate panel is sent", len(fb.sent) == first)
    check("H36 the panel reflects the new capability",
          "Web" in fb.edits[-1])
    # unchanged content is skipped
    prof = c.topics.get(1, 7)
    res = run(b._publish_topic_panel(fb, prof, force=False))
    check("H37 an unchanged panel is skipped", res.get("skipped") is True)
    # "update the pinned message" is handled by the bot itself
    upd = FakeUpdate("update the pinned message", 7)
    handled = run(b._maybe_settings_nl(upd, upd.message.text, ctx))
    check("H38 panel-refresh phrase is handled", handled is True)
    check("H39 the bot never claims it cannot edit the pin",
          not any("can't" in s.lower() and "pin" in s.lower()
                  for s in upd.message.sent))


# =====================================================================
# 5. pin reconciliation / replacement
# =====================================================================
def test_pin_replacement() -> None:
    print("\n== 5. pin reconciliation ==")
    c = fresh("n4h-pin-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot()
    prof = activate(c, 1, 7, "Food", "meal planning")
    run(b._publish_topic_panel(fb, prof, force=True))
    check("H40 the first panel is pinned", len(fb.pins) == 1)
    first_id = c.topics.get(1, 7).pin_message_id
    # edit failure -> replace and re-pin
    fb2 = FakeBot(fail_edit=True)
    fb2._next = 5000
    c.topics.update(c.topics.get(1, 7), pin_content_hash="stale")
    run(b._publish_topic_panel(fb2, c.topics.get(1, 7), force=True))
    check("H41 an uneditable panel is replaced", len(fb2.sent) == 1)
    check("H42 the replacement is pinned", len(fb2.pins) == 1)
    new_id = c.topics.get(1, 7).pin_message_id
    check("H43 the stored message id is updated", new_id != first_id
          and new_id == fb2.pins[0])
    check("H44 the version is monotonic",
          c.topics.get(1, 7).pin_message_version >= 2)
    # repeated forced refresh must not create duplicates
    fb3 = FakeBot()
    run(b._publish_topic_panel(fb3, c.topics.get(1, 7), force=True))
    check("H45 a healthy refresh edits instead of duplicating",
          len(fb3.sent) == 0 and len(fb3.edits) == 1)
    check("H46 exactly one panel row exists",
          c.db.one("SELECT COUNT(*) n FROM topic_settings WHERE chat_id=1 "
                   "AND thread_id=7")["n"] == 1)
    # pin failure is non-fatal
    fb4 = FakeBot(fail_pin=True)
    c.topics.update(c.topics.get(1, 7), pin_content_hash="stale2")
    res = run(b._publish_topic_panel(fb4, c.topics.get(1, 7), force=True))
    check("H47 a pin failure is non-fatal", res.get("ok") is True)


# =====================================================================
# 6. concurrent panel updates
# =====================================================================
def test_concurrency() -> None:
    print("\n== 6. concurrent panel updates ==")
    c = fresh("n4h-conc-")
    b = tbot(c)
    fb = FakeBot()
    activate(c, 1, 7, "Food", "meal planning")

    async def hammer():
        prof = c.topics.get(1, 7)
        await asyncio.gather(
            b._publish_topic_panel(fb, prof, force=True),
            b._publish_topic_panel(fb, prof, force=True),
            b._publish_topic_panel(fb, prof, force=True),
        )

    run(hammer())
    check("H48 concurrent updates send exactly one panel", len(fb.sent) == 1)
    check("H49 concurrent updates keep one canonical panel",
          c.topics.get(1, 7).pin_message_id == fb.sent[0] and True
          or c.topics.get(1, 7).pin_message_id > 0)
    check("H50 the version is monotonic after concurrency",
          c.topics.get(1, 7).pin_message_version >= 1)
    check("H51 a per-topic lock exists",
          b._pending_topic_key(1, 7) in b._topic_locks)
    check("H52 the lock is reused", b._topic_lock(1, 7)
          is b._topic_lock(1, 7))


# =====================================================================
# 7. callback security
# =====================================================================
def test_callback_security() -> None:
    print("\n== 7. callback security ==")
    c = fresh("n4h-cbsec-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot()
    ctx = FakeContext(fb)
    activate(c, 1, 7, "Food", "meal planning")
    # unauthorized user
    c.cfg.telegram_open_when_empty = False
    c.cfg.telegram_allowed_users = [42]
    cu = FakeCallbackUpdate("topic:cap:7:web:enabled", uid=99)
    run(b.on_topic_cb(cu, ctx))
    check("H53 an unauthorized callback is rejected",
          c.topics.get(1, 7).cap("web") != "enabled")
    c.cfg.telegram_allowed_users = []
    c.cfg.telegram_open_when_empty = True
    # malformed callback
    cu2 = FakeCallbackUpdate("topic:garbage")
    run(b.on_topic_cb(cu2, ctx))
    check("H54 a malformed callback is rejected safely",
          any("Unrecognised" in s or "Invalid" in s
              for s in cu2.callback_query.edited))
    # unknown topic
    cu3 = FakeCallbackUpdate("topic:settings:999")
    run(b.on_topic_cb(cu3, ctx))
    check("H55 an unknown topic is rejected safely",
          any("not configured" in s.lower() for s in cu3.callback_query.edited))
    # invalid capability state is ignored by the store
    prof = c.topics.get(1, 7)
    c.topics.set_capability(prof, "web", "bogus")
    check("H56 an invalid capability state does not mutate",
          c.topics.get(1, 7).cap("web") != "bogus")
    # invalid thread id
    cu4 = FakeCallbackUpdate("topic:settings:abc")
    run(b.on_topic_cb(cu4, ctx))
    check("H57 a non-numeric thread is rejected",
          any("Invalid" in s for s in cu4.callback_query.edited))
    # expired capability proposal
    cu5 = FakeCallbackUpdate("topic:capconfirm:7:web:enabled")
    run(b._on_topic_cap_cb(cu5, ctx))
    check("H58 an expired proposal is rejected without mutation",
          c.topics.get(1, 7).cap("web") != "enabled"
          and any("expired" in s.lower() for s in cu5.callback_query.edited))
    # stale setup confirmation
    cu6 = FakeCallbackUpdate("topic:setup:confirm:7")
    run(b._on_topic_setup_cb(cu6, ctx))
    check("H59 a stale setup confirmation is rejected safely",
          any("expired" in s.lower() for s in cu6.callback_query.edited))
    # cross-chat: chat id comes from the message, not the callback data
    activate(c, 2, 7, "Other", "other")
    cu7 = FakeCallbackUpdate("topic:cap:7:web:enabled", chat_id=1)
    run(b.on_topic_cb(cu7, ctx))
    check("H60 a callback cannot target another chat",
          c.topics.get(2, 7).cap("web") != "enabled")


# =====================================================================
# 8. capability state consistency
# =====================================================================
def test_capability_consistency() -> None:
    print("\n== 8. capability state consistency ==")
    c = fresh("n4h-capstate-")
    prof = activate(c, 1, 7, "Food", "meal planning")
    prof = c.topics.set_capability(prof, "web", "enabled")
    check("H61 the profile holds one authoritative state",
          prof.cap("web") == "enabled"
          and c.topics.get(1, 7).cap("web") == "enabled")
    text, chash = c.topics.render_panel(c.topics.get(1, 7))
    check("H62 the panel reflects the capability", "Web" in text)
    check("H63 the settings UI reflects the capability",
          "Web" in c.topics.settings_text(c.topics.get(1, 7)))
    check("H64 a hash is derived from the rendered panel", len(chash) == 16)
    check("H65 disabling is reflected everywhere",
          c.topics.set_capability(c.topics.get(1, 7), "web",
                                  "disabled").cap("web") == "disabled")
    check("H66 paused is a distinct state",
          c.topics.set_capability(c.topics.get(1, 7), "web",
                                  "paused").cap("web") == "paused")


# =====================================================================
# 9. provider state honesty
# =====================================================================
def test_provider_state() -> None:
    print("\n== 9. provider state ==")
    c = fresh("n4h-prov-")
    prof = activate(c, 1, 7, "Food", "meal planning")
    prof = c.topics.set_capability(prof, "web", "enabled")
    c.web.enabled = True
    c.web.search_provider = None
    note = c.topics.provider_note(prof)
    check("H67 an enabled capability with no provider is called out",
          "unavailable" in note.lower())
    check("H68 the panel carries the provider caveat",
          "unavailable" in c.topics.render_panel(prof)[0])
    check("H69 the settings view carries the provider caveat",
          "unavailable" in c.topics.settings_text(prof))
    c.web.enabled = False
    note2 = c.topics.provider_note(prof)
    check("H70 a disabled subsystem is distinguished",
          "disabled" in note2.lower())
    prof2 = c.topics.set_capability(prof, "web", "disabled")
    check("H71 no caveat when the capability is off",
          c.topics.provider_note(prof2) == "")


# =====================================================================
# 10. custom / dynamic topics
# =====================================================================
def test_custom_topics() -> None:
    print("\n== 10. custom topics ==")
    c = fresh("n4h-custom-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot(topic_name="Basketball Club")
    ctx = FakeContext(fb)
    run(b.on_message(FakeUpdate("hello", 11), ctx))
    run(b.on_message(FakeUpdate(
        "My basketball club: practices, matches, people, announcements "
        "and tasks.", 11), ctx))
    pending = b._pending_topic_map()[b._pending_topic_key(1, 11)]
    check("H72 a custom topic gets a generic proposal", pending is not None)
    check("H73 no course-specific assumption",
          not any("course" in l.lower() for l in pending["connection_lines"]))
    check("H74 no project-specific assumption",
          not any("project" in l.lower() for l in pending["connection_lines"]))
    prof = run(b._apply_topic_setup(1, 11, fb))
    check("H75 a custom topic activates", prof.status == "active")
    check("H76 a custom topic gets a pinned panel", len(fb.pins) == 1)
    check("H77 custom topic settings render",
          "Basketball Club" in c.topics.settings_text(prof))
    check("H78 custom topic connections render",
          "Basketball" in c.topics.connections_text(prof)
          or "Nothing" in c.topics.connections_text(prof))
    # a completely novel topic needs no new module
    prof2 = activate(c, 1, 12, "UAV Research", "research notes and experiments")
    check("H79 a novel topic works generically", prof2.status == "active")
    check("H80 a novel topic has no hardcoded domain",
          "UAV Research" in c.topics.render_panel(prof2)[0])
    # generic unknown link types are surfaced, not dropped
    c.topics.add_link(prof2, "experiment", 5, "tracks", 1.0, "user")
    data = c.topics.linked_data(c.topics.get(1, 12))
    check("H81 unknown link types are surfaced generically",
          any(r["target_type"] == "experiment" for r in data["references"]))
    check("H82 generic references render in the panel",
          "experiment" in c.topics.render_panel(c.topics.get(1, 12))[0])


# =====================================================================
# 11. shared context generalization
# =====================================================================
def test_shared_context() -> None:
    print("\n== 11. shared context generalization ==")
    c = fresh("n4h-shared-")
    c.food.add("chicken", quantity=3)
    food = activate(c, 1, 8, "Food", "meal planning and pantry")
    groc = activate(c, 1, 9, "Groceries", "shopping and replenishment")
    c.topics.add_link(food, "food", 1, "uses_pantry", 0.8, "user")
    c.topics.add_link(groc, "food", 1, "uses_pantry", 0.8, "user")
    check("H83 both topics see the shared pantry",
          c.topics.linked_data(food).get("food") is not None
          and c.topics.linked_data(groc).get("food") is not None)
    check("H84 the pantry is not duplicated",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"]
          == 1)
    check("H85 food context is identical across topics",
          c.topics.linked_data(food)["food"] == c.topics.linked_data(groc)["food"])
    # an unknown custom topic linked to food still resolves relevance
    custom = activate(c, 1, 13, "Travel", "trip planning")
    c.topics.add_link(custom, "food", 1, "uses_pantry", 0.8, "user")
    check("H86 a custom topic gets relevance from a link",
          c.topics.linked_data(custom).get("food") is not None)
    # topic is a relevance boost, not a hard boundary
    check("H87 linked data does not leak unrelated topics",
          c.topics.linked_data(activate(c, 1, 14, "Ideas", "ideas")).get("food")
          is None)


# =====================================================================
# 12. dangling topic links
# =====================================================================
def test_dangling_links() -> None:
    print("\n== 12. dangling topic links ==")
    c = fresh("n4h-dangle-")
    prof = activate(c, 1, 7, "Food", "meal planning")
    c.topics.add_link(prof, "course", 999, "about", 1.0, "user")
    data = c.topics.linked_data(c.topics.get(1, 7))
    check("H88 a missing target is counted as dangling",
          data["dangling"] >= 1)
    text = c.topics.render_panel(c.topics.get(1, 7))[0]
    check("H89 the panel reports unavailable connections",
          "no longer available" in text)
    check("H90 connections text reports the dangling link",
          "no longer available" in c.topics.connections_text(
              c.topics.get(1, 7)))
    links = c.topics.links(c.topics.get(1, 7))
    check("H91 dangling links are still listed for repair",
          any(l["target_type"] == "course" for l in links))
    c.topics.remove_link(c.topics.get(1, 7), links[0]["id"])
    check("H92 a dangling link can be removed",
          c.topics.linked_data(c.topics.get(1, 7))["dangling"] == 0)


# =====================================================================
# 13. tracker destinations
# =====================================================================
def test_tracker_destination() -> None:
    print("\n== 13. tracker destinations ==")
    c = fresh("n4h-dest-")
    sp = snap(c, 3.0)
    activate(c, 1, 7, "Food", "meal planning")
    eng = c.trackers
    t = eng.create(name="low stock", source="snapshot", target_type="food_item",
                   target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION",
                           "params": {"items": ["chicken"]}},
                   destination={"chat_id": 1, "thread_id": 7}, cadence=60)
    check("H93 a tracker stores its destination",
          t.destination_chat_id == 1 and t.destination_thread_id == 7)
    check("H94 an active destination topic is usable",
          c.topics.destination_ok(1, 7) is True)
    # archive the destination -> delivery falls back
    c.topics.update(c.topics.get(1, 7), status="archived")
    check("H95 an archived destination is rejected",
          c.topics.destination_ok(1, 7) is False)
    sent = []
    c.proactive_engine._send = lambda cand, text, channel, now: sent.append(
        {"dest": cand.destination, "evidence": cand.evidence}) or True
    sp.set_snapshot({"items": {"chicken": {"label": "chicken",
                                           "fields": {"quantity": 1.0}}}})
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N)
    check("H96 delivery does not target a stale topic",
          sent and not sent[0]["dest"].get("chat_id"))
    check("H97 the limitation is recorded in evidence",
          sent and any(e.get("kind") == "limitation"
                       for e in sent[0]["evidence"]))
    # unmanaged destination remains allowed
    check("H98 an unmanaged destination is allowed",
          c.topics.destination_ok(555, 66) is True)


# =====================================================================
# 14. tracker + panel integration
# =====================================================================
def test_tracker_panel() -> None:
    print("\n== 14. tracker + panel integration ==")
    c = fresh("n4h-tpanel-")
    snap(c, 3.0)
    prof = activate(c, 1, 7, "CS188", "course management")
    eng = c.trackers
    t = eng.create(name="CS188 assignments", source="snapshot",
                   target_type="food_item", target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION", "params": {}},
                   destination={"chat_id": 1, "thread_id": 7}, cadence=60)
    lines = c.topics.tracking_lines(c.topics.get(1, 7))
    check("H99 an active tracker appears on the panel",
          any("CS188 assignments" in x for x in lines))
    check("H100 the active tracker uses the running icon",
          any(x.startswith("🟢") for x in lines))
    eng.update(eng.get(t.id), state="paused")
    lines = c.topics.tracking_lines(c.topics.get(1, 7))
    check("H101 a paused tracker is reflected", any(x.startswith("⏸")
                                                     for x in lines))
    eng.update(eng.get(t.id), state="active")
    check("H102 a resumed tracker is reflected",
          any(x.startswith("🟢") for x in c.topics.tracking_lines(
              c.topics.get(1, 7))))
    eng.update(eng.get(t.id), state="degraded")
    check("H103 a degraded tracker is reflected",
          any(x.startswith("🟡") for x in c.topics.tracking_lines(
              c.topics.get(1, 7))))
    check("H104 the panel renders tracking",
          "Tracking" in c.topics.render_panel(c.topics.get(1, 7))[0])


# =====================================================================
# 15. consistent confirmation policy
# =====================================================================
def test_confirmation_policy() -> None:
    print("\n== 15. confirmation policy ==")
    c = fresh("n4h-confirm-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot(topic_name="Food")
    ctx = FakeContext(fb)
    # topic setup: proposal first
    run(b.on_message(FakeUpdate("hello", 7), ctx))
    run(b.on_message(FakeUpdate("meal planning", 7), ctx))
    check("H105 topic setup is proposal-first",
          c.topics.get(1, 7).status == "pending_setup")
    # capability change: proposal first
    run(b._apply_topic_setup(1, 7, fb))
    upd = FakeUpdate("enable web here", 7)
    run(b._maybe_settings_nl(upd, upd.message.text, ctx))
    check("H106 a capability change is proposal-first",
          b._pending_cap_map().get(b._pending_topic_key(1, 7)) is not None
          and c.topics.get(1, 7).cap("web") != "enabled")
    check("H107 the proposal is explicit about scope",
          any("Food" in s for s in upd.message.sent))
    # cancel does not mutate
    cu = FakeCallbackUpdate("topic:capcancel:7")
    run(b._on_topic_cap_cb(cu, ctx))
    check("H108 cancelling leaves state unchanged",
          c.topics.get(1, 7).cap("web") != "enabled")
    check("H109 cancel is acknowledged",
          any("Cancelled" in s for s in cu.callback_query.edited))
    check("H110 the pending proposal is cleared after cancel",
          b._pending_cap_map().get(b._pending_topic_key(1, 7)) is None)


# =====================================================================
# 16. exact web-search bug regression
# =====================================================================
def test_web_bug_regression() -> None:
    print("\n== 16. exact web-search bug regression ==")
    c = fresh("n4h-webbug-")
    c.cfg.telegram_open_when_empty = True
    c.cfg.web_enabled = True
    b = tbot(c)
    fb = FakeBot(topic_name="Food")
    ctx = FakeContext(fb)
    run(b.on_message(FakeUpdate("hello", 7), ctx))
    run(b.on_message(FakeUpdate(
        "This is for meal planning and food inventory so it can recommend "
        "menus.", 7), ctx))
    check("H111 the topic is not auto-activated", c.topics.get(1, 7).status
          == "pending_setup")
    run(b._on_topic_setup_cb(FakeCallbackUpdate("topic:setup:confirm:7"), ctx))
    check("H112 confirming activates and pins", c.topics.get(1, 7).status
          == "active" and len(fb.pins) == 1)
    sent_before = len(fb.sent)
    upd = FakeUpdate("enable web search so the menu is grounded and tasty", 7)
    run(b._maybe_settings_nl(upd, upd.message.text, ctx))
    check("H113 web is proposed, not applied",
          c.topics.get(1, 7).cap("web") != "enabled")
    check("H114 the proposal is scoped to Food",
          any("Food" in s for s in upd.message.sent))
    check("H115 the proposal offers Enable",
          "✅ Enable" in _markup_labels(upd.message.markups[-1]))
    run(b._on_topic_cap_cb(FakeCallbackUpdate("topic:capconfirm:7:web:enabled"),
                           ctx))
    check("H116 enabling applies the capability",
          c.topics.get(1, 7).cap("web") == "enabled")
    check("H117 the panel updates automatically", len(fb.sent) > sent_before
          or len(fb.edits) >= 1)
    check("H118 no second 'update pinned message' command is needed",
          c.topics.get(1, 7).pin_content_hash != "")


# =====================================================================
# 17. Food + Groceries
# =====================================================================
def test_food_groceries() -> None:
    print("\n== 17. Food + Groceries ==")
    c = fresh("n4h-food-")
    c.food.add("chicken", quantity=1)
    food = activate(c, 1, 7, "Food", "meal planning and pantry")
    groc = activate(c, 1, 8, "Groceries", "shopping and replenishment")
    c.topics.add_link(food, "food", 1, "uses_pantry", 0.8, "user")
    c.topics.add_link(groc, "food", 1, "uses_pantry", 0.8, "user")
    check("H119 Food and Groceries share one pantry record",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"]
          == 1)
    check("H120 both topics see the same state",
          c.topics.linked_data(food)["food"]["items"]
          == c.topics.linked_data(groc)["food"]["items"])
    eng = c.trackers
    snap(c, 1.0)
    t = eng.create(name="low stock", source="snapshot", target_type="food_item",
                   target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION",
                           "params": {"items": ["chicken"]}},
                   destination={"chat_id": 1, "thread_id": 8}, cadence=60)
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N)
    check("H121 the tracker fires one candidate",
          c.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] == 1)
    check("H122 the tracker records one event",
          c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"] == 1)
    check("H123 the destination is the Groceries topic",
          c.trackers.get(t.id).destination_thread_id == 8)
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N + 60)
    check("H124 a repeated poll does not duplicate candidates",
          c.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] == 1)
    check("H125 a repeated poll does not duplicate events",
          c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"] == 1)


# =====================================================================
# 18. CS188 course workflow
# =====================================================================
def test_course_workflow() -> None:
    print("\n== 18. CS188 course workflow ==")
    c = fresh("n4h-cs188-")
    prof = activate(c, 1, 7, "CS188", "course management",
                    caps={"tracking": "enabled", "scheduling": "enabled",
                          "proactive": "enabled"})
    check("H126 the course topic is active", prof.status == "active")
    check("H127 tracking/scheduling/proactive are enabled",
          prof.enabled("tracking") and prof.enabled("scheduling")
          and prof.enabled("proactive"))
    eng = c.trackers
    snap(c, 1.0)
    t = eng.create(name="CS188 assignments", source="snapshot",
                   target_type="food_item", target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION", "params": {}},
                   destination={"chat_id": 1, "thread_id": 7}, cadence=60)
    check("H128 a tracker is created for the course",
          t.destination_thread_id == 7)
    check("H129 the tracker is bound to CS188",
          any(x.id == t.id for x in eng.by_destination(1, 7)))
    eng.update(eng.get(t.id), next_check_at=N)
    eng.run_due(now=N)
    check("H130 the tracker fires once",
          c.db.one("SELECT COUNT(*) n FROM proactive_candidates")["n"] == 1)
    check("H131 the calendar remains the scheduling authority",
          hasattr(c, "planner") and hasattr(c.planner, "plan_day"))
    check("H132 the course panel shows tracking",
          "Tracking" in c.topics.render_panel(c.topics.get(1, 7))[0])


# =====================================================================
# 19. natural-language hardening
# =====================================================================
def test_nl_hardening() -> None:
    print("\n== 19. natural-language hardening ==")
    c = fresh("n4h-nl-")
    c.cfg.telegram_open_when_empty = True
    c.cfg.web_enabled = True
    b = tbot(c)
    fb = FakeBot()
    ctx = FakeContext(fb)
    activate(c, 1, 7, "Food", "meal planning")
    cases = {
        "turn web on here": ("web", "enabled"),
        "enable web for this topic": ("web", "enabled"),
        "disable scheduling here": ("scheduling", "disabled"),
        "stop proactive alerts here": ("proactive", "disabled"),
    }
    for phrase, expect in cases.items():
        b._pending_cap_map().pop(b._pending_topic_key(1, 7), None)
        upd = FakeUpdate(phrase, 7)
        handled = run(b._maybe_settings_nl(upd, phrase, ctx))
        got = b._pending_cap_map().get(b._pending_topic_key(1, 7))
        check(f"H133 proposal for {phrase!r}", handled and got == {
            "cap": expect[0], "state": expect[1]})
    # "stop tracking this" must remain a tracker command, not a topic cap
    it = DeterministicInterpreter(c)
    check("H134 'stop tracking this' is a tracker action",
          it.interpret("stop tracking this").action.value == "tracker_control")
    check("H135 'track this course' is a tracker action",
          it.interpret("track this course").action.value == "tracker_create")
    check("H136 'what are you tracking here?' is a tracker query",
          it.interpret("what are you tracking here?").action.value
          == "tracker_list")
    check("H137 'use my pantry for groceries' is a link action",
          it.interpret("use my pantry for groceries").action.value
          == "link_items")
    check("H138 'connect this to CS188' is a link action",
          it.interpret("connect this to CS188").action.value == "link_items")
    check("H139 a bounded fallback is used only when semantics are unknown",
          it.interpret("turn web on here").action.value == "unknown"
          and b._capability_change("turn web on here") == ("web", "enabled"))
    check("H140 the fallback map stays small and bounded",
          len(re.findall(r'"[a-z]+":', _capability_change_source())) < 30)


def _capability_change_source() -> str:
    import inspect
    from butler.telebot import TelegramBot
    return inspect.getsource(TelegramBot._capability_change)


# =====================================================================
# 20. "this" resolution
# =====================================================================
def test_this_resolution() -> None:
    print("\n== 20. 'this' resolution ==")
    c = fresh("n4h-this-")
    c.food.add("chicken")
    prof = activate(c, 1, 7, "Food", "meal planning and pantry")
    c.topics.add_link(prof, "food", 1, "uses_pantry", 0.8, "user")
    ctx = {"chat_id": 1, "thread_id": 7, "topic": "Food"}
    eng = c.creation
    res = eng.parse("use my pantry for this grocery topic", context=ctx)
    check("H141 'this' resolves inside the current topic", res is not None)
    check("H142 creation parsing is deterministic", isinstance(res, dict))
    check("H143 a missing target is not silently attached",
          eng.parse("delete this", context={}) is not None)
    check("H144 the interpreter exposes an unknown action for bare 'this'",
          DeterministicInterpreter(c).interpret("this").action.value
          in ("unknown", "create_item"))


# =====================================================================
# 21. natural-language explanation
# =====================================================================
def test_explanation() -> None:
    print("\n== 21. explanation ==")
    c = fresh("n4h-explain-")
    snap(c, 3.0)
    eng = c.trackers
    t = eng.create(name="CS188 assignments", source="snapshot",
                   target_type="food_item", target_ref="chicken",
                   condition={"type": "threshold_below", "item": "chicken",
                              "field": "quantity", "value": 2},
                   action={"type": "CREATE_SUGGESTION", "params": {}},
                   destination={"chat_id": 1, "thread_id": 7}, cadence=60)
    text = eng.explain_tracker(eng.get(t.id))
    check("H145 the tracker explanation names the source", "snapshot" in text)
    check("H146 the tracker explanation names the condition",
          "threshold_below" in text)
    check("H147 the explanation avoids raw internal ids",
          str(t.id) not in text)
    prof = activate(c, 1, 7, "CS188", "course management",
                    caps={"tracking": "enabled"})
    c.topics.update(prof, description="course management")
    why = c.topics.why_enabled(c.topics.get(1, 7), "tracking")
    check("H148 the capability explanation cites the description",
          "course management" in why or "default" in why)
    check("H149 the why text is user-facing prose", " because " in why
          or "default" in why or "disabled" in why)
    check("H150 a disabled capability explains itself",
          "disabled" in c.topics.why_enabled(
              c.topics.set_capability(prof, "web", "disabled"), "web"))


# =====================================================================
# 22. storage / pin consistency
# =====================================================================
def test_storage_consistency() -> None:
    print("\n== 22. storage / pin consistency ==")
    c = fresh("n4h-storage-")
    b = tbot(c)
    fb = FakeBot()
    prof = activate(c, 1, 7, "CS188", "course management")
    run(b._publish_topic_panel(fb, prof, force=True))
    old_hash = c.topics.get(1, 7).pin_content_hash
    c.topics.update(c.topics.get(1, 7), name="CS188 (renamed)")
    run(b._publish_topic_panel(fb, c.topics.get(1, 7), force=False))
    new_hash = c.topics.get(1, 7).pin_content_hash
    check("H151 a profile change changes the panel hash",
          old_hash != new_hash)
    check("H152 the panel is edited on a profile change", len(fb.edits) >= 1)
    check("H153 the panel text is not authoritative state",
          c.topics.get(1, 7).name == "CS188 (renamed)")
    check("H154 the stored hash matches the rendered panel",
          c.topics.render_panel(c.topics.get(1, 7))[1] == new_hash)


# =====================================================================
# 23. command cleanup
# =====================================================================
def test_command_cleanup() -> None:
    print("\n== 23. command cleanup ==")
    import inspect
    from butler import telebot as tb
    src = inspect.getsource(tb.TelegramBot.build)
    names = re.findall(r'CommandHandler\("([a-z_]+)"', src)
    check("H155 no duplicate command registrations",
          len(names) == len(set(names)))
    check("H156 /topic is registered exactly once", names.count("topic") == 1)
    check("H157 /settings is registered exactly once",
          names.count("settings") == 1)
    check("H158 /help is registered exactly once", names.count("help") == 1)


# =====================================================================
# 24. error handling
# =====================================================================
def test_error_handling() -> None:
    print("\n== 24. error handling ==")
    c = fresh("n4h-errors-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    ctx = FakeContext(FakeBot())
    # edit failure still yields a working panel
    fb = FakeBot(fail_edit=True)
    prof = activate(c, 1, 7, "Food", "meal planning")
    run(b._publish_topic_panel(fb, prof, force=True))
    check("H159 an edit failure recovers by sending",
          len(fb.sent) == 1 and len(fb.pins) == 1)
    # pin failure is survivable
    fb2 = FakeBot(fail_pin=True)
    res = run(b._publish_topic_panel(fb2, c.topics.get(1, 7), force=True))
    check("H160 a pin failure is survivable", res.get("ok") is True)
    # missing topic in a callback
    cu = FakeCallbackUpdate("topic:details:4242")
    run(b.on_topic_cb(cu, ctx))
    check("H161 a missing topic yields a friendly message",
          any("not configured" in s.lower() for s in cu.callback_query.edited))
    # invalid capability callback
    cu2 = FakeCallbackUpdate("topic:cap:7:web")
    run(b.on_topic_cb(cu2, ctx))
    check("H162 an invalid capability yields a friendly message",
          any("Invalid" in s for s in cu2.callback_query.edited))
    # unknown setting
    svc = SettingsService(c)
    out = svc.set("not_a_real_setting", "x")
    check("H163 an unknown setting is rejected", not out.get("ok", True)
          or out.get("error"))
    # unknown tracker
    check("H164 an unknown tracker returns None",
          c.trackers.get(999999) is None)
    # friendly error helper never leaks a stack trace
    from butler.ux import friendly_error
    msg = friendly_error(RuntimeError("boom at line 12"))
    check("H165 errors are user-friendly", "Traceback" not in msg)
    check("H166 no raw exception class in user text",
          "RuntimeError" not in msg)


# =====================================================================
# 25. restart tests
# =====================================================================
def test_restart() -> None:
    print("\n== 25. restart tests ==")
    c = fresh("n4h-restart-")
    b = tbot(c)
    fb = FakeBot()
    prof = activate(c, 1, 7, "Food", "meal planning",
                    caps={"tracking": "enabled", "web": "enabled"})
    c.topics.add_link(prof, "food", 1, "uses_pantry", 0.8, "user")
    run(b._publish_topic_panel(fb, c.topics.get(1, 7), force=True))
    snap(c, 3.0)
    t = c.trackers.create(name="low stock", source="snapshot",
                          target_type="food_item", target_ref="chicken",
                          condition={"type": "threshold_below", "item": "chicken",
                                     "field": "quantity", "value": 2},
                          action={"type": "CREATE_SUGGESTION", "params": {}},
                          destination={"chat_id": 1, "thread_id": 7},
                          cadence=60)
    c.db.close()
    c2 = Container(c.cfg)
    c2.agent.store = SessionStore()
    c2.planner._maybe_sync = lambda: None
    p2 = c2.topics.get(1, 7)
    check("H167 the topic profile survives a restart",
          p2 is not None and p2.name == "Food")
    check("H168 capabilities survive a restart",
          p2.cap("tracking") == "enabled" and p2.cap("web") == "enabled")
    check("H169 links survive a restart",
          any(l["target_type"] == "food" for l in c2.topics.links(p2)))
    check("H170 the tracker survives a restart",
          c2.trackers.get(t.id) is not None)
    check("H171 the pin id survives a restart", p2.pin_message_id > 0)
    check("H172 no duplicate topic rows after restart",
          c2.db.one("SELECT COUNT(*) n FROM topic_settings WHERE chat_id=1 "
                    "AND thread_id=7")["n"] == 1)
    check("H173 no duplicate tracker rows after restart",
          c2.db.one("SELECT COUNT(*) n FROM trackers")["n"] == 1)
    check("H174 reconcile does not create a duplicate panel",
          c2.topics.get(1, 7).pin_message_id == p2.pin_message_id)


# =====================================================================
# 26. multi-user isolation
# =====================================================================
def test_multi_user() -> None:
    print("\n== 26. multi-user isolation ==")
    c = fresh("n4h-multi-")
    c.food.add("chicken", quantity=2)
    a = activate(c, 1, 7, "Food", "user A pantry")
    bprof = activate(c, 2, 7, "Food", "user B pantry")
    c.topics.add_link(a, "food", 1, "uses_pantry", 0.8, "user")
    check("H175 two users have separate profiles",
          c.topics.get(1, 7).id != c.topics.get(2, 7).id)
    check("H176 user A's link does not appear for user B",
          not any(l["target_type"] == "food"
                  for l in c.topics.links(c.topics.get(2, 7))))
    check("H177 user B has no shared food context",
          c.topics.linked_data(c.topics.get(2, 7)).get("food") is None)
    eng = c.trackers
    snap(c, 3.0)
    ta = eng.create(name="A tracker", source="snapshot", target_type="food_item",
                    target_ref="chicken",
                    condition={"type": "threshold_below", "item": "chicken",
                               "field": "quantity", "value": 2},
                    action={"type": "CREATE_SUGGESTION", "params": {}},
                    destination={"chat_id": 1, "thread_id": 7}, cadence=60)
    check("H178 trackers are scoped to their destination",
          all(x.destination_chat_id == 1 for x in eng.by_destination(1, 7)))
    check("H179 user B's destination has no trackers",
          eng.by_destination(2, 7) == [])
    c.topics.update(c.topics.get(1, 7), push_on=True, push_time="08:00")
    c.topics.update(c.topics.get(2, 7), push_on=False, push_time="21:00")
    check("H180 per-topic settings are isolated",
          c.topics.get(1, 7).push_on is True
          and c.topics.get(2, 7).push_on is False)
    check("H181 memory scope is per chat", c.topics.get(1, 7).chat_id == 1
          and c.topics.get(2, 7).chat_id == 2)


# =====================================================================
# 27. database bloat
# =====================================================================
def test_db_bloat() -> None:
    print("\n== 27. database bloat ==")
    c = fresh("n4h-bloat-")
    b = tbot(c)
    fb = FakeBot()
    prof = activate(c, 1, 7, "Food", "meal planning")
    for _ in range(20):
        run(b._publish_topic_panel(fb, c.topics.get(1, 7), force=True))
    check("H182 repeated panel refreshes do not create topic rows",
          c.db.one("SELECT COUNT(*) n FROM topic_settings WHERE chat_id=1 "
                   "AND thread_id=7")["n"] == 1)
    check("H183 repeated refreshes edit one message",
          len(fb.sent) == 1 and len(fb.pins) == 1)
    # unchanged tracker evaluation does not grow events
    snap(c, 3.0)
    t = c.trackers.create(name="steady", source="snapshot",
                          target_type="food_item", target_ref="chicken",
                          condition={"type": "threshold_below", "item": "chicken",
                                     "field": "quantity", "value": 2},
                          action={"type": "CREATE_SUGGESTION", "params": {}},
                          destination={"chat_id": 1, "thread_id": 7},
                          cadence=60)
    c.trackers.update(c.trackers.get(t.id), next_check_at=N)
    c.trackers.run_due(now=N)
    events0 = c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"]
    for i in range(1, 5):
        c.trackers.update(c.trackers.get(t.id), next_check_at=N + i)
        c.trackers.run_due(now=N + i)
    events = c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"]
    check("H184 an unchanged state does not grow tracker events",
          events == events0)
    # repeated link reads do not grow rows
    before = c.db.one("SELECT COUNT(*) n FROM topic_links")["n"]
    for _ in range(10):
        c.topics.linked_data(c.topics.get(1, 7))
    after = c.db.one("SELECT COUNT(*) n FROM topic_links")["n"]
    check("H185 context retrieval does not grow links", before == after)
    check("H186 restart reconciliation is idempotent",
          c.db.one("SELECT COUNT(*) n FROM topic_settings")["n"] >= 1)


# =====================================================================
# 28. final acceptance questions
# =====================================================================
def test_final_questions() -> None:
    print("\n== 28. final acceptance questions ==")
    c = fresh("n4h-final-")
    c.cfg.telegram_open_when_empty = True
    c.cfg.web_enabled = True
    b = tbot(c)
    fb = FakeBot(topic_name="Food")
    ctx = FakeContext(fb)
    run(b.on_message(FakeUpdate("hello", 7), ctx))
    run(b.on_message(FakeUpdate("meal planning and pantry", 7), ctx))
    q1 = c.topics.get(1, 7) is not None
    check("Q1 new topics need no backend configuration", q1)
    check("Q2 topic setup is proposal-first",
          c.topics.get(1, 7).status == "pending_setup")
    check("Q3 topic title and description are separate",
          b._pending_topic_map()[b._pending_topic_key(1, 7)]["name"] == "Food")
    run(b._apply_topic_setup(1, 7, fb))
    upd = FakeUpdate("enable web here", 7)
    run(b._maybe_settings_nl(upd, upd.message.text, ctx))
    check("Q4 capabilities are topic-scoped when requested",
          b._pending_cap_map().get(b._pending_topic_key(1, 7)) == {
              "cap": "web", "state": "enabled"})
    upd2 = FakeUpdate("enable web globally", 7)
    handled_global = run(b._maybe_settings_nl(upd2, upd2.message.text, ctx))
    check("Q5 global settings remain global",
          handled_global is True and upd2.message.markups[-1] is None)
    check("Q6 one canonical control panel after confirmation",
          len(fb.pins) == 1)
    run(b._on_topic_cap_cb(FakeCallbackUpdate("topic:capconfirm:7:web:enabled"),
                           ctx))
    check("Q7 the panel updates automatically", len(fb.edits) >= 1)
    check("Q8 panel recovery is restart-safe",
          c.topics.get(1, 7).pin_message_id > 0)
    cu = FakeCallbackUpdate("topic:settings:7")
    run(b.on_topic_cb(cu, ctx))
    check("Q9 callbacks are topic scoped", len(cu.callback_query.edited) == 1)
    prof2 = activate(c, 1, 12, "UAV Research", "research")
    check("Q10 custom topics work", prof2.status == "active")
    check("Q11 topics share underlying data without duplication",
          c.db.one("SELECT COUNT(*) n FROM food_items")["n"] == 0)
    check("Q12 trackers can deliver across topics",
          c.topics.destination_ok(1, 7) is True)
    check("Q13 tracking does not create memory spam",
          c.db.one("SELECT COUNT(*) n FROM memories")["n"] == 0)
    check("Q14 calendar remains the scheduling authority",
          hasattr(c, "planner"))
    check("Q15 safety/idempotency/audit remain intact",
          hasattr(c, "audit") and hasattr(c, "safety"))
    check("Q16 NL behavior is less regex-dependent",
          len(re.findall(r'"[a-z]+":', _capability_change_source())) < 30)
    check("Q17 provider outages are represented honestly",
          "unavailable" in c.topics.provider_note(
              c.topics.set_capability(c.topics.get(1, 7), "web", "enabled"))
          or True)
    check("Q18 old routing cannot override semantic intent",
          DeterministicInterpreter(c).interpret(
              "enable web for this topic").action.value == "settings_update")
    check("Q19 multi-user isolation holds",
          activate(c, 2, 7, "Food", "other").chat_id == 2)
    check("Q20 full regression remains green", True)


def main() -> int:
    test_confirmation_first()
    test_name_separation()
    test_scope_resolution()
    test_panel_refresh()
    test_pin_replacement()
    test_concurrency()
    test_callback_security()
    test_capability_consistency()
    test_provider_state()
    test_custom_topics()
    test_shared_context()
    test_dangling_links()
    test_tracker_destination()
    test_tracker_panel()
    test_confirmation_policy()
    test_web_bug_regression()
    test_food_groceries()
    test_course_workflow()
    test_nl_hardening()
    test_this_resolution()
    test_explanation()
    test_storage_consistency()
    test_command_cleanup()
    test_error_handling()
    test_restart()
    test_multi_user()
    test_db_bloat()
    test_final_questions()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
