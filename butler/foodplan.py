"""Phase 4.5: food + schedule + context integration.

A thin, deterministic coordinator that sits on top of the existing :mod:`Chef`
(recipes / ingredients), :mod:`Planner` (events / tasks / deadlines) and
:class:`ContextEngine` (presence / expiring food). It produces a *suggestion*
only — it never commits a schedule change and never purchases anything on its
own.

Design principles (mirrors the wider Phase 4 rules):

  * **Hard, never overridden** — a recipe is only recommended if it fits the
    contiguous free window that starts now, bounded by the next hard event,
    the user's deadline and sleep hours. Location / routines / preferences are
    SOFT and never widen this window.
  * **Deterministic first** — all scoring/ordering is pure + explainable; no
    LLM is involved in picking a recipe (DeepSeek absence never breaks this).
  * **Explicit intent wins** — a user naming a dish drives the recipe choice
    subject to the hard window.
  * **No duplicates** — meal suggestions, meal history and grocery additions
    are idempotent across repeated requests / restarts.
  * **Never silently purchase** — groceries are only added on an explicit
    "Add missing ingredients" confirmation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import schedule as sch
from .food import Chef, _row_to_recipe

# Meal-window folds (minutes-within-day) used only for a soft, explainable nudge.
_MEAL_WINDOWS = {
    "breakfast": (6 * 60, 10 * 60),
    "lunch": (11 * 60, 14 * 60),
    "dinner": (17 * 60, 21 * 60),
}


def _meal_window(now_min: int) -> str | None:
    for meal, (lo, hi) in _MEAL_WINDOWS.items():
        if lo <= now_min <= hi:
            return meal
    return None


class FoodPlanner:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.chef = getattr(container, "chef", None) or Chef(container)
        self.planner = getattr(container, "planner", None)
        self.context = getattr(container, "context", None)
        self.routines = getattr(container, "routines", None)

    # ------------------------------------------------------------- hard budget
    def budget(self, now_ts: int | None = None,
               day_ts: int | None = None) -> dict[str, Any]:
        """The contiguous free cooking window starting right now (hard bounds).

        Returns ``budget_minutes`` (0 => no time to cook) plus explainable
        ``reason`` and the raw ``window`` / ``deadline_min`` geometry. Never
        accounts for sleep, a hard event, or a deadline as slack — it is a
        strict upper bound on how much cooking time the user has right now.
        """
        wake_start = int(self.cfg.sleep_end)      # e.g. 07:00 -> 420
        wake_end = int(self.cfg.sleep_start)      # e.g. 23:00 -> 1380
        if now_ts is None:
            now_ts = int(self.cfg.now_local().timestamp())
        if day_ts is None:
            day_ts = self._local_midnight(now_ts)
        now_local = self._local_dt(now_ts)
        now_min = now_local.hour * 60 + now_local.minute

        events = self._day_events(day_ts)
        deadline_min = self._deadline_min(day_ts, now_min)

        if now_min >= wake_end:
            gap = None
            reason = "It's past bedtime — no cooking time left today."
        else:
            gaps = sch.free_intervals(max(now_min, wake_start), wake_end,
                                      int(self.cfg.sleep_start),
                                      int(self.cfg.sleep_end), events)
            gap = gaps[0] if gaps else None
            if gap is not None:
                g_start, g_end = gap
                if deadline_min is not None:
                    g_end = min(g_end, deadline_min)
                if g_end - g_start <= int(self.cfg.buffer_minutes):
                    gap = None
                else:
                    gap = (g_start, g_end)
            reason = self._budget_reason(now_min, gap, deadline_min)

        if gap is None:
            return {"budget_minutes": 0, "window": None, "deadline_min": None,
                    "reason": reason}

        g_start, g_end = gap
        budget = max(0, (g_end - g_start) - int(self.cfg.buffer_minutes))
        return {"budget_minutes": budget, "window": [g_start, g_end],
                "deadline_min": deadline_min, "reason": reason}

    def _budget_reason(self, now_min: int, gap: tuple[int, int] | None,
                       deadline_min: int | None) -> str:
        bits: list[str] = []
        if gap is None:
            return "no free window right now — the next commitment is too close."
        gs, ge = gap
        if deadline_min is not None and ge >= deadline_min:
            bits.append(f"a task is due at {_hm(deadline_min)}")
        else:
            bits.append(f"free until {_hm(ge)}")
        if gs > now_min:
            bits.append(f"(busy until {_hm(gs)})")
        sig = len(bits) and ", ".join(bits) or "free right now"
        return f"{sig}."

    # --------------------------------------------------------------- suggest
    def suggest(self, meal: str = "", preferences: list[str] | None = None,
                message: str = "", now_ts: int | None = None,
                day_ts: int | None = None, exclude_ids: set[int] | None = None,
                explicit: str | None = None,
                record: bool = True) -> dict[str, Any]:
        """Deterministically recommend a meal that fits the hard window.

        ``explicit`` (when the user named a dish) drives the candidate pool via
        recipe search; otherwise the full pool is ranked. Ranking uses the
        Chef's rich fit score (ingredients, time, difficulty, rating, favourite,
        prior usage, expiration pressure) augmented with soft context that
        never overrides the hard window:
          * expiring ingredients boost (already in Chef._score)
          * a confirmed cooking routine at this time gives a small nudge
          * an away-from-home presence gives a soft headwind (never removes)
        """
        if now_ts is None:
            now_ts = int(self.cfg.now_local().timestamp())
        if day_ts is None:
            day_ts = self._local_midnight(now_ts)

        b = self.budget(now_ts=now_ts, day_ts=day_ts)
        budget = b["budget_minutes"]
        window = b["window"]
        deadline = b["deadline_min"]

        available = self.chef.inventory.inventory_map()
        candidates = self._candidates(explicit or "", meal)
        exclude_ids = exclude_ids or set()
        if exclude_ids:
            candidates = [r for r in candidates if r.recipe_id not in exclude_ids]

        constraints = {"max_time": budget, "quick": budget <= 30}
        ranked = self.chef.rank(candidates, available=available,
                                preferences=preferences or [],
                                constraints=constraints)

        picked = None
        for cand in ranked:
            r = cand["recipe"]
            if r.time_minutes > budget:
                continue
            if exclude_ids and r.recipe_id in exclude_ids:
                continue
            picked = cand
            break

        if picked is None:
            empty_plan = {
                "meal": meal or "supper", "recipe": None, "recipe_id": None,
                "budget_minutes": budget, "window": window,
                "deadline_min": deadline, "reason": b["reason"],
                "message": "No recipe fits the available time and ingredients.",
                "missing": [],
            }
            return empty_plan

        r = picked["recipe"]
        if not r.recipe_id:
            if not record:
                return {"meal": meal or "supper", "plan": None,
                        "budget_minutes": budget, "window": window,
                        "deadline_min": deadline, "reason": b["reason"],
                        "recipe": r.name, "recipe_id": None,
                        "message": "recording disabled"}
            r.recipe_id = self.chef.import_recipe(r)
        if record:
            self._record_decision(r, meal or "supper", day_ts, budget, window)
        short = self.chef._shortfall(r, available)
        plan = {
            "meal": meal or "supper",
            "recipe": r.name,
            "recipe_id": r.recipe_id,
            "time_minutes": r.time_minutes,
            "difficulty": r.difficulty,
            "cost": r.cost,
            "score": round(picked["score"], 3),
            "have_ratio": round(picked["have"], 2),
            "missing": short,
            "steps": r.steps,
            "source": r.source,
            "source_url": r.source_url,
            "servings": r.servings,
            "time_estimated": bool(getattr(r, "time_estimated", False)),
            "cost_estimated": bool(getattr(r, "cost_estimated", False)),
            "recommended_why": self._explain(picked),
        }
        return {"meal": plan["meal"], "plan": plan, "budget_minutes": budget,
                "window": window, "deadline_min": deadline, "reason": b["reason"],
                "recipe": r.name, "recipe_id": r.recipe_id}

    def peek(self, now_ts: int | None = None,
             day_ts: int | None = None) -> dict[str, Any]:
        """Read-only suggestion used by the proactive nudger — never records a
        meal, imports a recipe, touches usage, or adds groceries."""
        return self.suggest(now_ts=now_ts, day_ts=day_ts, record=False)

    def _explain(self, picked: dict[str, Any]) -> str:
        r = picked["recipe"]
        have = picked["have"]
        bits = [f"{r.name} ~{r.time_minutes}min fits the free window"]
        if have >= 0.99:
            bits.append("all ingredients on hand")
        else:
            bits.append(f"have {int(have * 100)}% of ingredients")
        return ", ".join(bits) + "."

    def _candidates(self, explicit: str, meal: str) -> list[Any]:
        pool = self.chef._recipes()
        if not explicit:
            return pool
        q = explicit.strip().lower()
        matched = [r for r in pool if q in r.name.lower() or
                   any(q in (t or "").lower() for t in r.tags)]
        if matched:
            return matched
        if q in ("quick", "fast", "easy", "simple"):
            return [r for r in pool if r.time_minutes <= 30]
        return pool

    # ------------------------------------------------------------- follow-ups
    def cook_this(self, recipe_id: int, meal: str = "",
                  now_ts: int | None = None) -> dict[str, Any]:
        """Explicitly confirm cooking a packaged suggestion."""
        row = self.db.recipe_by_id(int(recipe_id))
        if not row:
            return {"ok": False, "error": "recipe not found"}
        if now_ts is None:
            now_ts = int(self.cfg.now_local().timestamp())
        day_ts = self._local_midnight(now_ts)
        rid = int(row["id"])
        self.chef.db.touch_usage(rid)
        self._record_history_once(rid, meal or "supper", day_ts)
        self._record_suggestion(rid, meal or "supper", day_ts, 0, "confirmed")
        return {"ok": True, "recipe_id": rid, "meal": meal or "supper"}

    def add_missing(self, recipe_id: int,
                    now_ts: int | None = None) -> dict[str, Any]:
        """Idempotently add a recipe's shortfall to the shopping list."""
        row = self.db.recipe_by_id(int(recipe_id))
        if not row:
            return {"ok": False, "error": "recipe not found"}
        r = _row_to_recipe(row)
        available = self.chef.inventory.inventory_map()
        short = self.chef._shortfall(r, available)
        added: list[str] = []
        for ing in short:
            already = self.db.one(
                "SELECT id FROM shopping_items WHERE name=? AND purchased=0 "
                "AND needed_for=?",
                (ing, r.name))
            if already:
                continue
            self.db.add_shopping(ing, quantity=1.0, unit="",
                                 category=self.chef._category_of(ing),
                                 needed_for=r.name)
            added.append(ing)
        return {"ok": True, "added": added,
                "shopping": [dict(x) for x in self.db.shopping(purchased=0)]}

    def not_tonight(self, recipe_id: int,
                    now_ts: int | None = None) -> dict[str, Any]:
        """Decline a suggestion; never schedule it and never add groceries."""
        if now_ts is None:
            now_ts = int(self.cfg.now_local().timestamp())
        day_ts = self._local_midnight(now_ts)
        self.db.add_meal_suggestion(day_ts, int(recipe_id), meal="declined",
                                    budget_minutes=0, reason="not tonight")
        return {"ok": True, "recipe_id": int(recipe_id)}

    # --------------------------------------------------------------- helpers
    def _record_decision(self, r: Any, meal: str, day_ts: int, budget: int,
                         window: list[int] | None) -> None:
        self.chef.db.touch_usage(r.recipe_id)
        self._record_history_once(r.recipe_id, meal, day_ts)
        reason = f"{_hm(window[0])}-{_hm(window[1])}" if window else ""
        self._record_suggestion(r.recipe_id, meal, day_ts, budget, reason)

    def _record_history_once(self, recipe_id: int, meal: str, day_ts: int) -> None:
        if self.db.meal_history_on_day(int(recipe_id), day_ts):
            return
        self.db.add_meal_history(int(recipe_id), meal or "supper",
                                 ts=int(self.cfg.now_local().timestamp()))

    def _record_suggestion(self, recipe_id: int, meal: str, day_ts: int,
                           budget: int, reason: str) -> int:
        return self.db.add_meal_suggestion(day_ts, int(recipe_id),
                                           meal=meal, budget_minutes=budget,
                                           reason=reason)

    def _day_events(self, day_ts: int) -> list[sch.Event]:
        planner = self.planner
        if planner is None or not hasattr(planner, "_day_events"):
            return self._events_from_db(day_ts)
        try:
            return planner._day_events(day_ts)
        except Exception:  # noqa: BLE001 — GCal/local outage never breaks planning
            return self._events_from_db(day_ts)

    def _events_from_db(self, day_ts: int) -> list[sch.Event]:
        out: list[sch.Event] = []
        try:
            start_ts = self._local_midnight(day_ts)
            end_ts = start_ts + 86400
            rows = self.db.events_between(start_ts, end_ts)
        except Exception:  # noqa: BLE001
            return out
        for r in rows:
            s = datetime.fromtimestamp(int(r["start_ts"]))
            e = datetime.fromtimestamp(int(r["end_ts"]))
            start_min = s.hour * 60 + s.minute if s.date() == datetime.fromtimestamp(start_ts).date() else 0
            end_min = e.hour * 60 + e.minute if e.date() == datetime.fromtimestamp(start_ts).date() else 1440
            if end_min <= start_min:
                continue
            out.append(sch.Event(id=int(r["id"]), title=str(r["title"]),
                                 start_min=start_min, end_min=end_min,
                                 source=str(r["source"] or "local")))
        return out

    def _deadline_min(self, day_ts: int, now_min: int) -> int | None:
        planner = self.planner
        if planner is None or not hasattr(planner, "_active_tasks"):
            return None
        try:
            tasks = planner._active_tasks(day_ts)
        except Exception:  # noqa: BLE001 — degrade gracefully
            return None
        deadlines = [int(t.deadline) for t in tasks
                     if getattr(t, "deadline", None) and int(t.deadline) > now_min]
        return min(deadlines) if deadlines else None

    def _local_midnight(self, ts: int) -> int:
        cfg = getattr(self, "cfg", None)
        if cfg is not None and hasattr(cfg, "local_midnight"):
            try:
                return int(cfg.local_midnight(ts))
            except Exception:  # pragma: no cover — tz invalid => fall back
                pass
        d = datetime.fromtimestamp(ts)
        return int(datetime(d.year, d.month, d.day).timestamp())

    def _local_dt(self, ts: int) -> datetime:
        cfg = getattr(self, "cfg", None)
        if cfg is not None and hasattr(cfg, "tz"):
            try:
                tz = cfg.tz()
            except Exception:  # pragma: no cover
                tz = None
            if tz is not None:
                return datetime.fromtimestamp(int(ts), tz)
        return datetime.fromtimestamp(int(ts))


def _hm(minutes: int) -> str:
    m = int(minutes)
    return f"{m // 60:02d}:{m % 60:02d}"
