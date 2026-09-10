"""Phase 7 acceptance tests: M0 audit + M1 agent core.

Run:  python tests/run_acceptance_p70.py

Covers the M1 deliverables:
  REGISTRY (1-8)   INTENT MODEL (9-12)   SESSION (13-17)
  RUNTIME READS (18-23)   GATING/CONFIRM (24-28)   IDEMPOTENCY (29-30)
  PROMPTS (31-33)   INTEGRATION (34-37)

TZ is pinned to UTC so all arithmetic is deterministic on any host.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

os.environ["TZ"] = "UTC"
try:
    time.tzset()  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    pass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.agent import (AgentReply, AgentRuntime, Intent, IntentParser,  # noqa: E402
                          Param, PendingAction, SessionStore, Tool,
                          ToolRegistry, ToolValidationError,
                          build_default_registry)
from butler.agent import prompts  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def section(title):
    print(f"\n=== {title} ===")


def _mk(base):
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.google_calendar_enabled = False
    cfg.timezone = "UTC"
    cfg.ensure_dirs()
    return Container(cfg)


# =================================================================
section("Tool registry & validation")
reg = ToolRegistry()
reg.add("echo", "echo a value", lambda a: a,
        [Param("text", "str", required=True),
         Param("times", "int", default=1),
         Param("mode", "str", default="a", choices=("a", "b"))],
        action="find")
check(1, reg.has("echo") and reg.get("echo").name == "echo", "register/get/has")
check(2, reg.names() == ["echo"], "names lists tools")
clean = reg.validate("echo", {"text": "hi"})
check(3, clean == {"text": "hi", "times": 1, "mode": "a"}, "defaults filled")
check(4, reg.validate("echo", {"text": "hi", "times": "3"})["times"] == 3,
      "int coercion from string")
try:
    reg.validate("echo", {"text": "hi", "bogus": 1})
    check(5, False, "unknown arg must be rejected")
except ToolValidationError:
    check(5, True, "unknown arg rejected")
try:
    reg.validate("echo", {})
    check(6, False, "missing required must be rejected")
except ToolValidationError:
    check(6, True, "missing required rejected")
try:
    reg.validate("echo", {"text": "hi", "mode": "z"})
    check(7, False, "bad choice must be rejected")
except ToolValidationError:
    check(7, True, "bad choice rejected")
try:
    reg.register(Tool(name="echo", description="dup", handler=lambda a: a))
    check(8, False, "duplicate tool must raise")
except ValueError:
    check(8, True, "duplicate tool raises")

# =================================================================
section("Intent model")
legacy = type("L", (), {"kind": "tasks", "target": "5", "query": "q",
                        "params": {"x": 1}, "raw": "/tasks", "scope": "today"})()
it = Intent.from_legacy(legacy)
check(9, it.kind == "tasks" and it.target == "5" and it.params == {"x": 1},
      "from_legacy maps fields")
check(10, it.source == "deterministic" and it.confirmed is False,
      "from_legacy sets source/confirmed")
it2 = Intent.from_dict(it.to_dict())
check(11, it2 == it, "Intent round-trips through dict")
rep = AgentReply(ok=True, data=[1, 2], intent=it)
check(12, rep.to_dict()["intent"]["kind"] == "tasks" and rep.to_dict()["ok"],
      "AgentReply serialises")

# =================================================================
section("Session state")
store = SessionStore()
s = store.get("u1")
s.add_turn("user", "hello")
s.add_turn("assistant", "hi")
check(13, len(s.history()) == 2, "turns recorded")
s.remember("k", 7)
check(14, s.recall("k") == 7 and s.recall("missing", "d") == "d", "remember/recall")
p1 = PendingAction(id="a", tool="x", args={}, created=1)
p2 = PendingAction(id="b", tool="y", args={}, created=2)
s.park(p1)
s.park(p2)
check(15, s.take().id == "b", "take returns most recent pending")
check(16, s.take("a").id == "a" and not s.pending, "take by id")
s.reset()
check(17, not s.turns and not s.data and not s.pending, "reset clears session")
check(18, store.get("u1") is s, "store returns same session instance")

# =================================================================
section("Runtime reads (real container)")
c = _mk(tempfile.mkdtemp(prefix="p70-"))
rt = c.agent
check(19, isinstance(rt, AgentRuntime), "container wires AgentRuntime")
check(20, len(rt.tools()) >= 20, f"registry has {len(rt.tools())} tools")
json.dumps(rt.tools())
check(21, True, "tool schema is JSON-serialisable")
r = rt.run_tool("tasks")
check(22, r.ok and isinstance(r.data, list), "tasks tool executes (read)")
r2 = rt.run_tool("context")
check(23, r2.ok and "tasks" in r2.data, "context tool returns snapshot")
r3 = rt.run_tool("week")
check(24, r3.ok and "days" in r3.data, "week tool executes with default days")
r4 = rt.run_tool("nope_missing")
check(25, not r4.ok and r4.results[0].decision == "denied", "unknown tool denied")
r5 = rt.run_tool("find", {"bogus": 1})
check(26, not r5.ok and r5.results[0].decision == "invalid", "invalid args rejected")

# =================================================================
section("Safety gate, confirmation & idempotency")
flag = {"n": 0}
rt.registry.add("test_confirm", "test external", lambda a: {"ran": True},
                [], action="organize", side_effect=True)
denied = rt.run_tool("test_confirm", user="g")
check(27, not denied.ok and denied.results[0].decision == "denied",
      "unconfirmed external denied")
check(28, bool(denied.results[0].meta.get("pending_id")),
      "denied external parks a pending action")
reply = rt.run("yes", user="g", confirm=True)
check(29, reply.ok and reply.results and reply.results[0].data == {"ran": True},
      "confirmation executes the parked action")

calls = {"n": 0}


def _write(a):
    calls["n"] += 1
    return {"n": calls["n"]}


rt.registry.add("test_write", "test local write", _write,
                [Param("x", "int", default=0)],
                action="note", side_effect=True)
w1 = rt.run_tool("test_write", {"x": 1}, user="g")
w2 = rt.run_tool("test_write", {"x": 1}, user="g")
check(30, w1.ok and not w1.results[0].replayed
      and w2.results[0].replayed and calls["n"] == 1,
      "side effect runs exactly once (idempotent replay)")

# =================================================================
section("Intent parsing & prompts")
parser = IntentParser(decider=c.decider, chat=c.chat, registry=rt.registry)
pi = parser.parse("/tasks")
check(31, pi.kind == "tasks", "deterministic parse preserves slash intent")
check(32, parser.needs_llm(Intent(kind="chat")) is True,
      "chat needs LLM fallback")
choice = prompts.parse_tool_choice('{"tool":"tasks","args":{},"reason":"r"}')
check(33, choice and choice["tool"] == "tasks", "tool-choice JSON parsed")
check(34, prompts.parse_tool_choice("not json") is None, "bad tool-choice -> None")
check(35, "tasks" in prompts.render_tools(rt.tools()), "render_tools lists names")

# =================================================================
section("Integration")
check(36, c.agent is not None and c.decider is not None,
      "agent and decider coexist")
check(37, all(rt.registry.has(t) for t in ("day", "week", "tasks", "gcal_sync",
                                           "course_check")),
      "core tools registered")

print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
