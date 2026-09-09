"""Reward library + topic-routing helpers (motivation & per-topic behaviour).

Rewards are deterministic: they let the agent suggest a small treat or rest
break after a task is completed, scaled by how important/heavy that task was.
Nothing is fabricated — rewards are user/library defined. ``topic_routing``
maps a forum topic (by title or ``chat_id:thread_id``) onto a default intent
kind, so free text in that topic answers with the relevant command.
"""

from __future__ import annotations

import time
from typing import Any

import sqlite3

log = __import__("logging").getLogger("butler.motivation")
import re  # noqa: E402

# Default reward library seeded when the table is empty.
_DEFAULT_REWARDS = [
    ("20 min walk", "rest", 2, ""),
    ("short break", "rest", 1, ""),
    ("read a chapter", "rest", 2, ""),
    ("game break", "rest", 1, ""),
    ("nap", "rest", 3, ""),
    ("matcha latte", "treat", 2, "matcha"),
    ("snack", "treat", 1, ""),
    ("treat yourself", "treat", 3, ""),
    ("coffee run", "treat", 2, "coffee"),
]

# How to scale the suggested reward weight to a completed task.
_WEIGHT_BY_MINUTES = [(0, 1), (30, 2), (60, 3), (120, 4), (180, 5)]
_WEIGHT_BY_PRIORITY = {0: 0, 1: 1, 2: 1, 3: 2, 5: 3}


def seed_rewards(db: Any) -> None:
    """Idempotently seed the default library if the table is empty."""
    try:
        if db.rewards():
            return
    except Exception:
        db.conn.executescript(
            "CREATE TABLE IF NOT EXISTS rewards(id INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL, kind TEXT DEFAULT 'rest', weight INTEGER"
            " DEFAULT 1, stock_food TEXT DEFAULT '', times_used INTEGER"
            " DEFAULT 0, last_used INTEGER DEFAULT 0, created_at INTEGER DEFAULT 0)")
    for name, kind, weight, stock in _DEFAULT_REWARDS:
        db.add_reward(name, kind, weight, stock)


def task_importance(minutes: int = 0, priority: int = 0) -> int:
    """A 1..5 importance estimate for a just-completed task."""
    m_w = 1
    for thr, w in _WEIGHT_BY_MINUTES:
        if minutes >= thr:
            m_w = w
    p_w = _WEIGHT_BY_PRIORITY.get(int(priority), 0)
    return max(1, min(5, m_w + p_w))


def suggest_reward(db: Any, importance: int) -> dict[str, Any] | None:
    """Pick a reward roughly matching the task importance, least recently used.

    Rewards are chosen from the defined library so nothing is invented. Larger
    importance nudges toward treats/lighter weight; easiest first so small wins
    feel achievable.
    """
    import random
    rows = list(db.rewards())
    if not rows:
        return None
    want = max(1, min(5, int(importance)))
    best = sorted(rows, key=lambda r: (
        abs(int(r["weight"]) - (want // 2 + 1)),
        int(r["last_used"]), random.random(),
    ))
    return dict(best[0])


def reward_line(reward: dict[str, Any] | None) -> str:
    if not reward:
        return "Take a well-earned break 🎉"
    kind = reward.get("kind", "rest")
    name = reward.get("name", "a treat")
    if kind == "treat":
        return f"Reward idea: {name} (grab it for later) 🍬"
    return f"Reward idea: {name} 🌿"


def topic_key(chat_id: int, thread_id: int | None) -> str:
    return f"{int(chat_id)}:{int(thread_id or 0)}"


def get_topic(db: Any, chat_id: int, thread_id: int | None) -> sqlite3.Row | None:
    return db.topic_setting(int(chat_id), int(thread_id or 0))


def routing_for(db: Any, chat_id: int, thread_id: int | None,
                title: str = "") -> str:
    """Best default intent kind for a topic, or '' if none is configured."""
    row = get_topic(db, chat_id, thread_id)
    if row is not None and row["routing"]:
        return row["routing"]
    if title:
        by_title = db.topic_setting_by_title(title)
        if by_title is not None and by_title["routing"]:
            return by_title["routing"]
    return ""


def pending_push_topics(db: Any) -> list[dict[str, Any]]:
    """Topics with push_on enabled, sorted by due push_time (HH:MM)."""
    out = []
    for row in db.topic_settings_all():
        if not row["push_on"]:
            continue
        out.append({
            "chat_id": int(row["chat_id"]),
            "thread_id": int(row["thread_id"]),
            "topic": row["topic"] or "",
            "push_time": row["push_time"] or "07:00",
            "push_freq": row["push_freq"] or "daily",
            "routing": row["routing"] or "",
        })
    out.sort(key=lambda t: t["push_time"])
    return out


def parse_push_time(value: str, default: str = "07:00") -> str:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(value).strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else default
