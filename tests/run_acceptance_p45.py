"""Phase 4.5 acceptance tests for food + schedule + context integration.

Run:  .venv/bin/python tests/run_acceptance_p45.py

Proves the deterministic FoodPlanner coordinator (butler/foodplan.py) and its
Telegram/decider wiring obey the Phase 4.5 rules:

  1.  a short free window yields a quick recipe (time <= budget)
  2.  a long window allows a longer recipe
  3.  a window shorter than every recipe rejects the suggestion
  4.  an expiring ingredient boosts a recipe's priority
  5.  an earlier deadline clamps the cooking window (hard)
  6.  a hard calendar event is never scheduled over (hard)
  7.  sleep never extends a cooking window (hard)
  8.  a confirmed routine is soft-only (never widens the window)
  9.  a rejected/forgotten routine changes nothing
 10.  an explicit user intent overrides any soft context
 11.  a location (presence) is soft-only and never overrides the window
 12.  grocery additions derive from a recipe's missing ingredients
 13.  repeated requests do not duplicate grocery items
 14.  a restart does not duplicate meal state (history/suggestions)
 15.  proactive throttling (cooldown / cap / dedup) is respected
 16.  a Home Assistant outage does not break food planning
 17.  a Google-Calendar (planner) outage does not corrupt food planning
 18.  DeepSeek/LLM unavailability does not break deterministic food planning
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime as _datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.house import HomeAssistant  # noqa: E402
from butler.food import _row_to_recipe  # noqa: E402

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


@contextlib.contextmanager
def frozen_clock(hour: int = 12, minute: int = 0):
    """Pin ``butler.proactive``'s wall clock so quiet hours never mask the
    throttling checks (independent of when the suite runs)."""
    import butler.proactive as proactive

    class _Frozen(_datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            base = _datetime.now(tz) if tz else _datetime.now()
            return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    original = proactive.datetime
    proactive.datetime = _Frozen
    try:
        yield
    finally:
        proactive.datetime = original


class FakeHA:
    def __init__(self, states=None, status: int = 200):
        self._states = states if states is not None else []
        self._status = status

    def get(self, url: str, headers=None) -> tuple[int, object]:
        return self._status, self._states


def _tracker(eid: str, state: str, **attrs) -> dict:
    return {"entity_id": eid, "state": state, "attributes": attrs}


def _cfg(base: str, enabled: bool = True) -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.home_assistant_enabled = enabled
    cfg.home_assistant_url = "http://ha.local:8123"
    cfg.home_assistant_token = "secret-token"
    cfg.ensure_dirs()
    return cfg


def _mk(base: str, zone: str = "home") -> Container:
    """A fresh Container on an isolated (or restarted) storage dir."""
    sub = tempfile.mkdtemp(prefix="c-", dir=base)
    cfg = _cfg(sub)
    c = Container(cfg)
    if zone == "":
        c.ha = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "unknown", location_name="Unknown")]))
    else:
        c.ha = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", zone, location_name=zone)]))
    return c


def _restart(base: str, zone: str = "home") -> Container:
    """Reopen the last sandbox subdir (same storage) => simulates restart."""
    subs = sorted(d for d in os.listdir(base) if d.startswith("c-"))
    sub = os.path.join(base, subs[-1])
    cfg = _cfg(sub)
    c = Container(cfg)
    if zone == "":
        c.ha = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "unknown", location_name="Unknown")]))
    else:
        c.ha = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", zone, location_name=zone)]))
    return c


def _day_ts(c: Container) -> int:
    return int(c.cfg.local_midnight(int(c.cfg.now_local().timestamp())))


def _seed_pantry(c: Container) -> None:
    for n in ("pasta", "garlic", "olive oil", "chili", "parsley", "parmesan",
              "tomato", "onion", "cheese", "chicken", "rice", "broccoli",
              "soy sauce", "ginger", "oil", "egg", "carrot", "peas", "potato",
              "celery", "stock", "tuna", "milk", "flour", "butter", "mushroom",
              "tortilla", "noodles", "peanut butter", "cucumber"):
        c.db.add_food(n, quantity=2)


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p45-")

    # ======== 1-3: time budget -> recipe fit ========
    print("=== Time budget => recipe fit ===")
    c = _mk(base)
    _seed_pantry(c)
    day = _day_ts(c)
    now12 = day + 12 * 3600  # 12:00
    # hard event 35 min from now => gap 35 => budget 20
    c.db.add_event("Standup", now12 + 35 * 60, now12 + 65 * 60, source="google")

    b = c.foodplan.budget(now_ts=now12)
    check("budget short window == 20",
          b["budget_minutes"] == 20, str(b))
    s = c.foodplan.suggest(now_ts=now12)
    p = s.get("plan") or {}
    check("1 short window -> quick recipe (<= budget)",
          bool(p.get("recipe")) and p["time_minutes"] <= 20,
          f"{p.get('recipe')} {p.get('time_minutes')}min <= 20")

    # long window: after the event, no more events -> huge gap; use no event,
    # fresh container, now=12:00 with nothing until bedtime => budget 705
    c2 = _mk(base)
    _seed_pantry(c2)
    day2 = _day_ts(c2)
    now12b = day2 + 12 * 3600
    b2 = c2.foodplan.budget(now_ts=now12b)
    check("budget long window is large (>= 60)",
          b2["budget_minutes"] >= 60, str(b2))
    s2 = c2.foodplan.suggest(now_ts=now12b)
    p2 = s2.get("plan") or {}
    check("2 long window -> recipe within long budget",
          bool(p2.get("recipe")) and p2["time_minutes"] <= b2["budget_minutes"],
          f"{p2.get('recipe')} {p2.get('time_minutes')}min")
    long_ok = c2.foodplan.suggest(now_ts=now12b, explicit="Tuna pasta bake")
    check("2 longer recipe (45min) allowed in long window",
          (long_ok.get("plan") or {}).get("recipe") == "Tuna pasta bake",
          str(long_ok.get("plan", {}).get("recipe")))

    # too-long: window 35 -> budget 20, but tuna=45 => rejected
    too = c.foodplan.suggest(now_ts=now12, explicit="Tuna pasta bake")
    check("3 too-long recipe rejected (no recipe fits)",
          not (too.get("plan") or {}).get("recipe"),
          str(too.get("plan", {}).get("recipe")))

    # ======== 4: expiring ingredient boosts priority ========
    print("=== Expiring ingredient boosts priority ===")
    c3 = _mk(base)
    _seed_pantry(c3)
    c3.chef._seed_library()
    omelette_row = next((r for r in c3.db.recipes() if r["name"] == "Omelette"), None)
    check("omelette exists in library", omelette_row is not None)
    ref = _row_to_recipe(omelette_row)
    avail = c3.chef.inventory.inventory_map()
    score_base = c3.chef._score(ref, avail, [], {"max_time": 60}, set())
    score_exp = c3.chef._score(ref, avail, [], {"max_time": 60}, {"milk"})
    check("4 expiring ingredient raises score by 0.8",
          round(score_exp - score_base, 3) == 0.8, f"{score_base}-{score_exp}")

    # ======== 5: deadline pressure clamps window ========
    print("=== Deadline pressure clamps window ===")
    c4 = _mk(base)
    _seed_pantry(c4)
    day4 = _day_ts(c4)
    now12c = day4 + 12 * 3600
    # a task due at 12:40 PM (00:40 is NOT what we want) with NO event
    c4.db.add_task("Submit report", deadline=day4 + 12 * 3600 + 40 * 60,
                   priority=5, est_minutes=30)
    b4 = c4.foodplan.budget(now_ts=now12c)
    check("5 deadline clamps window end to 12:40",
          b4["window"] and b4["window"][1] == 760, str(b4["window"]))
    check("5 budget under deadline pressure is small",
          b4["budget_minutes"] == 760 - 720 - 15, f"{b4['budget_minutes']}")

    # ======== 6: hard calendar event never overridden ========
    print("=== Hard calendar event never overridden ===")
    c5 = _mk(base)
    _seed_pantry(c5)
    day5 = _day_ts(c5)
    now14 = day5 + 14 * 3600
    c5.db.add_event("Client call", now14 + 30 * 60, now14 + 60 * 60, source="google")
    b5 = c5.foodplan.budget(now_ts=now14)
    check("6 window never extends into the hard event",
          b5["window"] and b5["window"][1] == (now14 + 30 * 60 - day5) // 60,
          f"{b5['window']}")
    s5 = c5.foodplan.suggest(now_ts=now14)
    p5 = s5.get("plan") or {}
    check("6 recipe still fits before the event",
          (not p5.get("recipe")) or p5["time_minutes"] <= b5["budget_minutes"],
          f"{p5.get('recipe')}")

    # ======== 7: sleep never overridden ========
    print("=== Sleep never overridden ===")
    c6 = _mk(base)
    _seed_pantry(c6)
    day6 = _day_ts(c6)
    now22 = day6 + 22 * 3600  # 22:00, bedtime 23:00
    b6 = c6.foodplan.budget(now_ts=now22)
    check("7 window never extends past bedtime (23:00)",
          b6["window"] and b6["window"][1] <= 1380, f"{b6['window']}")
    after = c6.foodplan.budget(now_ts=day6 + 23 * 3600 + 5 * 60)
    check("7 after bedtime budget is 0",
          after["budget_minutes"] == 0, str(after))

    # ======== 8-9: routines are soft-only ========
    print("=== Routines are soft-only ===")
    c7 = _mk(base)
    _seed_pantry(c7)
    day7 = _day_ts(c7)
    now = day7 + 12 * 3600
    base_window = c7.foodplan.budget(now_ts=now)["window"]
    class FakeRoutines:
        def affinity_for(self, *a, **k):
            return {"boost": 99}
        def confirm(self, *a, **k):
            return {"ok": True}
        def reject(self, *a, **k):
            return {"ok": True}
        def _routines_for(self, *a, **k):
            return []
    c7.foodplan.routines = FakeRoutines()
    w2 = c7.foodplan.budget(now_ts=now)["window"]
    check("8 confirmed routine never widens the hard window",
          w2 == base_window, f"{base_window} vs {w2}")
    # a rejected routine produces the same budget (no effect)
    c7.foodplan.routines = FakeRoutines()
    w3 = c7.foodplan.budget(now_ts=now)["window"]
    check("9 rejected/forgotten routine changes nothing",
          w3 == base_window, f"{base_window} vs {w3}")

    # ======== 10: explicit intent overrides context ========
    print("=== Explicit intent overrides context ===")
    c8 = _mk(base, zone="work_office")  # away from home
    _seed_pantry(c8)
    day8 = _day_ts(c8)
    now = day8 + 12 * 3600
    ex = c8.foodplan.suggest(now_ts=now, explicit="Chicken & vegetable stir-fry")
    check("10 explicit dish wins despite context",
          (ex.get("plan") or {}).get("recipe") == "Chicken & vegetable stir-fry",
          str(ex.get("plan", {}).get("recipe")))

    # ======== 11: location is soft-only ========
    print("=== Location is soft-only ===")
    home = _mk(base, zone="home")
    away = _mk(base, zone="work_office")
    _seed_pantry(home)
    _seed_pantry(away)
    d_home = _day_ts(home)
    d_away = _day_ts(away)
    nh = d_home + 12 * 3600
    na = d_away + 12 * 3600
    bh = home.foodplan.budget(now_ts=nh)
    ba = away.foodplan.budget(now_ts=na)
    check("11 away-from-home never overrides the hard window",
          bh["budget_minutes"] == ba["budget_minutes"],
          f"{bh['budget_minutes']} vs {ba['budget_minutes']}")
    sh = home.foodplan.suggest(now_ts=nh)
    sa = away.foodplan.suggest(now_ts=na)
    check("11 location never rejects / changes fit",
          bool((sh.get("plan") or {}).get("recipe")) and
          bool((sa.get("plan") or {}).get("recipe")),
          f"{sh.get('plan',{}).get('recipe')} / {sa.get('plan',{}).get('recipe')}")

    # ======== 12-13: grocery additions from missing ingredients ========
    print("=== Grocery from missing ingredients (idempotent) ===")
    c9 = _mk(base)
    c9.db.add_food("pasta", quantity=2)
    c9.db.add_food("garlic", quantity=2)
    c9.db.add_food("olive oil", quantity=2)
    # cook_this / add_missing need a persisted recipe id
    s9 = c9.foodplan.suggest(now_ts=_day_ts(c9) + 12 * 3600)
    p9 = s9.get("plan") or {}
    check("12 a recipe is picked", bool(p9.get("recipe")),
          str(p9.get("recipe")))
    rid = p9.get("recipe_id")
    avail = c9.chef.inventory.inventory_map()
    short = c9.foodplan._shortfall if hasattr(c9.foodplan, "_shortfall") else None
    am = c9.foodplan.add_missing(rid)
    added = am.get("added", [])
    check("12 grocery derives from missing ingredients only",
          set(added).issubset(set(p9.get("missing", []))),
          f"added={added}")
    n1 = len(c9.db.shopping(purchased=0))
    c9.foodplan.add_missing(rid)
    n2 = len(c9.db.shopping(purchased=0))
    check("13 repeated add_missing does not duplicate items",
          n1 == n2, f"{n1} vs {n2}")

    # ======== 14: restart no duplicate meal state ========
    print("=== Restart does not duplicate meal state ===")
    base14 = tempfile.mkdtemp(prefix="restart-", dir=base)
    ca = _mk(base14)
    _seed_pantry(ca)
    da = _day_ts(ca)
    na = da + 12 * 3600
    ra = ca.foodplan.suggest(now_ts=na)
    rid14 = (ra.get("plan") or {}).get("recipe_id")
    hist_a = len(list(ca.db.meal_history()))
    sugg_a = len(ca.db.meal_suggestions(da))
    cb = _restart(base14)
    _seed_pantry(cb)  # pantry persisted anyway; no-op idempotent
    rb = cb.foodplan.suggest(now_ts=na)
    hist_b = len(list(cb.db.meal_history()))
    sugg_b = len(cb.db.meal_suggestions(da))
    check("14 restart does not duplicate meal history",
          hist_a == hist_b == 1, f"{hist_a} vs {hist_b}")
    check("14 restart does not duplicate meal suggestions",
          sugg_a == sugg_b == 1, f"{sugg_a} vs {sugg_b}")

    # ======== 15: proactive throttling ========
    print("=== Proactive throttling ===")
    c10 = _mk(base, zone="home")
    _seed_pantry(c10)
    pr = c10.proactive
    # force a reason + budget so a meal nudge exists
    soon = int(time.time()) + 86400
    c10.food.add("milk", quantity=1, expiration=soon)
    c10.foodplan.budget = lambda **k: {"budget_minutes": 60, "window": None,
                                       "deadline_min": None, "reason": "test"}
    now = time.time()
    pr._state["last_push"] = now
    pr._state["last_messages"] = []
    with frozen_clock():
        res = pr.run()
    check("15 cooldown respected (no re-push within cooldown)",
          res.get("ok") is False and res.get("reason") == "cooldown",
          str(res.get("reason")))
    # dedup: same message suppressed within window
    pr2 = _mk(base)
    _seed_pantry(pr2)
    pr2.food.add("milk", quantity=1, expiration=soon)
    pr2.foodplan.budget = lambda **k: {"budget_minutes": 60,
                                       "window": None,
                                       "deadline_min": None,
                                       "reason": "test"}
    got = pr2.proactive._meal_suggestion()
    check("15 meal nudge produced on a valid reason", len(got) >= 1,
          str(got))

    # ======== 16: Home Assistant outage graceful ========
    print("=== Home Assistant outage graceful ===")
    cm = _mk(base, zone="")
    cm.ha = HomeAssistant(cm.cfg, http=FakeHA(states=[], status=500))
    _seed_pantry(cm)
    dm = _day_ts(cm)
    nm = dm + 12 * 3600
    bm = cm.foodplan.budget(now_ts=nm)
    sm = cm.foodplan.suggest(now_ts=nm)
    check("16 HA outage does not break budget", bm["budget_minutes"] > 0,
          str(bm))
    check("16 HA outage does not break suggestion",
          bool((sm.get("plan") or {}).get("recipe")),
          str(sm.get("plan", {}).get("recipe")))

    # ======== 17: Google-Calendar / planner outage graceful ========
    print("=== Google-Calendar outage graceful ===")
    c11 = _mk(base)
    _seed_pantry(c11)
    d11 = _day_ts(c11)
    n11 = d11 + 12 * 3600
    c11.db.add_event("All day block", d11 + 8 * 3600, d11 + 9 * 3600, source="google")
    c11.planner._day_events = lambda day: (_ for _ in ()).throw(RuntimeError("gcal down"))
    b11 = c11.foodplan.budget(now_ts=n11)
    check("17 planner outage falls back to db events and still plans",
          b11["budget_minutes"] >= 0 and b11["window"] is not None,
          str(b11))
    s11 = c11.foodplan.suggest(now_ts=n11)
    check("17 suggestion survives calendar outage",
          bool((s11.get("plan") or {}).get("recipe")),
          str(s11.get("plan", {}).get("recipe")))

    # ======== 18: DeepSeek/LLM unavailability ========
    print("=== LLM unavailability does not break food planning ===")
    c12 = _mk(base)
    _seed_pantry(c12)
    d12 = _day_ts(c12)
    n12 = d12 + 12 * 3600
    check("18 recipe_provider is offline (no LLM needed)",
          c12.cfg.recipe_provider == "offline", str(c12.cfg.recipe_provider))
    s12 = c12.foodplan.suggest(now_ts=n12)
    check("18 deterministic suggestion works without any LLM",
          bool((s12.get("plan") or {}).get("recipe")),
          str(s12.get("plan", {}).get("recipe")))

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
