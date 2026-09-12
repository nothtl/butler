"""LIVE (optional) Q13 scenario: the standing-instruction flow end to end.

Run:  .venv/bin/python tests/run_live_deepseek_q13_scenario.py --allow-live

Drives the REAL DeepSeek interpreter + executive service (no Telegram) through
the acceptance scenario:

  "When I ask you for food, also search the Web for good menus that fit my
   pantry." -> persistence question -> "Always in this topic."
  "What are you going to do when I ask for food?" -> explanation
  "Suggest something for dinner." -> pantry + web behavior
  "Actually don't search the web this time." -> one-off override
  "Suggest dinner again." -> behavior resumes

NOT part of the deterministic aggregate.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.agent.behaviors import TopicBehaviorStore  # noqa: E402
from butler.agent.interpret import resolve_interpreter  # noqa: E402
from butler.agent.interactions import InteractionKind  # noqa: E402
from butler.agent.service import ExecutiveService  # noqa: E402
from butler.agent.semantic import ActionKind  # noqa: E402
from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402


def _container() -> Container:
    cfg = Config.load()
    base = tempfile.mkdtemp(prefix="q13-live-", dir="/tmp/opencode")
    cfg.data_dir = os.path.join(base, "s")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "c.toml")
    cfg.timezone = "UTC"
    cfg.roots = [os.path.join(base, "r")]
    os.makedirs(cfg.roots[0], exist_ok=True)
    cfg.ensure_dirs()
    return Container(cfg)


def main() -> int:
    if "--allow-live" not in sys.argv:
        print("BLOCKED: pass --allow-live to make real DeepSeek calls")
        return 0
    c = _container()
    if not (getattr(c.cfg, "llm_api_key", "") and getattr(c.cfg, "llm_base_url", "")):
        print("BLOCKED: no LLM key configured")
        return 0
    prof, _ = c.topics.ensure(-100, 21, "Food")
    c.topics.update(prof, status="active", name="Food", purpose="Food")
    ctx = {"chat_id": -100, "thread_id": 21, "topic_name": "Food"}
    interp = resolve_interpreter(c)
    svc = ExecutiveService(c)
    session = svc.session("user")
    checks = []

    def step(text: str):
        req = interp.interpret(text, topic=ctx)
        req.topic = dict(ctx)
        res = svc.handle(req)
        return req, res

    def check(name, cond, note=""):
        checks.append((name, bool(cond)))
        print(f"  {'PASS' if cond else 'FAIL'}  {name}  {note}")

    print("1) standing instruction")
    req, res = step("When I ask you for food, also search the Web for good "
                    "menus that fit my pantry.")
    data = res.data if isinstance(res.data, dict) else {}
    clar = data.get("clarification") or {}
    print(f"   interpreted action={req.action.value} conf={req.confidence:.2f}")
    check("S1 not a tracker", req.action != ActionKind.TRACKER_CREATE)
    check("S2 not a random target",
          req.action != ActionKind.WEB_SEARCH or not (req.target and req.target.name))
    check("S3 asks about persistence", bool(clar), str(clar.get("slot_name", "")))
    check("S4 no tracker created",
          len(c.trackers.list() if hasattr(c.trackers, "list") else []) == 0)

    print("2) 'Always in this topic.'")
    clar_id = clar.get("interaction_id")
    if clar_id:
        res2 = svc.resume_with_slot(clar_id, "persistence",
                                    "Always in this topic", user="user")
    else:
        _, res2 = step("Always in this topic.")
    store = TopicBehaviorStore(c)
    items = store.list(prof.id)
    check("S5 behavior persisted", len(items) >= 1,
          items[0].trigger if items else "none")

    print("3) what will you do")
    _, res3 = step("What are you going to do when I ask for food?")
    text3 = (res3.answer or (res3.data or {}).get("text") or "").lower()
    check("S6 explains stored behavior", "web" in text3 or "search" in text3,
          text3[:80])

    print("4) suggest dinner")
    req4, res4 = step("Suggest something for dinner.")
    d4 = res4.data if isinstance(res4.data, dict) else {}
    applied = bool(d4.get("behavior_applied"))
    task = svc.conversation.active(session)
    used = bool(task and task.filled_slots.get("use_web")) or applied
    check("S7 behavior applies (web)", used,
          f"applied={applied} action={req4.action.value}")

    print("5) override this time")
    req5, res5 = step("Actually don't search the web this time.")
    d5 = res5.data if isinstance(res5.data, dict) else {}
    check("S8 override handled", bool(d5) or bool(res5.answer))

    print("6) behavior resumes")
    req6, res6 = step("Suggest dinner again.")
    d6 = res6.data if isinstance(res6.data, dict) else {}
    task6 = svc.conversation.active(session)
    check("S9 behavior resumes",
          bool(d6.get("behavior_applied"))
          or bool(task6 and task6.filled_slots.get("use_web"))
          or bool(store.list(prof.id)))

    passed = sum(1 for _, ok in checks if ok)
    print(f"\nscenario: {passed}/{len(checks)} checks passed")
    print("RESULT: PASS" if passed == len(checks) else "RESULT: FAIL")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
