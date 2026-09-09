"""Phase 3: Food / Chef.

FoodInventory (fridge & cupboard) + Chef (recipe selection, meal planning,
grocery list). Design rules:

  * Inventory mutations / parsing are DETERMINISTIC FIRST: generic quantities
    ("3", "1.5"), units ("kg","g","ml","L","pcs"), and dates are parsed with a
    regex. DeepSeek is only invoked to interpret genuinely ambiguous free-text
    ("a couple of sticks of butter"), and only in ``parse``.
  * Recipe matching is a deterministic cross-reference over available
    ingredients + preferences + constraints (time, difficulty, equipment,
    expiration, budget). No LLM is used to "judge" which recipe is best — the
    scoring function is transparent and reproducible.
  * ``plan_meal`` takes a free-time budget (minutes on hand) so it never
    recommends a meal that doesn't fit the scheduler's free time. Callers
    (decider / proactive) compute the budget from the scheduler and pass it in.
  * The ``recipe_provider`` config picks the source: ``offline`` (built-in
    pantry recipes, works with no network) or ``web`` (an online recipe API).
    Web fetching degrades gracefully to ``offline`` on any failure.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("butler.food")

# ---------------------------------------------------------------------------
# unit normalisation
_UNITS = {
    "g", "kg", "gram", "grams", "gramm", "lb", "lbs", "pound", "oz", "ounce",
    "ml", "l", "ltr", "liter", "litre", "cl", "dl", "cup", "cups", "tbsp",
    "tsp", "pinch", "piece", "pieces", "pcs", "pc", "slice", "slices", "pack",
    "packet", "bag", "bottle", "can", "tin", "box", "carton", "dozen", "half",
}


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _parse_qty(text: str) -> tuple[float, str]:
    """Pull "<number> <unit>" off the front of a food description.

    A trailing word is only treated as a unit when it is a recognised unit;
    otherwise it belongs to the food name (e.g. "3 eggs" -> "eggs").
    """
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?/(?:[0-9]+)?|[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)\s*([a-z]+)?\b",
                 text, re.I)
    if not m:
        return 1.0, ""
    num = m.group(1)
    word = (m.group(2) or "").lower()
    try:
        if "/" in num:
            a, b = num.split("/")
            qty = float(a) / float(b) if b else float(a)
        else:
            qty = float(num)
    except ValueError:
        qty = 1.0
    unit = word if word in _UNITS else ""
    return qty, unit


def _clean_name(text: str, qty: float, unit: str) -> str:
    """Strip the leading quantity (+ unit) and junk prepositions from a name."""
    t = re.sub(r"^\s*[0-9]+(?:\.[0-9]+)?/?[0-9]*\s*", "", text)
    if unit:
        t = re.sub(rf"^{re.escape(unit)}\s*", "", t, flags=re.I)
    t = t.strip()
    t = re.sub(r"^(of|the|an?|a)\s+", "", t, flags=re.I).strip()
    t = re.sub(r"^(carton|bottle|jar|bag|box|pack|tray|can|tin|loaf|bunch|dozen)\s+of\s+",
               "", t, flags=re.I).strip()
    return t.lower()


def _parse_date(text: str, now: int) -> int:
    """Resolve a human date or N d/w/m phrase to a unix timestamp (or 0)."""
    t = text.strip().lower()
    if not t:
        return 0
    m = re.match(r"^(\d+)\s*(d|day|days|w|week|weeks|m|month|months)", t)
    if m:
        n = int(m.group(1))
        mult = {"d": 86400, "day": 86400, "days": 86400,
                "w": 604800, "week": 604800, "weeks": 604800,
                "m": 2592000, "month": 2592000, "months": 2592000}[m.group(2)]
        return now + n * mult
    return _absolute_date(t)


def _absolute_date(t: str) -> int:
    try:
        return int(datetime.strptime(t, "%Y-%m-%d").timestamp())
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# built-in recipe knowledge base (offline provider) — good, cheap and easy.
@dataclass
class Recipe:
    name: str
    ingredients: list[str]
    time_minutes: int
    difficulty: int          # 1 easy .. 5 hard
    equipment: list[str] = field(default_factory=list)
    cost: float = 2.0        # rough per-serving cost in $ (cheap = low)
    tags: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    source: str = "builtin"  # builtin | themealdb | ...
    source_url: str = ""
    recipe_id: int = 0       # Recipe Library row id (0 = not persisted)
    servings: int = 2
    rating: float = 0.0      # user rating 0..5
    favorite: bool = False
    times_used: int = 0
    last_used: int = 0       # unix ts of last time it was planned
    prep_minutes: int = 0
    cook_minutes: int = 0
    time_estimated: bool = False   # True => cook time is an estimate, unverified
    cost_estimated: bool = False   # True => cost is an estimate, unverified
    nutrition_source: str = ""     # '' | 'author' | 'verified' | 'missing'


def _recipe_to_dict(r: Any) -> dict[str, Any]:
    """Serialize a Recipe (or a DB row) into the API dictionary shape."""
    d = dict(r) if not isinstance(r, Recipe) else {
        "name": r.name, "ingredients": list(r.ingredients),
        "time_minutes": r.time_minutes, "difficulty": r.difficulty,
        "equipment": list(r.equipment), "cost": r.cost, "tags": list(r.tags),
        "steps": list(r.steps), "source": r.source, "source_url": r.source_url,
        "recipe_id": r.recipe_id, "servings": r.servings, "rating": r.rating,
        "favorite": r.favorite, "times_used": r.times_used, "last_used": r.last_used,
        "prep_minutes": r.prep_minutes, "cook_minutes": r.cook_minutes,
        "time_estimated": bool(getattr(r, "time_estimated", False)),
        "cost_estimated": bool(getattr(r, "cost_estimated", False)),
        "nutrition_source": getattr(r, "nutrition_source", "") or "",
    }
    # normalise stored JSON columns
    for k in ("ingredients", "steps", "tags", "equipment"):
        if isinstance(d.get(k), str):
            import json as _json
            try:
                d[k] = _json.loads(d[k])
            except Exception:  # noqa: BLE001
                d[k] = []
    return d


def _row_to_recipe(row: Any) -> Recipe:
    import json as _json
    row = dict(row)
    def j(v: str | None) -> list[str]:
        try:
            return _json.loads(v) if v else []
        except Exception:  # noqa: BLE001
            return []
    prep = int(row.get("prep_minutes", 0) or 0)
    cook = int(row.get("cook_minutes", 0) or 0)
    total = (prep + cook) or int(row.get("time_minutes", 0) or 20)
    return Recipe(
        str(row["name"]), j(row["ingredients"]), total,
        int(row.get("difficulty", 2) or 2),
        j(row["equipment"]), float(row.get("cost", 2.0) or 2.0), j(row["tags"]),
        j(row["steps"]), str(row.get("source", "builtin") or "builtin"),
        str(row.get("source_url", "") or ""), int(row["id"] or 0),
        int(row.get("servings", 2) or 2), float(row.get("rating", 0.0) or 0.0),
        bool(row.get("favorite", 0)), int(row.get("times_used", 0) or 0),
        int(row.get("last_used", 0) or 0),
        prep, cook,
        bool(row.get("time_estimated", 0)),
        bool(row.get("cost_estimated", 0)),
        str(row.get("nutrition_source", "") or ""),
    )


BUILTIN_RECIPES: list[Recipe] = [
    Recipe("Pasta with garlic & olive oil",
           ["pasta", "garlic", "olive oil", "chili", "parsley", "parmesan"],
           20, 1, ["pot", "pan"], 1.2, ["italian", "quick", "vegetarian"],
           ["Boil pasta. Saute garlic & chili in olive oil. Toss, top with parmesan."]),
    Recipe("Tomato pasta",
           ["pasta", "tomato", "onion", "garlic", "olive oil", "cheese"],
           30, 1, ["pot", "pan"], 1.5, ["italian", "vegetarian", "pantry"],
           ["Cook pasta. Saute onion & garlic, add tomato, simmer, toss pasta."]),
    Recipe("Chicken & vegetable stir-fry",
           ["chicken", "rice", "broccoli", "soy sauce", "garlic", "ginger", "oil"],
           25, 2, ["pan", "rice cooker"], 2.5, ["asian", "healthy"],
           ["Stir-fry chicken & veg over high heat with soy, garlic, ginger. Serve on rice."]),
    Recipe("Fried rice",
           ["rice", "egg", "soy sauce", "carrot", "peas", "onion", "oil"],
           15, 1, ["pan", "rice cooker"], 1.3, ["asian", "quick"],
           ["Fry onion, add rice, soy, egg, veg. Toss until hot."]),
    Recipe("Vegetable soup",
           ["onion", "carrot", "tomato", "potato", "celery", "stock", "oil"],
           40, 1, ["pot"], 1.8, ["soup", "healthy"],
           ["Saute veg, cover with stock, simmer 30 min, season."]),
    Recipe("Potato hash",
           ["potato", "egg", "onion", "oil"],
           25, 1, ["pan"], 1.0, ["breakfast", "cheap"],
           ["Sear potatoes, add onion & egg, fry until golden."]),
    Recipe("Tuna pasta bake",
           ["pasta", "tuna", "milk", "cheese", "onion", "flour", "butter"],
           45, 2, ["pot", "oven"], 2.0, ["bake"],
           ["Cook pasta, make white sauce with flour/butter/milk, mix tuna & cheese, bake."]),
    Recipe("Omelette",
           ["egg", "cheese", "milk", "butter", "onion", "mushroom"],
           10, 1, ["pan"], 1.1, ["breakfast", "quick"],
           ["Whisk eggs & milk, cook in butter, fold in fillings."]),
    Recipe("Quesadilla",
           ["tortilla", "cheese", "chicken", "tomato", "onion"],
           15, 1, ["pan"], 2.2, ["mexican", "quick"],
           ["Fill tortilla, toast until cheese melts."]),
    Recipe("Peanut noodles",
           ["noodles", "peanut butter", "soy sauce", "garlic", "carrot", "cucumber"],
           15, 1, ["pot"], 1.6, ["asian", "quick"],
           ["Cook noodles, whisk peanut/soy/garlic sauce, toss with veg."]),
]


# ---------------------------------------------------------------------------
class FoodInventory:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db

    # ------------------------------------------------------------ registry
    def add(self, name: str, quantity: float = 1.0, unit: str = "",
            expiration: int = 0, opened: int = 0, category: str = "",
            storage: str = "", notes: str = "") -> dict[str, Any]:
        name = _norm_name(name)
        if not name:
            return {"ok": False, "error": "name required"}
        fid = self.db.add_food(name, quantity=quantity, unit=unit,
                               expiration_date=expiration, opened_date=opened,
                               category=category, storage_location=storage,
                               notes=notes)
        return {"ok": True, "food_id": fid, "name": name, "quantity": quantity}

    def consume(self, name: str, quantity: float | None = None) -> dict[str, Any]:
        row = self.db.find_food(name)
        if not row:
            return {"ok": False, "error": f"no food '{name}'"}
        qty = row["quantity"]
        if quantity is None:
            qty = 0.0
        else:
            qty = float(row["quantity"]) - quantity
        if qty <= 0:
            self.db.delete_food(int(row["id"]))
            return {"ok": True, "name": row["name"], "removed": True}
        self.db.update_food(int(row["id"]), quantity=qty)
        return {"ok": True, "name": row["name"], "quantity": qty, "removed": False}

    def remove(self, name: str) -> dict[str, Any]:
        row = self.db.find_food(name)
        if not row:
            return {"ok": False, "error": f"no food '{name}'"}
        self.db.delete_food(int(row["id"]))
        return {"ok": True, "name": row["name"], "removed": True}

    def search(self, name: str) -> list[dict[str, Any]]:
        name = name.strip().lower()
        return [dict(r) for r in self.db.food() if name in r["name"]]

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.food()]

    def get(self, name: str) -> dict[str, Any] | None:
        row = self.db.find_food(name)
        return dict(row) if row else None

    def inventory_map(self) -> dict[str, float]:
        return {r["name"]: r["quantity"] for r in self.db.food()}

    # ---------------------------------------------------------- expiration
    def expiring(self, within_days: int = 3) -> list[dict[str, Any]]:
        now = int(datetime.now().timestamp())
        horizon = now + within_days * 86400
        out = []
        for r in self.db.food():
            exp = int(r["expiration_date"] or 0)
            if exp and exp <= horizon:
                d = dict(r)
                d["days_left"] = int((exp - now) / 86400)
                d["expired"] = exp < now
                out.append(d)
        out.sort(key=lambda r: r["expiration_date"])
        return out

    # ------------------------------------------------------------ NL parse
    def parse(self, text: str) -> dict[str, Any]:
        """Deterministic first; DeepSeek only for ambiguous free-text.

        Returns {'added': [...]} describing items to be stored. Caller (decider)
        commits them via :meth:`add`.
        """
        items: list[dict[str, Any]] = []
        parts: list[str] = []
        for part in re.split(r"[;,.\n]", text):
            part = part.strip(" \t,.")
            if not part:
                continue
            # Split a quantified "3 eggs and 1 carton of milk" clause into items,
            # but keep unquantified phrases ("salt and pepper") together.
            if re.search(r"\b[0-9]+\b.*\band\b.*\b[0-9]+\b", part):
                parts += [p.strip() for p in re.split(r"\s+and\s+", part) if p.strip()]
            else:
                parts.append(part)
        for part in parts:
            qty, unit = _parse_qty(part)
            name = _clean_name(part, qty, unit)
            if len(name) < 2:
                continue
            items.append({"name": name, "quantity": qty, "unit": unit})
        if not items:
            # fall back to DeepSeek for a messy, non-quantified description
            parsed = self._ai_parse(text)
            if parsed:
                items = parsed
        return {"added": items, "interpreted": bool(items)}

    def _ai_parse(self, text: str) -> list[dict[str, Any]]:
        chat = getattr(self.container, "chat", None)
        if chat is None or not getattr(chat, "_llm_ready", lambda: False)():
            return []
        try:
            raw = chat._llm((
                "Convert this shopping/fridge note into a JSON array of "
                "{name, quantity, unit, expiration}. Only real food items, "
                "no prose, no markdown. Use quality measures for vague words "
                "(e.g. 'some', 'a bunch' -> quantity 1).",
                text,
            ))
            import json as _json
            m = re.search(r"\[.*\]", raw or "", re.S)
            if not m:
                return []
            data = _json.loads(m.group(0))
            return [{"name": _norm_name(it.get("name", "")), "quantity": float(it.get("quantity", 1)),
                     "unit": it.get("unit", "")} for it in data if it.get("name")]
        except Exception:
            return []


def _stem(w: str) -> str:
    """Singularize to make plural forms match (egg <-> eggs, peas <-> pea)."""
    w = w.lower()
    for suf in ("es", "s"):
        if w.endswith(suf) and len(w) > len(suf) + 1:
            return w[:-len(suf)]
    return w


def _match_ingredient(ing: str, available: dict[str, float]) -> bool:
    """True if recipe ingredient ``ing`` is (reasonably) on hand."""
    ist = _stem(ing)
    stems = {_stem(n) for n in available}
    if ist in stems:
        return True
    for name in available:
        if re.match(rf"\b{re.escape(ing)}\b", _stem(name), flags=re.I):
            return True
    return False


def _have_ratio(r: Recipe, available: dict[str, float]) -> float:
    have = sum(1 for ing in r.ingredients if _match_ingredient(ing, available))
    return have / max(1, len(r.ingredients))


# ---------------------------------------------------------------------------
class Chef:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.inventory = FoodInventory(container)

    def provider(self) -> str:
        return (self.cfg.recipe_provider or "offline").lower()

    # -------------------------------------------------------------- scoring
    def recipes(self, available: dict[str, float] | None = None,
                preferences: list[str] | None = None,
                constraints: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Rank the full recipe pool by fit (ingredients, time, difficulty,
        cost, tags, ratings, favourites, prior usage, expiration pressure)."""
        available = available if available is not None else self.inventory.inventory_map()
        return self.rank(self._recipes(), available, preferences, constraints)

    def rank(self, candidates: list[Recipe], available: dict[str, float] | None = None,
             preferences: list[str] | None = None,
             constraints: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Score + order an arbitrary candidate set (used by both the full-pool
        ranker and the narrowed recipe-search result set)."""
        available = available if available is not None else self.inventory.inventory_map()
        preferences = preferences or []
        constraints = constraints or {}
        expire = self._expiring_names()
        out: list[dict[str, Any]] = []
        for r in candidates:
            score = self._score(r, available, preferences, constraints, expire)
            out.append({"recipe": r, "score": score,
                        "have": _have_ratio(r, available)})
        out.sort(key=lambda x: (-x["score"], x["recipe"].difficulty,
                                x["recipe"].time_minutes))
        return out

    def _expiring_names(self, within_days: int = 3) -> set[str]:
        return {_stem(i.get("name", "")) for i in
                self.inventory.expiring(within_days) if i.get("name")} if self.inventory else set()

    def _seed_library(self) -> None:
        """Persist the built-in pantry recipes into the Recipe Library once."""
        for r in BUILTIN_RECIPES:
            try:
                self.db.add_recipe(r.name, source="builtin", source_url="",
                                   ingredients=r.ingredients, steps=r.steps,
                                   tags=r.tags, equipment=r.equipment,
                                   servings=2, cook_minutes=r.time_minutes,
                                   difficulty=r.difficulty, cost=r.cost,
                                   nutrition_source="author")
            except Exception as exc:  # noqa: BLE001
                log.debug("seed recipe %s skipped: %s", r.name, exc)

    def _library_recipes(self) -> list[Recipe]:
        return [_row_to_recipe(r) for r in self.db.recipes()]

    def _recipes(self) -> list[Recipe]:
        """Pool = Recipe Library (builtins seeded) + optional web browse."""
        self._seed_library()
        pool = self._library_recipes()
        prov = self.provider()
        if prov == "web":
            try:
                web = self._web_recipes()
                if web:
                    pool = self._dedupe(web + pool)
            except Exception as exc:  # noqa: BLE001
                log.warning("web recipe fetch failed, using library+builtin: %s", exc)
        return self._dedupe(pool + BUILTIN_RECIPES)

    @staticmethod
    def _dedupe(recipes: list[Recipe]) -> list[Recipe]:
        seen: set[tuple[str, str]] = set()
        out: list[Recipe] = []
        for r in recipes:
            key = (r.name.lower(), r.source)
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        return out

    def _web_recipes(self) -> list[Recipe]:
        return _fetch_web_recipes()

    # ------------------------------------------------------- recipe library
    def library(self, favorite_only: bool | None = None) -> list[dict[str, Any]]:
        self._seed_library()
        rows = self.db.recipes(favorite_only=int(favorite_only) if favorite_only is not None else None)
        return [_recipe_to_dict(_row_to_recipe(r)) for r in rows]

    def favorites(self) -> list[dict[str, Any]]:
        return self.library(favorite_only=True)

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        return [_recipe_to_dict(_row_to_recipe(r)) for r in self.db.recipes_recent(limit)]

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.meal_history(limit)]

    def import_recipe(self, r: Recipe) -> int:
        """Persist a recipe into the Recipe Library (deduped) and return its id."""
        self._seed_library()
        return self.db.add_recipe(
            r.name, source=r.source or "web", source_url=r.source_url,
            ingredients=r.ingredients, steps=r.steps, tags=r.tags,
            equipment=r.equipment, servings=r.servings,
            prep_minutes=r.prep_minutes, cook_minutes=r.time_minutes,
            difficulty=r.difficulty, cost=r.cost,
            time_estimated=int(r.time_estimated), cost_estimated=int(r.cost_estimated),
            nutrition_source=r.nutrition_source)

    def _pick(self, r: Recipe) -> dict[str, Any]:
        """Persist a chosen recipe and record usage + meal history."""
        if not r.recipe_id:
            r.recipe_id = self.import_recipe(r)
        self.db.touch_usage(r.recipe_id)
        return _recipe_to_dict(r)

    def rate(self, recipe_id: int, rating: float) -> dict[str, Any]:
        self.db.set_rating(int(recipe_id), max(0.0, min(5.0, float(rating))))
        row = self.db.recipe_by_id(int(recipe_id))
        return dict(row) if row else {}

    def favorite(self, recipe_id: int) -> dict[str, Any]:
        row = self.db.recipe_by_id(int(recipe_id))
        if not row:
            return {"ok": False, "error": "recipe not found"}
        self.db.set_favorite(int(recipe_id), not bool(row["favorite"]))
        row = self.db.recipe_by_id(int(recipe_id))
        return dict(row) if row else {}

    def set_favorite(self, recipe_id: int, fav: bool) -> dict[str, Any]:
        row = self.db.recipe_by_id(int(recipe_id))
        if not row:
            return {"ok": False, "error": "recipe not found"}
        self.db.set_favorite(int(recipe_id), bool(fav))
        return dict(self.db.recipe_by_id(int(recipe_id)))

    # ------------------------------------------------------- online search
    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Recipe Search: web (real recipes) + library + builtin, ranked.

        Web results are imported into the Recipe Library so they become usable
        / rateable / memorable. Fails gracefully to the library on any error.
        """
        out: list[Recipe] = []
        web: list[Recipe] = []
        if self.provider() == "web":
            try:
                web = _fetch_web_search(query)
            except Exception as exc:  # noqa: BLE001
                log.warning("recipe search failed: %s", exc)
        # local matches (library + builtin) by name/tag/ingredient
        for r in self._recipes():
            hay = " ".join(r.tags + r.ingredients).lower()
            if self._local_match(query, r.name.lower(), hay):
                out.append(r)
        if web:
            for w in web:
                w.recipe_id = self.import_recipe(w)
            out = self._dedupe(web + out)
        # "Search" returns the matching candidates; "rank" orders them by the
        # rich recipe-fit score (ingredients, time, difficulty, rating,
        # favourite, prior usage, expiration pressure, cost, preferences).
        ranked = self.rank(out, available=self.inventory.inventory_map())
        return [_recipe_to_dict(r) for r in (x["recipe"] for x in ranked)][:limit]

    @staticmethod
    def _local_match(query: str, name: str, hay: str) -> bool:
        q = query.strip().lower()
        if not q:
            return True
        words = [w for w in re.split(r"\W+", q) if w]
        if any(w in name for w in words):
            return True
        return all(w in hay for w in words)

    @staticmethod
    def _score(r: Recipe, available: dict[str, float],
               preferences: list[str], constraints: dict[str, Any],
               expire: set[str] | None = None) -> float:
        have = _have_ratio(r, available)
        score = have * 5.0
        # constraints
        t = constraints.get("max_time")
        if t and r.time_minutes > t:
            score -= 3
        diff = constraints.get("max_difficulty")
        if diff and r.difficulty > diff:
            score -= 2
        tag = constraints.get("tags")
        if tag and not any(tag in r.tags for tag in [tag]):
            score -= 1
        # preferences (favour matched tags)
        for p in preferences:
            if p in r.tags:
                score += 0.5
        # expiration pressure: if we require quick use, prefer short cook time
        if constraints.get("quick"):
            score += max(0.0, (30 - r.time_minutes) / 30.0)
        # qualities: easy + inexpensive get a small bonus (user's intent)
        score += (5 - r.difficulty) * 0.2     # easier is better
        # cheaper is better — but only when cost is a real value, never reward
        # a fabricated/estimated cost over an unknown one.
        if not r.cost_estimated:
            score += (4.0 - min(r.cost, 4.0)) * 0.2
        # user affinity: rating, favourites, production familiarity
        if r.rating:
            score += min(r.rating, 5.0) * 0.6
        if r.favorite:
            score += 1.0
        if r.times_used:
            score += min(r.times_used, 10) * 0.1
        # expiration pressure: using soon-to-expire ingredients is rewarded
        if expire:
            exp_hit = sum(1 for ing in r.ingredients if _stem(ing) in expire) if expire else 0
            score += exp_hit * 0.8
        # available-ingredient synergy: bonus when ingredients fully on hand
        if have >= 0.999:
            score += 1.5
        return round(score, 3)

    # ------------------------------------------------------------ meal plan
    def plan_meal(self, budget_minutes: int = 45, meal: str = "",
                  preferences: list[str] | None = None) -> dict[str, Any]:
        """Recommend a meal that fits the free-time budget and ingredients.

        The caller (decider / proactive) computes ``budget_minutes`` from the
        scheduler's free time; never recommend cooking longer than available.
        """
        constraints = {"max_time": budget_minutes, "quick": budget_minutes <= 30}
        ranked = self.recipes(preferences=preferences, constraints=constraints)
        for cand in ranked:
            r = cand["recipe"]
            if r.time_minutes <= budget_minutes:
                short = self._shortfall(r, self.inventory.inventory_map())
                info = self._pick(r)
                self.db.add_meal_history(r.recipe_id, meal or "supper",
                                         ts=int(datetime.now().timestamp()))
                return {
                    "meal": meal or "supper",
                    "recipe": r.name,
                    "recipe_id": r.recipe_id,
                    "time_minutes": r.time_minutes,
                    "difficulty": r.difficulty,
                    "cost": r.cost,
                    "score": cand["score"],
                    "have_ratio": round(cand["have"], 2),
                    "missing": short,
                    "steps": r.steps,
                    "source": r.source,
                    "source_url": r.source_url,
                    "servings": info.get("servings", 2),
                }
        return {"meal": meal or "supper", "recipe": None,
                "message": "No recipe fits the available time and ingredients.",
                "missing": []}

    def _shortfall(self, r: Recipe, available: dict[str, float]) -> list[str]:
        return [ing for ing in r.ingredients if not _match_ingredient(ing, available)]

    # ---------------------------------------------------------- grocery list
    def grocery_list(self, meal_plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Deterministic grocery list: meal plan shortfalls + low inventory.

        Dedupes across inventory and repeated meal-plan recipes.
        """
        available = self.inventory.inventory_map()
        need: dict[str, float] = {}
        if meal_plan and meal_plan.get("recipe"):
            for ing in meal_plan.get("missing", []):
                need[ing] = need.get(ing, 0.0) + 1.0
        # low-stock baseline: add name for any common pantry item nearly used up
        for name in ("oil", "salt", "soy sauce", "cheese", "rice", "pasta", "flour"):
            have = available.get(name, 0)
            if have and have <= 0.5:
                need.setdefault(name, 1.0)
        # commit to shopping list without duplicating an existing unpurchased item
        for name, qty in need.items():
            self.db.add_shopping(name, quantity=qty, unit="",
                                 category=self._category_of(name),
                                 needed_for=meal_plan.get("recipe", "") if meal_plan else "")
        return [dict(r) for r in self.db.shopping(purchased=0)]

    @staticmethod
    def _category_of(name: str) -> str:
        name = name.lower()
        groups = {
            "produce": ["tomato", "onion", "carrot", "potato", "celery", "garlic",
                        "broccoli", "peas", "cucumber", "mushroom", "ginger", "chili"],
            "dairy": ["milk", "cheese", "butter", "cream", "yogurt"],
            "meat": ["chicken", "tuna", "beef", "pork", "bacon", "egg"],
            "pantry": ["rice", "pasta", "noodles", "flour", "soy sauce", "oil",
                       "stock", "tortilla", "peanut butter", "parmesan", "sugar"],
        }
        for cat, words in groups.items():
            if any(w in name for w in words):
                return cat
        return "other"


# ---------------------------------------------------------------------------
# web recipe provider (TheMealDB, no API key) — degrades to offline on error.
_API = "https://www.themealdb.com/api/json/v1/1"


def _fetch_web_recipes() -> list[Recipe]:
    """Browse a few meal categories and fetch full (ingredient) recipes."""
    import requests
    out: list[Recipe] = []
    for cat in ("Dinner", "Breakfast", "Vegetarian"):
        try:
            r = requests.get(f"{_API}/filter.php?c={cat}", timeout=8)
            r.raise_for_status()
            meals = (r.json() or {}).get("meals", []) or []
            ids = [m["idMeal"] for m in meals[:8]]
            for mid in ids:
                detail = _meal_detail(mid)
                if detail:
                    out.append(detail)
        except Exception as exc:  # noqa: BLE001
            log.debug("category %s unavailable: %s", cat, exc)
            continue
    return _dedupe_recipes(out)


def _fetch_web_search(query: str) -> list[Recipe]:
    """Recipe Search: ask TheMealDB by name, fetch real recipes."""
    import requests
    try:
        r = requests.get(f"{_API}/search.php?s={query}", timeout=8)
        r.raise_for_status()
        meals = (r.json() or {}).get("meals", []) or []
    except Exception as exc:  # noqa: BLE001
        log.debug("search %s unavailable: %s", query, exc)
        return []
    out = [_meal_detail(m["idMeal"]) for m in meals[:8]]
    return _dedupe_recipes([d for d in out if d])


def _meal_detail(meal_id: str) -> Recipe | None:
    """Fetch full details (ingredients/steps) for one TheMealDB id."""
    import requests
    try:
        r = requests.get(f"{_API}/lookup.php?i={meal_id}", timeout=8)
        r.raise_for_status()
        meals = (r.json() or {}).get("meals", []) or []
        if not meals:
            return None
        m = meals[0]
    except Exception as exc:  # noqa: BLE001
        log.debug("detail %s unavailable: %s", meal_id, exc)
        return None
    ingredients = []
    for i in range(1, 21):
        ing = (m.get(f"strIngredient{i}") or "").strip()
        if not ing:
            continue
        meas = (m.get(f"strMeasure{i}") or "").strip()
        ingredients.append((f"{meas} {ing}" if meas else ing).strip())
    steps = []
    for line in (m.get("strInstructions") or "").replace("\r", "\n").split("\n"):
        s = line.strip()
        if s:
            steps.append(s)
    tags = [t.strip() for t in (m.get("strTags") or "").split(",") if t.strip()]
    if m.get("strArea"):
        tags.insert(0, str(m.get("strArea")).lower())
    if m.get("strCategory"):
        tags.insert(0, str(m.get("strCategory")).lower())
    dedup: list[str] = []
    for t in tags:
        if t.lower() not in dedup:
            dedup.append(t)
    tags = dedup
    n = len(ingredients) or 1
    # TheMealDB does not publish cook/nutrition/cost data, so any numeric value
    # we carry is an *estimate* and must be flagged as such (never presented as
    # a verified fact). We estimate cook time from the complexity of the steps.
    time_minutes = min(90, max(15, n * 6 + 10))
    return Recipe(
        (m.get("strMeal") or "").strip(), ingredients, time_minutes, 2,
        ["pan"], 2.0, tags[:6], steps[:15], "themealdb",
        f"https://www.themealdb.com/meal/{meal_id}", 0, 2,
        time_estimated=True, cost_estimated=True, nutrition_source="",
    )


def _dedupe_recipes(recipes: list[Recipe]) -> list[Recipe]:
    seen: set[str] = set()
    out: list[Recipe] = []
    for r in recipes:
        if not r.name:
            continue
        key = r.name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
