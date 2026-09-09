"""Phase 4.2: deterministic context-aware task affinity.

A soft *scoring* layer that tells the scheduler which tasks are a better match
for the user's current situation (where they are, what they said, how they feel)
without ever over-riding a hard constraint.

Design rules:

  * PURE — no I/O, no LLM, no network. It only converts plain data (zone name,
    task title/tags, the user's raw message) into an integer "affinity".
  * SOFT — an affinity is a tie-breaker inside the scheduler's urgency order
    (see ``butler/schedule.Task.urgency_key``). It never changes *whether* a
    task is feasible, only *which* comparable task is picked first when the
    deadline/priority/remaining are otherwise identical.
  * SAFE — unknown or unavailable presence yields an affinity of ``0`` (exactly
    the pre-HA behaviour). No raw GPS coordinate, entity id or token ever enters
    here; we operate on a resolved zone name only.

The categories below are deliberately small and coarse: study, exercise, cook,
shop, rest, work. Tasks are classified by keyword, zones map to categories, and
the user's explicit statements (``I want to exercise``, ``I don't want to study``,
``I'm tired``) carry far more weight than any inference.
"""

from __future__ import annotations

import re
from typing import Any

# The coarse categories a task (or a zone) can belong to.
CATEGORIES = ("study", "exercise", "cook", "shop", "rest", "work")

# Keyword table: category -> list of substrings (checked against title + tags).
_DEFAULT_KEYS: dict[str, list[str]] = {
    "study": ["study", "homework", "assignment", "project", "read", "review",
              "lecture", "lab", "cs", "math", "physics", "exam", "quiz", "essay",
              "paper", "flashcard", "ml", "algorith"],
    "exercise": ["exercise", "workout", "gym", "run", "jog", "lift", "yoga",
                 "swim", "bike", "cardio", "walk", "stretch"],
    "cook": ["cook", "meal", "recipe", "dinner", "lunch", "breakfast", "bake",
             "chop", "prep", "snack"],
    "shop": ["shop", "shopping", "grocery", "market", "buy", "errand"],
    "rest": ["rest", "nap", "break", "relax", "chill"],
    "work": ["work", "email", "inbox", "respond", "admin", "file", "organise",
             "organize", "clean", "report"],
}

# The active table (``configure`` may override with user-provided keywords).
_KEYS: dict[str, list[str]] = dict(_DEFAULT_KEYS)

# Very short substrings that would over-match as bare substring matches (e.g.
# "cs" inside "ecstatic", "ml" inside "email"). These are matched on word
# boundaries for the literal abbreviation only, so "do the CS homework" still
# matches while random letters don't.
_SHORT_KEYWORDS: frozenset[str] = frozenset({"cs", "ml", "ai", "gym", "lab"})

# Zone keyword match. A zone name is normalised and matched by substring, so
# "Library", "The Library" and "library" all resolve to a study affinity. The
# weights are small (affinity is a soft nudge, never a hard constraint).
_DEFAULT_ZONE_KEYS: dict[str, dict[str, int]] = {
    "library": {"study": 4},
    "study": {"study": 3},
    "classroom": {"study": 3},
    "lab": {"study": 3},
    "campus": {"study": 2, "work": 1},
    "gym": {"exercise": 4},
    "park": {"exercise": 3},
    "pool": {"exercise": 3},
    "court": {"exercise": 2},
    "dorm": {"study": 1, "cook": 1, "rest": 1},
    "home": {"cook": 1, "rest": 1},
    "kitchen": {"cook": 4},
    "store": {"shop": 4},
    "shop": {"shop": 4},
    "grocery": {"shop": 4},
    "market": {"shop": 4},
    "trader": {"shop": 4},
    "costco": {"shop": 4},
    "cafe": {"study": 1, "rest": 1},
    "coffee": {"study": 1, "rest": 1, "work": 1},
    "office": {"work": 3},
    "desk": {"work": 2, "study": 1},
}

# The active zone table (overridable via ``configure``).
_ZONE_KEYS: dict[str, dict[str, int]] = dict(_DEFAULT_ZONE_KEYS)

# Categories that feel like effort; drained by "I'm tired".
_DEMANDING = ("study", "exercise", "work")


def _keyword_tables(overrides: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Return the active keyword table, merging user overrides over the default.

    A user override either adds new categories or replaces a category's keywords
    (an empty list for a category disables it). Purely additive is the safest
    default, so categories not mentioned keep their built-in keywords.
    """
    if not overrides:
        return {cat: list(kws) for cat, kws in _DEFAULT_KEYS.items()}
    table = {cat: list(kws) for cat, kws in _DEFAULT_KEYS.items()}
    table.update({cat: list(kws) for cat, kws in overrides.items()})
    return table


def configure(*, keywords: dict[str, list[str]] | None = None,
              zone_keywords: dict[str, dict[str, int]] | None = None,
              unknown_category: str | None = None) -> None:
    """Install user overrides (audit: no hardcoded mapping we can't change).

    These are applied at process start from ``config/[affinity]``. Passing
    ``None`` for a table keeps the built-in default; ``unknown_category``
    replaces the default "work" label for tasks matching no keyword (``""``
    means neutral/unclassified).
    """
    global _KEYS, _ZONE_KEYS, _UNKNOWN_CATEGORY
    if keywords is not None:
        _KEYS = _keyword_tables(keywords)
    else:
        _KEYS = _keyword_tables(None)
    if zone_keywords is not None:
        _ZONE_KEYS = dict(zone_keywords)
    else:
        _ZONE_KEYS = dict(_DEFAULT_ZONE_KEYS)
    if unknown_category is not None:
        _UNKNOWN_CATEGORY = unknown_category
    else:
        _UNKNOWN_CATEGORY = "work"


# The category a task with no keyword match falls into. Defaults to "work"
# (historical behaviour); set to "" for a neutral/unclassified task.
_UNKNOWN_CATEGORY = "work"


def classify(title: str = "", tags: str = "") -> frozenset[str]:
    """Coarse categories a task belongs to. Deterministic and cheap.

    Tasks matching no keyword are classified as ``_UNKNOWN_CATEGORY`` (an empty
    frozenset when configured neutral), never a hard-coded single category.
    """
    text = f"{title} {tags}".lower()
    found = [c for c, kws in _KEYS.items() if _any_in(kws, text)]
    if not found and _UNKNOWN_CATEGORY:
        found = [_UNKNOWN_CATEGORY]
    return frozenset(found)


def _any_in(kws: list[str], text: str) -> bool:
    for kw in kws:
        if _match_keyword(kw, text):
            return True
    return False


def _match_keyword(kw: str, text: str) -> bool:
    """Match a keyword against text, using word boundaries for short ones."""
    kw = kw.lower()
    if kw in _SHORT_KEYWORDS:
        # Match as a whole word ("cs"), raising false-positives from substrings
        # ("cs" inside "ecstatic"), but still allow prefixes like "cs168"/"ml".
        return re.search(r"(?<![a-z0-9])" + re.escape(kw), text) is not None
    return kw in text


def zone_weights_for(zone: str = "") -> dict[str, int]:
    """Map a resolved zone name to per-category affinity weights (may be empty)."""
    z = (zone or "").lower().strip()
    if not z:
        return {}
    out: dict[str, int] = {}
    for kw, wts in _ZONE_KEYS.items():
        if kw in z:
            for c, w in wts.items():
                out[c] = out.get(c, 0) + w
    return out


def preference_weights(message: str = "") -> dict[str, int]:
    """Affinity from the user's *explicit* words — far stronger than inference."""
    m = (message or "").lower()
    w: dict[str, int] = {}

    if re.search(r"\b(want|would like|prefer|rather|let's|let us|i'd like)\b[\s\S]{0,40}"
                 r"\b(exercise|exercising|workout|go to the gym|go gym|gym|run|jog)\b", m):
        w["exercise"] = max(w.get("exercise", 0), 6)
    if re.search(r"\b(want|would like|prefer)\b[\s\S]{0,40}\b(study|studying)\b", m) or \
            "want to study" in m:
        w["study"] = max(w.get("study", 0), 6)
    if re.search(r"\b(want|would like|prefer)\b[\s\S]{0,40}\b(cook|cooking|make dinner|make lunch)\b", m) \
            or "want to cook" in m:
        w["cook"] = max(w.get("cook", 0), 6)
    if re.search(r"\b(want|would like|prefer)\b[\s\S]{0,40}\b(shopping|shop|buy|grocery|go to the store)\b", m):
        w["shop"] = max(w.get("shop", 0), 6)
    if re.search(r"\b(want|would like|need)\b[\s\S]{0,40}\b(rest|resting|nap|break|relax)\b", m):
        w["rest"] = max(w.get("rest", 0), 6)

    # Negative statements weigh against a category (respect the user's intent).
    if re.search(r"\b(don't|do not|dont|doesn't|not)\b[\s\S]{0,40}\b(want to study|studying|study)\b", m) \
            or "don't want to study" in m:
        w["study"] = min(w.get("study", 0), -8)
    if re.search(r"\b(don't|do not|dont)\b[\s\S]{0,40}\b(exercise|workout|gym|running)\b", m):
        w["exercise"] = min(w.get("exercise", 0), -8)
    if re.search(r"\b(don't|do not|dont)\b[\s\S]{0,40}\b(cook|cooking)\b", m):
        w["cook"] = min(w.get("cook", 0), -6)
    if re.search(r"\b(don't|do not|dont)\b[\s\S]{0,40}\b(rest|nap)\b", m):
        w["rest"] = min(w.get("rest", 0), -6)

    return w


def is_tired(message: str = "") -> bool:
    """True when the user flags low energy in the message."""
    m = (message or "").lower()
    return bool(re.search(r"\b(tired|exhausted|sleepy|drained|burnt out|burned out|"
                          r"worn out|no energy|low energy)\b", m))


def task_affinity(title: str = "", tags: str = "", zone: str = "",
                  preferences: dict[str, int] | None = None,
                  tired: bool = False) -> int:
    """The soft affinity for one task given the current situation.

    Sums category affinity from the zone, the user's explicit preference, and a
    small energy adjustment. Unknown zone/preferences yield ``0``.
    """
    cats = classify(title, tags)
    prefs = preferences or {}
    score = 0
    zw = zone_weights_for(zone)
    for c in cats:
        score += zw.get(c, 0) + prefs.get(c, 0)
    if tired:
        for c in cats:
            if c in _DEMANDING:
                score -= 2
    return score


def zone_category_names(zone: str = "") -> list[str]:
    """The human labels for the categories a zone favours (for explanation)."""
    return sorted(zone_weights_for(zone).keys())


def _json_safe(value: Any) -> Any:
    return value
