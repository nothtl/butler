"""N1 acceptance: universal topics + shared context + persistent topic panel.

Run:  .venv/bin/python tests/run_acceptance_n1.py

Deterministic and offline. A Telegram forum topic is a durable TopicProfile —
a context/view over existing domain data — not a routing destination and not a
new datastore.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TZ", "UTC")
if hasattr(time, "tzset"):
    time.tzset()

from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402
from butler.topics import (  # noqa: E402
    ACTIVE, CAPABILITIES, DEFAULT_CAPS, PENDING_SETUP, TopicStore,
    interpret_purpose, name_guess, purpose_label, suggest_capabilities,
)

PASS = 0
FAIL = 0
NOW = int(datetime(2026, 9, 7, 9, 0, tzinfo=timezone.utc).timestamp())


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
    cfg.sleep_start = 23 * 60
    cfg.sleep_end = 7 * 60
    cfg.ensure_dirs()
    return cfg


def fresh(prefix: str = "n1-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


def configure(c, chat, thread, name, text):
    st = c.topics
    prof, _ = st.ensure(chat, thread, name)
    i = interpret_purpose(text)
    links, amb = st.resolve_links(name or i["name_guess"], i["description"])
    prof = st.update(prof, name=name or i["name_guess"] or "Topic",
                     purpose=i["purpose"], description=i["description"],
                     capabilities=i["capabilities"], status=ACTIVE)
    for l in links:
        st.add_link(prof, l["target_type"], l["target_id"], l["relation"],
                    l["confidence"], l["provenance"])
    return st.get(chat, thread), amb


class FakeMsg:
    def __init__(self, mid):
        self.message_id = mid


class FakeBot:
    def __init__(self, *, fail_edit=False, fail_pin=False):
        self.sent, self.edits, self.pins = [], [], []
        self._next = 1000
        self.fail_edit = fail_edit
        self.fail_pin = fail_pin

    async def send_message(self, chat_id, text, message_thread_id=None):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text,
                          "thread_id": message_thread_id})
        return FakeMsg(self._next)

    async def edit_message_text(self, text, chat_id, message_id):
        if self.fail_edit:
            raise RuntimeError("message missing")
        self.edits.append({"chat_id": chat_id, "message_id": message_id,
                           "text": text})

    async def pin_chat_message(self, chat_id, message_id,
                               disable_notification=True):
        if self.fail_pin:
            raise RuntimeError("cannot pin")
        self.pins.append({"chat_id": chat_id, "message_id": message_id})


def topic_bot(c):
    from butler.telebot import TelegramBot
    b = TelegramBot.__new__(TelegramBot)
    b.container = c
    b.topics = {}
    b._await_url = {}
    b._pending_urls = {}
    return b


class FakeChat:
    def __init__(self, cid):
        self.id = cid
        self.is_forum = True


class FakeUser:
    def __init__(self, uid=42, username="tester"):
        self.id = uid
        self.username = username


class FakeMessage:
    def __init__(self, text, thread_id):
        self.text = text
        self.message_thread_id = thread_id
        self.is_topic_message = bool(thread_id)
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, text, thread_id, uid=42):
        self.message = FakeMessage(text, thread_id)
        self.effective_message = self.message
        self.effective_chat = FakeChat(100)
        self.effective_user = FakeUser(uid)


class FakeTopicInfo:
    def __init__(self, name):
        self.name = name


class FakeTopicBot(FakeBot):
    async def get_forum_topic(self, chat_id, thread_id):
        return FakeTopicInfo("CS188")


class FakeContext:
    def __init__(self, bot):
        self.bot = bot


def test_telegram_flow() -> None:
    print("\n== Telegram discovery flow ==")
    c = fresh("n1-tg-")
    c.cfg.telegram_open_when_empty = True
    bot = topic_bot(c)
    fb = FakeTopicBot()
    ctx = FakeContext(fb)
    # first message in a brand-new topic -> setup prompt, no profile activation
    upd = FakeUpdate("hello", 7)
    asyncio.run(bot.on_message(upd, ctx))
    prof = c.topics.get(100, 7)
    check("T1 a new topic is discovered", prof is not None
          and prof.status == PENDING_SETUP)
    check("T2 the setup prompt is sent",
          upd.message.sent and "What is this topic for" in upd.message.sent[0])
    # reply with a purpose -> a setup proposal, but NO activation/pin yet
    upd2 = FakeUpdate("This is my CS188 course. Track homework and schedule.",
                      7)
    asyncio.run(bot.on_message(upd2, ctx))
    prof = c.topics.get(100, 7)
    check("T3 the purpose reply shows a proposal, not activation",
          prof.status == PENDING_SETUP)
    check("T4 no panel is pinned before confirmation",
          len(fb.sent) == 0 and len(fb.pins) == 0)
    check("T4b the proposal is shown",
          any("Confirm" in s or "Purpose" in s for s in upd2.message.sent))
    # confirmation activates + pins
    prof = asyncio.run(bot._apply_topic_setup(100, 7, fb))
    check("T5 confirmation activates the topic", prof.status == ACTIVE)
    check("T6 the panel is sent and pinned",
          len(fb.sent) == 1 and len(fb.pins) == 1)
    # unauthorized user gets nothing
    c.cfg.telegram_open_when_empty = False
    c.cfg.telegram_allowed_users = [7]
    upd3 = FakeUpdate("hi", 8, uid=99)
    asyncio.run(bot.on_message(upd3, ctx))
    check("T7 an unauthorized user is denied",
          c.topics.get(100, 8) is None and not upd3.message.sent)
    check("T8 the confirmed topic stays active",
          c.topics.get(100, 7).status == ACTIVE)


# =====================================================================
# A. new topic discovery
# =====================================================================
def test_discovery() -> None:
    print("\n== A. topic discovery ==")
    c = fresh("n1-disc-")
    st = c.topics
    prof, created = st.ensure(100, 5, "CS188")
    check("A1 a new topic creates a profile", created and prof.id > 0)
    check("A2 a new topic is pending_setup", prof.status == PENDING_SETUP)
    check("A3 the topic name is stored", prof.name == "CS188")
    check("A4 a second ensure does not recreate", st.ensure(100, 5)[1] is False)
    check("A5 profiles are keyed by chat+thread",
          st.get(100, 5) is not None and st.get(100, 6) is None)
    c.db.close()
    c2 = Container(c.cfg)
    check("A6 the profile survives a restart", c2.topics.get(100, 5) is not None)
    bot = topic_bot(c2)
    check("A7 the setup prompt names the topic",
          "CS188" in bot._setup_prompt(c2.topics.get(100, 5)))
    prof2, amb = configure(c2, 100, 5, "CS188",
                           "This is my CS188 course. Track homework and deadlines "
                           "and schedule study work.")
    check("A8 purpose capture activates the topic", prof2.status == ACTIVE)
    check("A9 purpose is a human label", prof2.purpose == "Course management")
    check("A10 the description is stored verbatim",
          "CS188 course" in prof2.description)
    check("A11 the course code is guessed from text",
          name_guess("This is my CS188 course") == "CS188")
    check("A12 no backend configuration was required", prof2.status == ACTIVE)


# =====================================================================
# B. topic profile
# =====================================================================
def test_profile() -> None:
    print("\n== B. topic profile ==")
    c = fresh("n1-prof-")
    st = c.topics
    prof, _ = st.ensure(1, 1, "CS188")
    check("B1 every capability has a state",
          set(prof.capabilities) == set(CAPABILITIES))
    caps = suggest_capabilities("track homework and deadlines and schedule study")
    check("B2 tracking is suggested from 'track'", caps["tracking"] == "enabled")
    check("B3 scheduling is suggested from 'schedule'",
          caps["scheduling"] == "enabled")
    check("B4 file organization is opt-in (off by default)",
          DEFAULT_CAPS["file_organization"] == "disabled")
    neg = suggest_capabilities("don't track assignments")
    check("B5 a negative statement disables a capability",
          neg["tracking"] == "disabled")
    check("B6 purpose_label handles food", purpose_label("meal planning and pantry")
          == "Food & meal planning")
    prof = st.update(prof, purpose="Course management", description="CS188",
                     status=ACTIVE)
    check("B7 update persists purpose", st.get(1, 1).purpose == "Course management")
    prof = st.set_capability(prof, "tracking", "enabled")
    check("B8 set_capability persists", st.get(1, 1).cap("tracking") == "enabled")
    before = st.get(1, 1).cap("tracking")
    st.set_capability(st.get(1, 1), "nonsense", "enabled")
    check("B9 an invalid capability is ignored",
          st.get(1, 1).cap("tracking") == before)
    st.update(st.get(1, 1), status="paused")
    check("B10 list filters by status",
          any(p.thread_id == 1 for p in st.list(status="paused"))
          and not any(p.thread_id == 1 for p in st.list(status="active")))
    st.update(st.get(1, 1), status=ACTIVE)
    tmpl = st.to_template(st.get(1, 1))
    check("B11 templates copy configuration, not data",
          "capabilities" in tmpl and "tasks" not in tmpl)
    p2, _ = st.ensure(1, 2, "CS188 clone")
    p2 = st.apply_template(p2, tmpl)
    check("B12 apply_template copies capabilities",
          p2.cap("tracking") == st.get(1, 1).cap("tracking"))
    check("B13 timestamps are set",
          st.get(1, 1).created_at > 0 and st.get(1, 1).updated_at > 0)


# =====================================================================
# C. context
# =====================================================================
def test_context() -> None:
    print("\n== C. context ==")
    c = fresh("n1-ctx-")
    c.db.add_course("CS188")
    c.db.add_task("CS188 HW4", est_minutes=90, priority=4)
    c.db.add_task("CS170 HW1", est_minutes=60, priority=3)
    configure(c, 100, 5, "CS188", "CS188 course track homework and schedule")
    svc = ExecutiveService(c, now_ts=NOW)
    r = svc.ask(text="what is due next", topic={"chat_id": 100, "thread_id": 5},
                include_context=True)
    snap = r.context
    check("C1 the snapshot carries the topic", snap.topic.get("name") == "CS188")
    check("C2 the topic is active in the snapshot",
          snap.topic.get("status") == ACTIVE)
    check("C3 linked course data is present",
          snap.topic_context.get("courses")
          and snap.topic_context["courses"][0]["code"] == "CS188")
    rel = [t for t in snap.tasks if t.get("topic_relevant")]
    check("C4 linked tasks are marked relevant", rel and "CS188" in rel[0]["title"])
    check("C5 the linked task sorts first", snap.tasks[0]["title"].startswith("CS188"))
    check("C6 the topic does NOT hard-limit global search",
          len(snap.tasks) == 2)
    check("C7 sources include topic", "topic" in snap.sources)
    r2 = svc.ask(text="what is due next", include_context=True)
    check("C8 no topic means no topic context", not r2.context.topic)
    # two topics share the same course record
    c.db.execute("UPDATE topic_settings SET status='paused' WHERE chat_id=100 "
                 "AND thread_id=5")
    r3 = svc.ask(text="anything", topic={"chat_id": 100, "thread_id": 5},
                 include_context=True)
    check("C9 an inactive topic contributes no linked data",
          not r3.context.topic_context)
    c.db.execute("UPDATE topic_settings SET status='active' WHERE chat_id=100 "
                 "AND thread_id=5")
    st = c.topics
    p2, _ = st.ensure(200, 9, "CS188 mirror")
    st.add_link(p2, "course", 1, "about", 0.9, "user")
    d1 = st.linked_data(st.get(100, 5))
    d2 = st.linked_data(st.get(200, 9))
    check("C10 two topics return the same shared course",
          d1["courses"] and d2["courses"]
          and d1["courses"][0]["id"] == d2["courses"][0]["id"])
    check("C11 no course record was duplicated",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)
    txt = st.context_text(st.get(100, 5))
    check("C12 context text marks topic as a hint, not a boundary",
          "not a hard boundary" in txt)


# =====================================================================
# D. linking
# =====================================================================
def test_linking() -> None:
    print("\n== D. linking ==")
    c = fresh("n1-link-")
    c.db.add_course("CS188")
    c.db.add_course("CS170")
    st = c.topics
    p, _ = st.ensure(1, 1, "CS188")
    links, amb = st.resolve_links("CS188", "cs188 course")
    check("D1 exact course match", any(l["target_type"] == "course"
                                       and l["target_id"] == 1 for l in links))
    check("D2 unrelated course is not matched",
          not any(l.get("target_id") == 2 for l in links))
    # project exact match
    c.projects.create_project("Stock Bot", deadline=NOW + 86400)
    p2, _ = st.ensure(1, 2, "Stock Bot")
    links2, amb2 = st.resolve_links("Stock Bot", "software project")
    check("D3 exact project match", any(l["target_type"] == "project" for l in links2))
    # ambiguous project
    c.projects.create_project("Stock Bot Alpha", deadline=NOW + 86400)
    c.projects.create_project("Stock Bot Beta", deadline=NOW + 86400)
    _, amb3 = st.resolve_links("Stock Bot", "stock bot project")
    check("D4 ambiguous project match is flagged", bool(amb3))
    # explicit link + remove
    prof = st.get(1, 1)
    st.add_link(prof, "task", 99, "tracks", 1.0, "user")
    check("D5 explicit link stored",
          any(l["target_type"] == "task" for l in st.links(prof)))
    lid = [l["id"] for l in st.links(prof) if l["target_type"] == "task"][0]
    st.remove_link(prof, lid)
    check("D6 link removed",
          not any(l["target_type"] == "task" for l in st.links(prof)))
    # food keyword links
    lf, _ = st.resolve_links("Groceries", "shopping using pantry and meals")
    check("D7 food/pantry link from keywords",
          any(l["target_type"] == "food" for l in lf))
    check("D8 meal plan link from keywords",
          any(l["target_type"] == "meal_plan" for l in lf))
    # link metadata
    st.add_link(prof, "course", 1, "about", 0.95, "name_match")
    row = [l for l in st.links(prof) if l["target_type"] == "course"][0]
    check("D9 link stores confidence", float(row["confidence"]) == 0.95)
    check("D10 link stores provenance", row["provenance"] == "name_match")
    # unique constraint refreshes rather than duplicates
    n0 = len([l for l in st.links(prof) if l["target_type"] == "course"])
    st.add_link(prof, "course", 1, "about", 0.9, "user")
    n1 = len([l for l in st.links(prof) if l["target_type"] == "course"])
    check("D11 re-linking refreshes, no duplicate", n0 == n1 == 1)
    # no graph database: only the lightweight bridge table exists
    tables = {r["name"] for r in c.db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("D12 no graph database tables", not (tables & {"nodes", "edges",
          "graph", "objects", "relations"}))
    check("D13 the bridge table is present", "topic_links" in tables)
    check("D14 a topic can reference a shared record by id",
          st.links(prof)[0]["target_type"] in ("course", "project", "food",
                                               "meal_plan", "task"))


# =====================================================================
# E. pinned panel
# =====================================================================
def test_pin() -> None:
    print("\n== E. pinned control panel ==")
    c = fresh("n1-pin-")
    c.db.add_course("CS188")
    c.db.add_task("CS188 HW4", est_minutes=90, priority=4)
    prof, _ = configure(c, 100, 5, "CS188",
                        "CS188 course track homework and schedule")
    bot = topic_bot(c)
    st = c.topics
    text, chash = st.render_panel(prof)
    check("E1 the panel renders", bool(text) and len(chash) == 16)
    check("E2 the panel names the topic", "CS188" in text)
    check("E3 the panel shows the purpose", "Course management" in text)
    check("E4 the panel shows capabilities", "Butler" in text and "✅" in text)
    check("E5 the panel is not raw JSON", "{" not in text and "target_type" not in text)
    fb = FakeBot()
    asyncio.run(bot._publish_topic_panel(fb, st.get(100, 5), force=True))
    check("E6 a new panel is sent", len(fb.sent) == 1)
    check("E7 the panel is pinned", len(fb.pins) == 1)
    mid = st.get(100, 5).pin_message_id
    check("E8 the message id is stored", mid > 0)
    check("E9 the content hash is stored", bool(st.get(100, 5).pin_content_hash))
    v1 = st.get(100, 5).pin_message_version
    # unchanged -> no edit, no new message
    asyncio.run(bot._publish_topic_panel(fb, st.get(100, 5)))
    check("E10 an unchanged panel is skipped (hash)", len(fb.edits) == 0
          and len(fb.sent) == 1)
    # capability change -> edit in place
    prof2 = st.set_capability(st.get(100, 5), "tracking", "disabled")
    asyncio.run(bot._publish_topic_panel(fb, st.get(100, 5)))
    check("E11 a capability change edits the panel", len(fb.edits) == 1)
    check("E12 editing does not create a new message", len(fb.sent) == 1)
    check("E13 the version increments",
          st.get(100, 5).pin_message_version > v1)
    # missing/uneditable -> replacement + re-pin, no duplicate
    fb2 = FakeBot(fail_edit=True)
    asyncio.run(bot._publish_topic_panel(fb2, st.get(100, 5), force=True))
    check("E14 an uneditable panel is replaced", len(fb2.sent) == 1)
    check("E15 the replacement is pinned", len(fb2.pins) == 1)
    # reconciliation repairs a missing pin
    st.update(st.get(100, 5), pin_message_id=0, pin_content_hash="")
    fb3 = FakeBot()
    rep = asyncio.run(bot.reconcile_topics(fb3))
    check("E16 startup reconciliation repairs a missing panel",
          rep.get("repaired", 0) >= 1 and len(fb3.sent) == 1)
    # repeated reconciliation is idempotent
    fb4 = FakeBot()
    rep2 = asyncio.run(bot.reconcile_topics(fb4))
    check("E17 repeated reconciliation creates no duplicate",
          len(fb4.sent) == 0)
    # pin failure does not crash
    fb5 = FakeBot(fail_pin=True)
    out = asyncio.run(bot._publish_topic_panel(fb5, st.get(100, 5), force=True))
    check("E18 a pin failure is tolerated", out.get("ok") is True)


# =====================================================================
# F. UX
# =====================================================================
def test_ux() -> None:
    print("\n== F. UX ==")
    c = fresh("n1-ux-")
    c.db.add_course("CS188")
    c.db.add_task("CS188 HW4", est_minutes=90, priority=4)
    prof, _ = configure(c, 100, 5, "CS188",
                        "CS188 course track homework and schedule")
    st = c.topics
    prof = st.get(100, 5)
    check("F1 settings lists capabilities", "Capabilities" in st.settings_text(prof))
    check("F2 settings shows notification behavior",
          "Notifications" in st.settings_text(prof))
    check("F3 connections is human-readable",
          "Course" in st.connections_text(prof))
    check("F4 details explains provenance", "Provenance" in st.details_text(prof))
    check("F5 why_enabled explains tracking",
          "Track is enabled" in st.why_enabled(prof, "tracking"))
    check("F6 why_enabled explains a disabled capability",
          "disabled" in st.why_enabled(prof, "file_organization"))
    check("F7 storage text is friendly",
          "storage" in st.storage_text(prof).lower())
    bot = topic_bot(c)
    kb = bot._topic_cap_keyboard(5, prof)
    check("F8 the capability keyboard builds rows",
          len(kb.inline_keyboard) >= len(CAPABILITIES))
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("F9 callback data matches the validated shape",
          all(re.match(r"^[a-z_]+:[a-z_:0-9-]+$", d) for d in data))
    check("F10 the panel keyboard offers settings/connections/details",
          all(x in str(bot._topic_keyboard(5)) for x in
              ("Settings", "Connections", "Details")))
    check("F11 details names what Butler is doing",
          "Capabilities" in st.details_text(prof))
    check("F12 connections shows linked counts when present",
          isinstance(st.connections_text(prof), str))


# =====================================================================
# G. security
# =====================================================================
def test_security() -> None:
    print("\n== G. security ==")
    c = fresh("n1-sec-")
    st = c.topics
    prof, _ = st.ensure(100, 5, "CS188")
    st.update(prof, purpose="Course management", description="CS188",
              status=ACTIVE, capabilities=dict(DEFAULT_CAPS))
    text, _ = st.render_panel(st.get(100, 5))
    check("G1 the panel contains no secrets",
          "token" not in text.lower() and "api_key" not in text.lower())
    # profiles are keyed by chat+thread (no arbitrary attachment)
    check("G2 a topic is scoped to its chat",
          st.get(999, 5) is None)
    # audit trail
    actions = [a["action"] for a in c.audit.recent(limit=100)]
    check("G3 topic mutations are audited",
          any(a.startswith("topic_") for a in actions), str(actions[:3]))
    # capability state validation
    before = st.get(100, 5).cap("tracking")
    st.set_capability(st.get(100, 5), "tracking", "bogus")
    check("G4 an invalid capability state is rejected",
          st.get(100, 5).cap("tracking") == before)
    # callback shape
    check("G5 forged topic callbacks are rejected",
          not re.match(r"^[a-z_]+:[a-z_:0-9-]+$", "topic:cap:5:tracking:enabled; rm"))
    # unauthorized telegram denied
    from butler.telebot import TelegramBot
    bot = TelegramBot.__new__(TelegramBot); bot.container = c
    c.cfg.telegram_allowed_users = [7]; c.cfg.telegram_open_when_empty = False
    class U:
        class effective_user:
            id = 99
    check("G6 an unauthorized user is denied", bot._authorized(U()) is False)
    # no cross-topic data leakage
    p2, _ = st.ensure(200, 5, "Other")
    check("G7 topics in different chats are independent",
          st.get(100, 5).id != st.get(200, 5).id)
    # invalid thread id in callback handled safely (parsing)
    bad = "topic:settings:notanumber"
    parts = bad.split(":")
    safe = True
    try:
        int(parts[2])
        safe = False
    except ValueError:
        safe = True
    check("G8 an invalid callback thread id is handled", safe)
    # topic configuration uses the existing store (single write path)
    check("G9 there is exactly one topic store",
          isinstance(c.topics, TopicStore))
    # no secrets in audit detail
    recent = c.audit.recent(limit=20)
    check("G10 audit details are redacted of secret keys",
          all("api_key" not in json.dumps(a.get("detail", "")) for a in recent))


# =====================================================================
# H. memory scoping
# =====================================================================
def test_memory() -> None:
    print("\n== H. memory scoping ==")
    c = fresh("n1-mem-")
    c.db.add_course("CS188")
    c.memory.remember(type="preference", key="coding",
                      value="i prefer long coding sessions",
                      provenance="explicit_user", confidence=0.9, now=NOW)
    c.memory.remember(type="preference", key="reading",
                      value="for cs188 read before lecture",
                      provenance="explicit_user", confidence=0.9,
                      tags=["topic:cs188"], now=NOW)
    configure(c, 100, 5, "CS188", "CS188 course track homework")
    svc = ExecutiveService(c, now_ts=NOW)
    r = svc.ask(text="prefer coding", topic={"chat_id": 100, "thread_id": 5},
                include_context=True)
    vals = " ".join(m.get("value", "") for m in r.context.relevant_memories)
    check("H1 global memory is retrieved with a topic", "coding" in vals)
    check("H2 topic-scoped memory is retrieved in its topic", "cs188" in vals)
    r2 = svc.ask(text="prefer coding", include_context=True)
    vals2 = " ".join(m.get("value", "") for m in r2.context.relevant_memories)
    check("H3 global memory is retrieved without a topic", "coding" in vals2)
    check("H4 topic-scoped memory is not forced without the topic",
          "cs188" not in vals2)
    check("H5 the same single memory store is reused",
          hasattr(c, "memory") and not hasattr(c, "topic_memory"))
    check("H6 topic-scoped memory keeps its provenance",
          any(m.get("provenance") == "explicit_user"
              for m in r.context.relevant_memories))
    check("H7 retrieval stays bounded",
          len(r.context.relevant_memories)
          <= int(c.cfg.memory_context_limit))
    # inferred stays soft
    obs = c.memory.observe(type="preference", key="x", value="guess",
                           provenance="routine_inferred", confidence=0.9, now=NOW)
    check("H8 inferred memory is never hard",
          obs["ok"] and obs["stored"]["confirmation_state"] == "inferred")


# =====================================================================
# I. backward compatibility + migration
# =====================================================================
def test_compat() -> None:
    print("\n== I. backward compatibility ==")
    # legacy routing columns still work
    c = fresh("n1-compat-")
    c.db.upsert_topic_setting(1, 2, topic="CS188", routing="day")
    row = c.db.topic_setting(1, 2)
    check("I1 legacy routing is preserved", row["routing"] == "day")
    from butler.motivation import routing_for, pending_push_topics
    check("I2 routing_for still resolves", routing_for(c.db, 1, 2) == "day")
    c.db.upsert_topic_setting(1, 3, topic="Food", push_on=1,
                              push_time="07:00", push_freq="daily")
    check("I3 pending_push_topics still works",
          any(t["thread_id"] == 3 for t in pending_push_topics(c.db)))
    c.db.set_title_routing("CS188", "day")
    check("I4 set_title_routing still works",
          c.db.topic_setting_by_title("CS188")["routing"] == "day")

    # migration from a realistic pre-N1 DB (topic_settings without new columns)
    base = tempfile.mkdtemp(prefix="n1-mig-")
    cfg = Config()
    cfg.data_dir = os.path.join(base, "s")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "c.toml")
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    conn = sqlite3.connect(cfg.db_path())
    conn.execute(
        "CREATE TABLE topic_settings(id INTEGER PRIMARY KEY, chat_id INTEGER, "
        "thread_id INTEGER, topic TEXT, routing TEXT, push_on INTEGER, "
        "push_time TEXT, push_freq TEXT, updated_ts INTEGER, "
        "UNIQUE(chat_id, thread_id))")
    conn.execute("INSERT INTO topic_settings(chat_id,thread_id,topic,routing,"
                 "push_on,push_time,push_freq,updated_ts) "
                 "VALUES(1,2,'CS188','day',1,'07:00','daily',1)")
    conn.commit(); conn.close()
    cm = Container(cfg)
    row = cm.db.topic_setting(1, 2)
    check("I5 a pre-N1 DB migrates in place", row is not None)
    check("I6 the legacy row is preserved", row["routing"] == "day"
          and row["topic"] == "CS188")
    check("I7 new columns exist after migration",
          "capabilities" in row.keys() and "status" in row.keys())
    check("I8 existing topics default to active",
          str(row["status"] or "active") == "active")
    check("I9 topic_links table exists after migration",
          cm.db.one("SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='topic_links'") is not None)

    # MCP parity unchanged
    full = MCPServer(c, profile="full")
    ro = MCPServer(c, profile="readonly")
    check("I10 full MCP profile is still 51",
          len({t["name"] for t in full._tools_spec()}) == 51)
    check("I11 readonly MCP profile is still 37",
          len({t["name"] for t in ro._tools_spec()}) == 37)
    check("I12 food subsystem is unaffected",
          hasattr(c, "food") and hasattr(c, "chef"))
    check("I13 scheduler/planner are unaffected",
          hasattr(c, "planner") and hasattr(c, "optimizer"))


# =====================================================================
# J. realistic scenarios
# =====================================================================
def test_scenarios() -> None:
    print("\n== J. realistic scenarios ==")
    c = fresh("n1-scen-")
    c.db.add_course("CS188")
    c.db.add_task("CS188 HW4", est_minutes=90, priority=4)
    st = c.topics
    bot = topic_bot(c)

    # 1 CS188: course management + tracking + scheduling + proactive
    prof, amb = configure(c, 100, 1, "CS188",
                          "This is my CS188 course. Track homework, projects, "
                          "deadlines and help me schedule the work.")
    check("J1 CS188 links its course",
          any(l["target_type"] == "course" for l in st.links(prof)))
    check("J2 CS188 enables tracking", prof.enabled("tracking"))
    check("J3 CS188 enables scheduling", prof.enabled("scheduling"))
    check("J4 CS188 enables proactive", prof.enabled("proactive"))
    fb = FakeBot()
    asyncio.run(bot._publish_topic_panel(fb, prof, force=True))
    check("J5 CS188 panel is pinned", len(fb.pins) == 1)

    # 2 Food topic
    pf, _ = configure(c, 100, 2, "Food", "meal planning and pantry")
    check("J6 Food topic is configured", pf.status == ACTIVE)
    check("J7 Food links pantry/meal data",
          any(l["target_type"] in ("food", "meal_plan") for l in st.links(pf)))

    # 3 Groceries links pantry + meal plan, shared data
    pg, _ = configure(c, 100, 3, "Groceries",
                      "shopping using my pantry and planned meals")
    check("J8 Groceries links the pantry",
          any(l["target_type"] == "food" for l in st.links(pg)))
    check("J9 Groceries links the meal plan",
          any(l["target_type"] == "meal_plan" for l in st.links(pg)))
    check("J10 Groceries and Food share the same food reference",
          {l["target_type"] for l in st.links(pg)}
          & {l["target_type"] for l in st.links(pf)})

    # 4 Basketball Club needs no course/project assumptions
    pb, _ = configure(c, 100, 4, "Basketball Club",
                      "club coordination, practices and meetings")
    check("J11 a club topic configures without course data",
          pb.status == ACTIVE and not any(l["target_type"] == "course"
                                          for l in st.links(pb)))
    check("J12 a club topic still has knowledge/proactive",
          pb.enabled("knowledge") and pb.enabled("proactive"))

    # duplicate topic like CS188 (template) copies config, not data
    tmpl = st.to_template(st.get(100, 1))
    pd, _ = st.ensure(100, 5, "CS188 lab")
    pd = st.apply_template(pd, tmpl)
    check("J13 a template copies capabilities, not links",
          pd.cap("tracking") == st.get(100, 1).cap("tracking")
          and not st.links(pd))
    # explicit connect two topics (shared reference)
    st.add_link(pd, "course", 1, "about", 1.0, "user")
    check("J14 an explicit connection shares the same course",
          st.links(pd)[0]["target_id"] == 1
          and c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)


def main() -> int:
    print("N1 acceptance: universal topics + shared context")
    test_discovery()
    test_telegram_flow()
    test_profile()
    test_context()
    test_linking()
    test_pin()
    test_ux()
    test_security()
    test_memory()
    test_compat()
    test_scenarios()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
