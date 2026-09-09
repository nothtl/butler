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
_KEYS: dict[str, list[str]] = {
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

# Zone keyword match. A zone name is normalised and matched by substring, so
# "Library", "The Library" and "library" all resolve to a study affinity. The
# weights are small (affinity is a soft nudge, never a hard constraint).
_ZONE_KEYS: dict[str, dict[str, int]] = {
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

# Categories that feel like effort; drained by "I'm tired".
_DEMANDING = ("study", "exercise", "work")


def classify(title: str = "", tags: str = "") -> frozenset[str]:
    """Coarse categories a task belongs to. Deterministic and cheap."""
    text = f"{title} {tags}".lower()
    found = [c for c, kws in _KEYS.items() if _any_in(kws, text)]
    if not found:
        found = ["work"]  # unlabelled tasks default to the generic category
    return frozenset(found)


def _any_in(kws: list[str], text: str) -> bool:
    return any(kw in text for kw in kws)


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
