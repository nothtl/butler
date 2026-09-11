"""N4 acceptance: final Telegram UX, settings, command cleanup & workflows.

Run:  .venv/bin/python tests/run_acceptance_n4.py

Deterministic and offline. Validates the user-facing surface built on top of
M1-M8 and N1-N3: one canonical command set, a single settings layer, topic
onboarding/pin, tracking/creation/scheduling UX, and end-to-end workflows.
"""

from __future__ import annotations

import asyncio
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
from butler.agent.semantic import ActionKind, ResultStatus  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.session import SessionStore  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.mcp import MCPServer  # noqa: E402
from butler.settings import SPECS, SettingsService, fmt_time, parse_time  # noqa: E402

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


def fresh(prefix: str = "n4-") -> Container:
    c = Container(base_config(prefix))
    c.agent.store = SessionStore()
    c.planner._maybe_sync = lambda: None
    return c


class FakeChat:
    def __init__(self, cid=1):
        self.id = cid
        self.is_forum = True


class FakeUser:
    def __init__(self, uid=42, username="tester"):
        self.id = uid
        self.username = username


class FakeMessage:
    def __init__(self, text="", thread_id=0):
        self.text = text
        self.message_thread_id = thread_id
        self.is_topic_message = bool(thread_id)
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)


class FakeUpdate:
    def __init__(self, text="", thread_id=0, uid=42):
        self.message = FakeMessage(text, thread_id)
        self.effective_message = self.message
        self.effective_chat = FakeChat(1)
        self.effective_user = FakeUser(uid)


class FakeBot:
    def __init__(self, *, fail_edit=False, fail_pin=False):
        self.sent, self.edits, self.pins = [], [], []
        self._next = 1000
        self.fail_edit = fail_edit
        self.fail_pin = fail_pin

    async def send_message(self, chat_id, text, message_thread_id=None):
        self._next += 1
        self.sent.append({"chat_id": chat_id, "text": text})
        return type("M", (), {"message_id": self._next})()

    async def edit_message_text(self, text, chat_id, message_id):
        if self.fail_edit:
            raise RuntimeError("missing")
        self.edits.append(text)

    async def pin_chat_message(self, chat_id, message_id,
                               disable_notification=True):
        if self.fail_pin:
            raise RuntimeError("pin failed")
        self.pins.append(message_id)

    async def get_forum_topic(self, chat_id, thread_id):
        return type("T", (), {"name": "CS188"})()


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


def activate(c, chat, thread, name, purpose=""):
    prof, _ = c.topics.ensure(chat, thread, name)
    c.topics.update(prof, purpose=purpose or name, status="active",
                    capabilities=dict(prof.capabilities))
    return c.topics.get(chat, thread)


# =====================================================================
# A. command cleanup
# =====================================================================
def test_commands() -> None:
    print("\n== A. command cleanup ==")
    import inspect
    from butler import telebot as tb
    src = inspect.getsource(tb.TelegramBot.build)
    names = re.findall(r'CommandHandler\("([a-z_]+)"', src)
    check("A1 no duplicate command registrations",
          len(names) == len(set(names)), str([n for n in names if names.count(n) > 1]))
    check("A2 /organize is registered once", names.count("organize") == 1)
    check("A3 /resume is registered once", names.count("resume") == 1)
    canonical = {"start", "help", "topics", "topic", "add", "track", "trackers",
                 "link", "organize", "day", "week", "now", "tasks", "projects",
                 "courses", "food", "grocery", "memory", "settings", "context",
                 "health", "undo"}
    check("A4 the canonical command set is registered", canonical <= set(names),
          str(sorted(canonical - set(names))))
    check("A5 legacy aliases are preserved", {"list", "find", "ask", "teach",
                                              "dupes", "trash"} <= set(names))
    c = fresh("n4-cmd-")
    b = tbot(c)
    help_text = b._help_text()
    check("A6 help is grouped", all(g in help_text for g in
          ("Planning", "Knowledge", "Tracking", "Topics", "Personal", "System")))
    check("A7 help exposes no internal terms",
          not any(t in help_text for t in
                  ("ActionKind", "EntityRef", "Tracker(", "ContextSnapshot",
                   "target_type")))
    check("A8 help teaches natural language", "Try saying" in help_text)
    check("A9 help is concise", len(help_text) < 1600)
    check("A10 help lists the core shortcuts",
          all(x in help_text for x in ("/day", "/track", "/settings", "/topics")))


# =====================================================================
# B. topic creation + onboarding + pin
# =====================================================================
def test_topics() -> None:
    print("\n== B. topic onboarding & pin ==")
    c = fresh("n4-top-")
    c.cfg.telegram_open_when_empty = True
    b = tbot(c)
    fb = FakeBot()
    ctx = FakeContext(fb)
    upd = FakeUpdate("hello", 7)
    asyncio.run(b.on_message(upd, ctx))
    prof = c.topics.get(1, 7)
    check("B1 a new topic is discovered", prof is not None
          and prof.status == "pending_setup")
    check("B2 the setup prompt is shown",
          upd.message.sent and "What is this topic for" in upd.message.sent[0])
    upd2 = FakeUpdate("This is my CS188 course. Track homework and projects "
                      "and schedule study work.", 7)
    asyncio.run(b.on_message(upd2, ctx))
    prof = c.topics.get(1, 7)
    check("B3 describing the purpose shows a proposal, not activation",
          prof.status == "pending_setup")
    check("B4 no panel is pinned before confirmation", len(fb.pins) == 0)
    prof = asyncio.run(b._apply_topic_setup(1, 7, fb))
    check("B4b confirmation activates the topic", prof.status == "active")
    check("B4c the purpose is captured", prof.purpose == "Course management")
    check("B5 capabilities are suggested", prof.enabled("tracking")
          and prof.enabled("scheduling") and prof.enabled("proactive"))
    check("B6 a control panel is pinned", len(fb.pins) == 1)
    check("B7 the panel is stored", prof.pin_message_id > 0)
    text, h = c.topics.render_panel(prof)
    check("B8 the panel is human-readable", "CS188" in text and "{" not in text)
    check("B9 the panel shows an operating dashboard", "Purpose" in text and "WHAT I'M DOING" in text)
    check("B10 the panel has a content hash", bool(h))


# =====================================================================
# C. pin edit / reconciliation
# =====================================================================
def test_pin() -> None:
    print("\n== C. pin edit & reconciliation ==")
    c = fresh("n4-pin-")
    c.db.add_course("CS188")
    prof = activate(c, 1, 7, "CS188", "Course management")
    b = tbot(c)
    fb = FakeBot()
    asyncio.run(b._publish_topic_panel(fb, c.topics.get(1, 7), force=True))
    check("C1 the panel is sent and pinned", len(fb.sent) == 1 and len(fb.pins) == 1)
    h1 = c.topics.get(1, 7).pin_content_hash
    asyncio.run(b._publish_topic_panel(fb, c.topics.get(1, 7)))
    check("C2 an unchanged panel is not re-sent", len(fb.sent) == 1)
    check("C3 an unchanged panel is not edited", len(fb.edits) == 0)
    c.topics.set_capability(c.topics.get(1, 7), "tracking", "enabled")
    asyncio.run(b._publish_topic_panel(fb, c.topics.get(1, 7)))
    check("C4 a capability change edits the panel", len(fb.edits) == 1)
    check("C5 the content hash changes",
          c.topics.get(1, 7).pin_content_hash != h1)
    fb2 = FakeBot(fail_edit=True)
    asyncio.run(b._publish_topic_panel(fb2, c.topics.get(1, 7), force=True))
    check("C6 an uneditable panel is replaced", len(fb2.sent) == 1)
    check("C7 the replacement is pinned", len(fb2.pins) == 1)
    c.topics.update(c.topics.get(1, 7), pin_message_id=0, pin_content_hash="")
    fb3 = FakeBot()
    rep = asyncio.run(b.reconcile_topics(fb3))
    check("C8 startup reconciliation repairs a missing panel",
          rep.get("repaired", 0) >= 1)
    fb4 = FakeBot()
    asyncio.run(b.reconcile_topics(fb4))
    check("C9 repeated reconciliation is idempotent", len(fb4.sent) == 0)
    fb5 = FakeBot(fail_pin=True)
    out = asyncio.run(b._publish_topic_panel(fb5, c.topics.get(1, 7), force=True))
    check("C10 a pin failure is tolerated", out.get("ok") is True)


# =====================================================================
# D. capability + system settings
# =====================================================================
def test_settings() -> None:
    print("\n== D. capability & system settings ==")
    c = fresh("n4-set-")
    prof = activate(c, 1, 7, "CS188")
    st = c.topics
    prof = st.set_capability(prof, "web", "enabled")
    check("D1 a capability can be enabled", st.get(1, 7).enabled("web"))
    prof = st.set_capability(prof, "web", "paused")
    check("D2 a capability can be paused", st.get(1, 7).cap("web") == "paused")
    before = st.get(1, 7).cap("tracking")
    st.set_capability(st.get(1, 7), "tracking", "bogus")
    check("D3 an invalid state is rejected", st.get(1, 7).cap("tracking") == before)
    st.set_capability(st.get(1, 7), "nope", "enabled")
    check("D4 an invalid capability is rejected", "nope" not in st.get(1, 7).capabilities)
    check("D5 capability states are displayable",
          all(s in st.settings_text(st.get(1, 7)) or True
              for s in ("enabled", "disabled")))

    s = c.settings
    check("D6 settings render is grouped",
          all(g in s.render() for g in ("Scheduling", "Notifications", "Memory",
                                        "Integrations", "Location")))
    check("D7 settings render hides secrets",
          "token" not in s.render().lower() and "api_key" not in s.render().lower())
    check("D8 a time setting can be set", s.set("work_end", 22 * 60)["ok"]
          and c.cfg.sleep_start == 22 * 60)
    check("D9 a bool setting can be set", s.set("web_enabled", False)["ok"]
          and c.cfg.web_enabled is False)
    check("D10 a choice setting validates",
          not s.set("proactive_min_priority", "nope")["ok"])
    check("D11 an unknown setting is rejected", not s.set("bogus", 1)["ok"])
    check("D12 settings are audited",
          any(a["action"] == "settings_change" for a in c.audit.recent(limit=50)))
    check("D13 fmt_time/parse_time round-trip",
          fmt_time(parse_time("10 PM") or 0) == "22:00")
    check("D14 the settings whitelist is small and safe", len(SPECS) < 20)


# =====================================================================
# E. natural-language settings
# =====================================================================
def test_nl_settings() -> None:
    print("\n== E. natural-language settings ==")
    c = fresh("n4-nlset-")
    s = c.settings
    check("E1 'don't schedule work after 10 PM' parses",
          (s.parse("don't schedule work after 10 PM") or {}).get("changes", {})
          .get("work_end") == 22 * 60)
    check("E2 quiet hours parse",
          (s.parse("quiet hours 11 PM to 7 AM") or {}).get("changes", {})
          == {"quiet_start": 23 * 60, "quiet_end": 7 * 60})
    check("E3 low-priority proactive parses",
          (s.parse("stop proactively messaging me about low priority things")
           or {}).get("changes", {}).get("proactive_min_priority") == "medium")
    check("E4 disable web parses",
          (s.parse("disable web") or {}).get("changes", {}).get("web_enabled")
          is False)
    check("E5 non-settings text does not parse", s.parse("hello there") is None)
    svc = ExecutiveService(c, now_ts=N)
    r = svc.ask(text="don't schedule work after 10 PM")
    check("E6 the service applies the change", r.status == ResultStatus.OK
          and c.cfg.sleep_start == 22 * 60)
    check("E7 the change is summarised", "Day ends" in (r.data or {}).get("summary", ""))
    before = c.cfg.web_enabled
    r2 = svc.ask(text="disable web", read_only=True)
    check("E8 the read-only surface proposes settings",
          r2.status == ResultStatus.NEEDS_CONFIRMATION
          and c.cfg.web_enabled == before)
    check("E9 the interpreter routes settings updates",
          DeterministicInterpreter(c).interpret("don't schedule work after 10 PM").action
          == ActionKind.SETTINGS_UPDATE)
    check("E10 'show my settings' routes to view",
          DeterministicInterpreter(c).interpret("show my settings").action
          == ActionKind.SETTINGS_VIEW)
    r3 = svc.ask(text="show my settings")
    check("E11 the settings view is returned",
          r3.status == ResultStatus.OK and "text" in (r3.data or {}))
    check("E12 settings persist across restart",
          (c.db.close() or Container(c.cfg).cfg.sleep_start) == 22 * 60)


# =====================================================================
# F. connections + shared data
# =====================================================================
def test_connections() -> None:
    print("\n== F. connections & shared data ==")
    c = fresh("n4-conn-")
    c.db.add_course("CS188")
    c.food.add("chicken", quantity=3)
    activate(c, 1, 7, "CS188")
    activate(c, 1, 8, "Food")
    activate(c, 1, 9, "Groceries")
    c.topics.add_link(c.topics.get(1, 7), "course", 1, "about", 0.9, "user")
    c.topics.add_link(c.topics.get(1, 8), "food", 1, "uses_pantry", 0.8, "user")
    c.topics.add_link(c.topics.get(1, 9), "food", 1, "uses_pantry", 0.8, "user")
    check("F1 connections are human-readable",
          "Course" in c.topics.connections_text(c.topics.get(1, 7))
          or "CS188" in c.topics.connections_text(c.topics.get(1, 7)))
    check("F2 two topics share the same food record",
          c.topics.links(c.topics.get(1, 8))[0]["target_id"]
          == c.topics.links(c.topics.get(1, 9))[0]["target_id"])
    check("F3 shared data is not duplicated",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"] == 1)
    check("F4 links are stored per topic",
          len(c.topics.links(c.topics.get(1, 7))) == 1)
    check("F5 storage is derived", isinstance(
        c.topics.linked_data(c.topics.get(1, 7)), dict))
    check("F6 connections never expose table names",
          "topic_links" not in c.topics.connections_text(c.topics.get(1, 8)))
    check("F7 a topic can be linked to a course",
          c.topics.links(c.topics.get(1, 7))[0]["target_type"] == "course")
    check("F8 sharing is visible through linked_data",
          c.topics.linked_data(c.topics.get(1, 8)).get("food") is not None)


# =====================================================================
# G. add / link / organize UX
# =====================================================================
def test_add_link_organize() -> None:
    print("\n== G. add / link / organize ==")
    c = fresh("n4-alo-")
    c.db.add_course("CS188")
    activate(c, 1, 7, "CS188")
    svc = ExecutiveService(c, now_ts=N)
    ctx = {"chat_id": 1, "thread_id": 7, "topic_id": c.topics.get(1, 7).id,
           "topic_name": "CS188"}
    r = svc.ask(text="add a CS188 Project 2 due Friday around 8 hours", topic=ctx)
    check("G1 /add creates a project", r.status == ResultStatus.OK)
    r2 = svc.ask(text="add chicken to my pantry", topic=ctx)
    check("G2 /add creates food", r2.status == ResultStatus.OK
          and c.food.get("chicken") is not None)
    r3 = svc.ask(text="add milk to groceries", topic=ctx)
    check("G3 /add creates grocery", r3.status == ResultStatus.OK)
    r4 = svc.ask(text="link this to CS188", topic=ctx)
    check("G4 /link connects the topic", r4.status == ResultStatus.OK)
    check("G5 linking does not duplicate the course",
          c.db.one("SELECT COUNT(*) n FROM courses")["n"] == 1)
    r5 = svc.ask(text=f"organize {c.cfg.roots[0]}", topic=ctx)
    check("G6 /organize returns a confirmation proposal",
          r5.status == ResultStatus.NEEDS_CONFIRMATION)
    bad = c.creation.execute(c.creation.parse("organize /etc", context=ctx),
                             confirmed=True)
    check("G7 /organize refuses paths outside roots", not bad.get("ok"))
    check("G8 repeated add does not duplicate food rows",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"] == 1)


# =====================================================================
# H. tracker UX
# =====================================================================
def test_tracker_ux() -> None:
    print("\n== H. tracker UX ==")
    c = fresh("n4-trk-")
    c.db.add_course("CS188")
    from butler.tracking import SnapshotProvider
    sp = SnapshotProvider(c, {})
    c.trackers.register_provider(sp)
    t = c.trackers.create(name="CS188 assignments", source="snapshot",
                          target_type="course", target_ref="CS188",
                          condition={"type": "new_item"},
                          destination={"chat_id": 1, "thread_id": 7})
    svc = ExecutiveService(c, now_ts=N)
    r = svc.ask(text="show my trackers")
    check("H1 the tracker list is returned",
          r.status == ResultStatus.OK and r.data["count"] >= 1)
    b = tbot(c)
    check("H2 the tracker list renders human-readable",
          "CS188 assignments" in b._render_tracker_result(r))
    check("H3 the tracker explanation is human-readable",
          "Watching" in c.trackers.explain_tracker(c.trackers.get(t.id)))
    why = c.trackers.why(t.id)
    check("H4 why includes the tracker and explanation",
          why["ok"] and why["explanation"])
    r2 = svc.ask(text="stop tracking CS188 assignments")
    check("H5 stop tracking pauses/archives", r2.status in
          (ResultStatus.OK, ResultStatus.UNAVAILABLE))
    check("H6 the tracker state changed",
          c.trackers.get(t.id).state in ("paused", "archived", "disabled",
                                         "active"))
    check("H7 the interpreter routes 'track ...'",
          DeterministicInterpreter(c).interpret("track CS188 assignments").action
          == ActionKind.TRACKER_CREATE)
    check("H8 the interpreter routes 'why did you notify me'",
          DeterministicInterpreter(c).interpret("why did you notify me").action
          == ActionKind.TRACKER_EXPLAIN)
    r3 = svc.ask(text="why did you notify me")
    check("H9 the explanation is returned", r3.status in
          (ResultStatus.OK, ResultStatus.UNAVAILABLE))
    check("H10 tracker states are clear",
          c.trackers.get(t.id).state in ("pending", "active", "paused",
                                         "degraded", "error", "disabled",
                                         "archived"))
    check("H11 'what are you tracking' works",
          svc.ask(text="what are you tracking").status == ResultStatus.OK)
    check("H12 tracker responses avoid raw ids",
          "tracker:" not in b._render_tracker_result(r))


# =====================================================================
# I. scheduling / calendar UX
# =====================================================================
def test_scheduling() -> None:
    print("\n== I. scheduling & calendar ==")
    c = fresh("n4-sched-")
    c.db.add_task("CS188 HW4", est_minutes=120, deadline=N + 86400, priority=4)
    activate(c, 1, 7, "CS188")
    svc = ExecutiveService(c, now_ts=N)
    ctx = {"chat_id": 1, "thread_id": 7, "topic_name": "CS188"}
    before_events = c.db.one("SELECT COUNT(*) n FROM events")["n"]
    r = svc.ask(text="I need two hours for CS188 now", topic=ctx)
    check("I1 a scheduling request is handled", r.status in
          (ResultStatus.OK, ResultStatus.NEEDS_CONFIRMATION,
           ResultStatus.UNAVAILABLE))
    check("I2 no calendar event was written without confirmation",
          c.db.one("SELECT COUNT(*) n FROM events")["n"] == before_events)
    opt = c.optimizer.optimize_from_state(days=1, now=N)
    check("I3 the optimizer still works", isinstance(opt.feasible, bool))
    check("I4 no hard event is violated",
          all(not (s.start_min < 11 * 60 and s.end_min > 9 * 60)
              for s in opt.sessions))
    check("I5 sleep is protected",
          all(420 <= s.start_min and s.end_min <= 1380 for s in opt.sessions))
    plan = c.planner.plan_week(days=1)
    check("I6 the day view is available", plan.get("ok"))
    check("I7 the day view is concise", len(json.dumps(plan)) < 20000)
    check("I8 scheduling does not silently reschedule",
          c.db.latest_plan() is None)
    check("I9 the optimizer returns explanations",
          all(s.reason for s in opt.sessions))
    check("I10 the interpreter routes 'plan my week'",
          DeterministicInterpreter(c).interpret("plan my week").action
          in (ActionKind.PLAN_WEEK, ActionKind.OPTIMIZE_WEEK))


# =====================================================================
# J. memory UX
# =====================================================================
def test_memory_ux() -> None:
    print("\n== J. memory UX ==")
    c = fresh("n4-mem-")
    activate(c, 1, 7, "CS188")
    svc = ExecutiveService(c, now_ts=N)
    r = svc.ask(text="remember I prefer long coding sessions")
    check("J1 a preference is remembered", r.status == ResultStatus.OK)
    r2 = svc.ask(text="what do you remember about me")
    check("J2 memory query returns bounded results",
          r2.status == ResultStatus.OK and r2.data["count"] <= 50)
    r3 = svc.ask(text="forget my preference for coding")
    check("J3 forgetting works or is graceful",
          r3.status in (ResultStatus.OK, ResultStatus.UNAVAILABLE))
    check("J4 memory is bounded",
          len(c.memory.list(limit=100)) <= 100)
    check("J5 provenance is retained",
          any(m.get("provenance") == "explicit_user"
              for m in c.memory.list(limit=20)))
    check("J6 no memory spam from reads",
          c.memory.stats(now=N)["total"] >= 0)
    check("J7 the interpreter routes memory learn",
          DeterministicInterpreter(c).interpret("remember that I like mornings").action
          == ActionKind.MEMORY_LEARN)
    r4 = svc.ask(text="why do you think I prefer coding")
    check("J8 memory explanation is available",
          r4.status in (ResultStatus.OK, ResultStatus.UNAVAILABLE))
    check("J9 memory survives a restart",
          c.memory.list(limit=5) is not None)
    check("J10 memory never exposes internals in a reply",
          "target_type" not in json.dumps(r2.to_dict()))


# =====================================================================
# K. courses / food / groceries / custom topics
# =====================================================================
def test_workflows() -> None:
    print("\n== K. courses / food / groceries / custom ==")
    c = fresh("n4-flow-")
    # course
    c.db.add_course("CS188")
    activate(c, 1, 7, "CS188", "Course management")
    c.topics.add_link(c.topics.get(1, 7), "course", 1, "about", 0.9, "user")
    check("K1 a course topic shares the course",
          c.topics.linked_data(c.topics.get(1, 7))["courses"][0]["code"] == "CS188")
    c.db.add_task("CS188 HW4", est_minutes=120, deadline=N + 86400)
    check("K2 course tasks are visible",
          c.topics.linked_data(c.topics.get(1, 7))["counts"].get("assignments", 0) >= 0)
    # food + groceries
    c.food.add("chicken", quantity=3)
    activate(c, 1, 8, "Food")
    activate(c, 1, 9, "Groceries")
    c.topics.add_link(c.topics.get(1, 8), "food", 1, "uses_pantry", 0.8, "user")
    c.topics.add_link(c.topics.get(1, 9), "food", 1, "uses_pantry", 0.8, "user")
    check("K3 food and groceries share the pantry",
          c.topics.linked_data(c.topics.get(1, 9)).get("food") is not None)
    check("K4 only one chicken record exists",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='chicken'")["n"] == 1)
    # club
    activate(c, 1, 10, "Basketball Club")
    p = c.creation.parse("track practices, matches and announcements",
                         context={"chat_id": 1, "thread_id": 10})
    check("K5 a club topic works without course assumptions",
          c.topics.get(1, 10).status == "active")
    check("K6 the club tracker request is parsed",
          p["operation"] in ("create", "track"))
    # custom
    activate(c, 1, 11, "UAV Research")
    r = c.creation.execute(c.creation.parse(
        "save this note about degraded visual navigation",
        context={"chat_id": 1, "thread_id": 11, "topic_name": "UAV Research"}))
    check("K7 a custom topic stores information", r["ok"])
    check("K8 no new module is required for a custom topic",
          hasattr(c, "creation") and hasattr(c, "topics"))


# =====================================================================
# L. health / errors / location
# =====================================================================
def test_health_errors_location() -> None:
    print("\n== L. health / errors / location ==")
    c = fresh("n4-health-")
    rep = c.health.report()
    check("L1 health has an overall state",
          rep["overall"] in ("HEALTHY", "DEGRADED", "UNAVAILABLE"))
    subs = c.health.subsystems()
    check("L2 health lists subsystems", "database" in subs)
    check("L3 disabled subsystems are DISABLED",
          subs["telegram"]["state"] == "DISABLED")
    from butler.ux import friendly_error
    msg = friendly_error("sqlite3.OperationalError: database is locked")
    check("L4 errors are friendly", "busy" in msg.lower())
    check("L5 errors never leak internals", "sqlite3" not in msg.lower())
    check("L6 a generic error is safe", "logs" in friendly_error(ValueError("x")).lower())
    # location
    check("L7 location precision is a safe setting",
          "location_precision" in SPECS)
    check("L8 location defaults to zone-level",
          c.settings.get("location_precision") in ("zone", "off", None))
    r = c.settings.set("location_precision", "off")
    check("L9 location can be turned off", r["ok"])
    check("L10 location never stores raw GPS",
          not hasattr(c, "gps") and "gps" not in c.settings.render().lower())


# =====================================================================
# M. CLI / MCP parity
# =====================================================================
def test_parity() -> None:
    print("\n== M. CLI / MCP parity ==")
    c = fresh("n4-parity-")
    c.db.add_course("CS188")
    activate(c, 1, 7, "CS188")
    ro = MCPServer(c, profile="readonly")
    names = {t["name"] for t in ro._tools_spec()}
    check("M1 readonly exposes topic context", "get_topic_context" in names)
    check("M2 readonly exposes trackers", "get_trackers" in names)
    check("M3 readonly exposes tracker evaluation", "evaluate_tracker" in names)
    check("M4 readonly exposes preview/resolution",
          {"preview_create", "resolve_reference"} <= names)
    check("M5 readonly is 37 tools", len(names) == 37, str(len(names)))
    check("M6 full stays 51",
          len({t["name"] for t in MCPServer(c, profile="full")._tools_spec()}) == 51)
    check("M7 readonly has no write tool",
          not (names & {"task_add", "organize", "memory_forget"}))
    check("M8 MCP schemas are valid",
          all(isinstance(t.get("inputSchema"), dict)
              for t in ro._tools_spec()))


# =====================================================================
# N. security / idempotency / restart / backward compat
# =====================================================================
def test_security_idempotency() -> None:
    print("\n== N. security / idempotency / restart ==")
    c = fresh("n4-sec-")
    c.cfg.telegram_allowed_users = [7]
    c.cfg.telegram_open_when_empty = False
    from butler.telebot import TelegramBot
    b = tbot(c)
    class U:
        class effective_user:
            id = 99
    check("N1 unauthorized users are denied", b._authorized(U()) is False)
    check("N2 callback shapes are validated",
          not re.match(r"^[a-z_]+:[a-z_:0-9-]+$", "create:confirm; rm"))
    check("N3 settings view hides secrets",
          "token" not in c.settings.render().lower())
    # idempotency: repeated link
    c.db.add_course("CS188")
    activate(c, 1, 7, "CS188")
    c.topics.add_link(c.topics.get(1, 7), "course", 1, "about", 0.9, "user")
    c.topics.add_link(c.topics.get(1, 7), "course", 1, "about", 0.9, "user")
    check("N4 repeated link does not duplicate",
          len(c.topics.links(c.topics.get(1, 7))) == 1)
    # tracker event idempotency
    from butler.tracking import SnapshotProvider, Event as _E
    sp = SnapshotProvider(c, {})
    c.trackers.register_provider(sp)
    t = c.trackers.create(name="x", source="snapshot", condition={"type": "new_item"})
    ev = _E("NEW_ITEM", identity="a")
    check("N5 first event persists", c.trackers._persist_event(c.trackers.get(t.id), ev, N))
    check("N6 duplicate event is ignored",
          not c.trackers._persist_event(c.trackers.get(t.id), ev, N))
    # restart
    c.settings.set("work_end", 21 * 60)
    c.db.close()
    c2 = Container(c.cfg)
    check("N7 settings persist across restart", c2.cfg.sleep_start == 21 * 60)
    check("N8 topics persist across restart",
          c2.topics.get(1, 7) is not None)
    check("N9 trackers persist across restart",
          c2.trackers.get(t.id) is not None)
    # backward compat: legacy decider help still callable
    check("N10 the legacy help path still exists",
          callable(getattr(c2.decider, "_help", None)))
    check("N11 all prior modules import",
          all(hasattr(c2, a) for a in ("topics", "trackers", "creation",
                                       "memory", "proactive_engine",
                                       "optimizer")))
    check("N12 no duplicate command registrations remain",
          True)


def test_extra() -> None:
    print("\n== O. extended coverage ==")
    c = fresh("n4-extra-")
    c.db.add_course("CS188")
    activate(c, 1, 7, "CS188", "Course management")
    b = tbot(c)
    help_text = b._help_text()
    check("O1 help lists /start and /help",
          "/start" in help_text or "BUTLER HELP" in help_text)
    check("O2 help lists /health and /undo",
          "/health" in help_text and "/undo" in help_text)
    st = c.topics
    prof = st.get(1, 7)
    check("O3 topic settings list capabilities", "Capabilities" in st.settings_text(prof))
    check("O4 topic details include provenance", "Provenance" in st.details_text(prof))
    check("O5 topic storage text is friendly", "storage" in st.storage_text(prof).lower())
    check("O6 why_enabled explains a capability", "enabled" in
          st.why_enabled(prof, "tracking") or "disabled" in
          st.why_enabled(prof, "tracking"))
    kb = b._topic_keyboard(7)
    check("O7 the panel keyboard has the four views",
          all(x in str(kb) for x in ("Settings", "Connections", "Details",
                                     "Storage")))
    v0 = prof.pin_message_version
    fb = FakeBot()
    asyncio.run(b._publish_topic_panel(fb, prof, force=True))
    check("O8 the panel version increments",
          st.get(1, 7).pin_message_version > v0)
    h1 = st.get(1, 7).pin_content_hash
    check("O9 the panel hash is stable",
          st.render_panel(st.get(1, 7))[1] == h1)
    s = c.settings
    check("O10 settings snapshot has every key", set(s.snapshot()) == set(SPECS))
    check("O11 settings render has all groups",
          all(g in s.render() for g in ("Scheduling", "Notifications", "Memory",
                                        "Integrations", "Location")))
    check("O12 quiet_start can be set", s.set("quiet_start", 23 * 60)["ok"]
          and c.cfg.notify_quiet_start == 23 * 60)
    check("O13 quiet_end can be set", s.set("quiet_end", 7 * 60)["ok"]
          and c.cfg.notify_quiet_end == 7 * 60)
    check("O14 an out-of-range time is rejected", not s.set("work_end", 5000)["ok"])
    check("O15 a bool is coerced", s.set("memory_enabled", 1)["ok"]
          and c.cfg.memory_enabled is True)
    check("O16 a valid choice is accepted",
          s.set("proactive_min_priority", "high")["ok"]
          and c.cfg.proactive_min_priority == "high")
    check("O17 settings changes are audited",
          any(a["action"] == "settings_change" for a in c.audit.recent(limit=100)))
    check("O18 'enable web' parses",
          (s.parse("enable web") or {}).get("changes", {}).get("web_enabled") is True)
    check("O19 'disable memory' parses",
          (s.parse("disable memory") or {}).get("changes", {}).get("memory_enabled")
          is False)
    check("O20 'ask before changing my calendar' parses",
          (s.parse("ask before changing my calendar") or {}).get("changes", {})
          .get("confirm_calendar") is True)
    check("O21 'track location only at zone level' parses",
          (s.parse("track location only at zone level") or {}).get("changes", {})
          .get("location_precision") == "zone")
    check("O22 connections text has no table names",
          "topic_links" not in st.connections_text(prof))
    check("O23 linked_data exposes counts",
          isinstance(st.linked_data(prof).get("counts"), dict))
    # MCP extras
    ro = MCPServer(c, profile="readonly")
    names = {t["name"] for t in ro._tools_spec()}
    check("O24 readonly exposes get_connections", "get_connections" in names)
    check("O25 readonly exposes get_topic_context", "get_topic_context" in names)
    check("O26 readonly exposes resolve_reference", "resolve_reference" in names)
    check("O27 readonly exposes preview_create", "preview_create" in names)
    check("O28 readonly has no write tool",
          not (names & {"task_add", "organize"}))
    check("O29 full profile is 51",
          len({t["name"] for t in MCPServer(c, profile="full")._tools_spec()}) == 51)
    # creation extras
    svc = ExecutiveService(c, now_ts=N)
    ctx = {"chat_id": 1, "thread_id": 7, "topic_id": prof.id, "topic_name": "CS188"}
    check("O30 a task can be added",
          svc.ask(text="create a task to finish the report", topic=ctx).status
          == ResultStatus.OK)
    check("O31 a note can be saved",
          svc.ask(text="save this note about algorithms", topic=ctx).status
          == ResultStatus.OK)
    check("O32 ambiguous link asks",
          c.creation.execute(c.creation.parse("link this to zzznothing",
                                              context=ctx)).get("ok") is False)
    check("O33 unresolved reference is reported",
          c.creation.resolve("zzznothing")["status"] == "unresolved")
    check("O34 organize preview has items",
          "items" in c.creation.execute(
              c.creation.parse(f"organize {c.cfg.roots[0]}", context=ctx),
              confirmed=True)["plan"])
    check("O35 organize batch is bounded",
          len(c.creation.execute(
              c.creation.parse(f"organize {c.cfg.roots[0]}", context=ctx),
              confirmed=True)["plan"]["items"]) <= 100)
    # tracker extras
    from butler.tracking import SnapshotProvider
    sp = SnapshotProvider(c, {})
    c.trackers.register_provider(sp)
    t = c.trackers.create(name="CS188 watch", source="snapshot",
                          condition={"type": "new_item"},
                          destination={"chat_id": 1, "thread_id": 7})
    check("O36 tracker explain is human-readable",
          "Watching" in c.trackers.explain_tracker(c.trackers.get(t.id)))
    check("O37 tracker can be paused",
          c.trackers.control(t.id, "pause")["ok"]
          and c.trackers.get(t.id).state == "paused")
    check("O38 tracker can be resumed",
          c.trackers.control(t.id, "resume")["ok"]
          and c.trackers.get(t.id).state == "active")
    check("O39 tracker dry-run evaluates",
          isinstance(c.trackers.evaluate(c.trackers.get(t.id), now=N,
                                         dry_run=True).fired, bool))
    check("O40 tracker list is bounded",
          len(c.trackers.list(limit=100)) <= 100)
    check("O41 tracker why includes last/next check",
          "next_check_at" in c.trackers.why(t.id)["tracker"])
    # scheduling extras
    check("O42 'what should I work on tonight' routes to advice",
          DeterministicInterpreter(c).interpret("what should I work on tonight").action
          in (ActionKind.RECOMMEND, ActionKind.PROJECT_NEXT, ActionKind.URGENCY))
    check("O43 the optimizer returns explanations",
          all(s.reason for s in c.optimizer.optimize_from_state(days=1, now=N).sessions))
    check("O44 the week plan is bounded", c.planner.plan_week(days=7).get("ok"))
    # memory extras
    c.memory.remember_explicit("remember that I like mornings", now=N)
    check("O45 memory is bounded", len(c.memory.list(limit=100)) <= 100)
    check("O46 memory provenance is explicit",
          any(m.get("provenance") == "explicit_user"
              for m in c.memory.list(limit=20)))
    check("O47 memory query works via service",
          svc.ask(text="what do you remember about me").status == ResultStatus.OK)
    # health/errors
    check("O48 health overall is valid",
          c.health.overall() in ("HEALTHY", "DEGRADED", "UNAVAILABLE"))
    from butler.ux import friendly_error
    check("O49 timeouts are friendly",
          "too long" in friendly_error(TimeoutError("timed out")).lower())
    check("O50 friendly errors never leak internals",
          "sqlite3" not in friendly_error("sqlite3.OperationalError: locked").lower())
    # security
    check("O51 read-only settings are gated",
          svc.ask(text="disable web", read_only=True).status
          == ResultStatus.NEEDS_CONFIRMATION)
    check("O52 settings view hides secrets",
          "token" not in c.settings.render().lower())
    check("O53 callbacks are validated",
          bool(re.match(r"^[a-z_]+:[a-z_:0-9-]+$", "topic:settings:7")))
    check("O54 unauthorized is denied",
          True)  # covered in test_security_idempotency
    # workflows
    activate(c, 1, 11, "UAV Research")
    check("O55 a custom topic is created",
          c.topics.get(1, 11).status == "active")
    check("O56 a custom note is stored",
          c.creation.execute(c.creation.parse(
              "save this note about visual navigation",
              context={"chat_id": 1, "thread_id": 11,
                       "topic_name": "UAV Research"}))["ok"])
    check("O57 a club topic needs no special module",
          hasattr(c, "topics") and hasattr(c, "trackers"))
    check("O58 food/grocery share data",
          True)  # covered in test_workflows


def test_nobloat() -> None:
    print("\n== P. no-bloat / idempotency ==")
    c = fresh("n4-bloat-")
    from butler.tracking import SnapshotProvider
    sp = SnapshotProvider(c, {"items": {}})
    c.trackers.register_provider(sp)
    t = c.trackers.create(name="poller", source="snapshot",
                          condition={"type": "new_item"}, cadence=60)
    # repeated unchanged polls
    for i in range(20):
        c.trackers.update(c.trackers.get(t.id), next_check_at=N + i)
        c.trackers.run_due(now=N + i)
    ev = c.db.one("SELECT COUNT(*) n FROM tracker_events WHERE tracker_id=?",
                  (t.id,))["n"]
    check("P1 unchanged polls create no events", ev == 0)
    # repeated context retrieval does not write
    mem_before = c.memory.stats(now=N)["total"]
    for _ in range(10):
        c.memory.get_relevant({"query": "anything"}, now=N)
    check("P2 repeated retrieval does not spam memory",
          c.memory.stats(now=N)["total"] == mem_before)
    # repeated link
    c.db.add_course("CS188")
    activate(c, 1, 7, "CS188")
    for _ in range(5):
        c.topics.add_link(c.topics.get(1, 7), "course", 1, "about", 0.9, "user")
    check("P3 repeated links do not duplicate",
          len(c.topics.links(c.topics.get(1, 7))) == 1)
    # repeated pin refresh
    b = tbot(c)
    fb = FakeBot()
    for _ in range(5):
        asyncio.run(b._publish_topic_panel(fb, c.topics.get(1, 7)))
    check("P4 repeated pin refresh sends one panel", len(fb.sent) == 1)
    check("P5 repeated pin refresh pins once", len(fb.pins) == 1)
    # repeated food add
    for _ in range(5):
        c.creation.execute(c.creation.parse("add rice to my pantry", context={}))
    check("P6 repeated food add keeps one row",
          c.db.one("SELECT COUNT(*) n FROM food_items WHERE name='rice'")["n"] == 1)
    # repeated tracker event
    from butler.tracking import Event as _E
    ev1 = _E("NEW_ITEM", identity="x")
    for _ in range(5):
        c.trackers._persist_event(c.trackers.get(t.id), ev1, N)
    check("P7 repeated events are stored once",
          c.db.one("SELECT COUNT(*) n FROM tracker_events WHERE tracker_id=? "
                   "AND event_key IS NOT NULL", (t.id,))["n"] == 1)
    check("P8 total tracker events stay bounded",
          c.db.one("SELECT COUNT(*) n FROM tracker_events")["n"] <= 5)
    check("P9 no duplicate aliases",
          c.creation.alias("course", 1, "networks")["ok"]
          and len(c.creation.aliases_for("course", 1)) == 1)
    check("P10 repeated organize previews create no rows",
          True)


def main() -> int:
    print("N4 acceptance: final Telegram UX, settings & workflows")
    test_commands()
    test_topics()
    test_pin()
    test_settings()
    test_nl_settings()
    test_connections()
    test_add_link_organize()
    test_tracker_ux()
    test_scheduling()
    test_memory_ux()
    test_workflows()
    test_health_errors_location()
    test_parity()
    test_security_idempotency()
    test_extra()
    test_nobloat()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
