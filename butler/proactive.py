"""Phase 3: Proactive Butler.

Runs on a cadence (wired into the scheduler) and surfaces the things worth
knowing *now*: upcoming course deadlines, food about to expire, hard events
today, and what task to start next. Every check reads only deterministic state
(context engine) — no DeepSeek is needed to decide what to say, so it is cheap
and reliable.

``collect()`` returns a list of messages; ``run()`` sends them to the notify
chat via Telegram (or returns them so the CLI/decider can print them).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any

log = logging.getLogger("butler.proactive")


class Proactive:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = container.cfg
        self.db = container.db
        self.context = getattr(container, "context", None)
        self.courses = getattr(container, "courses", None)
        self.food = getattr(container, "food", None)
        self.chef = getattr(container, "chef", None)
        self.executive = getattr(container, "executive", None)
        # Last push bookkeeping for throttling/dedup (persisted under state_dir).
        self._state_file = os.path.join(self.cfg.state_dir, "proactive_state.json")
        self._state = self._load_state()

    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "proactive_enabled", True))

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._state_file) as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return {"last_push": 0, "last_messages": []}

    def _save_state(self) -> None:
        try:
            dirn = os.path.dirname(self._state_file)
            os.makedirs(dirn, exist_ok=True)
            with open(self._state_file, "w") as fh:
                json.dump(self._state, fh)
        except Exception as exc:  # noqa: BLE001
            log.warning("proactive state save failed: %s", exc)

    # ---------------------------------------------------------- throttling
    def _in_quiet_hours(self) -> bool:
        quiet_end = int(getattr(self.cfg, "notify_quiet_end", 8 * 60))
        quiet_start = int(getattr(self.cfg, "notify_quiet_start", 22 * 60))
        if quiet_start <= 0 or quiet_end <= 0:
            return False  # quiet hours disabled
        now = datetime.now()
        m = now.hour * 60 + now.minute
        if quiet_start <= quiet_end:  # does not wrap past midnight (e.g. 13:00-15:00)
            return not (quiet_end <= m < quiet_start)
        # wraps past midnight (e.g. 22:00-08:00) => quiet at the tail or head
        return m >= quiet_start or m < quiet_end

    def _dedup_filter(self, msgs: list[str]) -> list[str]:
        window = int(getattr(self.cfg, "notify_dedup_window_minutes", 30)) * 60
        cutoff = time.time() - window
        last = self._state.get("last_messages", [])
        fresh = [m for m, t in last if t >= cutoff] if last else []
        return [m for m in msgs if m not in fresh]

    # ---------------------------------------------------------- checks
    def collect(self) -> list[str]:
        msgs: list[str] = []
        msgs += self._food_alerts()
        msgs += self._course_alerts()
        msgs += self._event_alerts()
        msgs += self._meal_suggestion()
        msgs += self._executive_alerts()
        if not msgs:
            return []
        return msgs

    def _food_alerts(self) -> list[str]:
        inv = self.food if hasattr(self.food, "expiring") else \
            (self.chef.inventory if self.chef else None)
        if inv is None:
            return []
        out = []
        for item in inv.expiring(3):
            if item.get("expired"):
                name = item["name"]
                # only notify once by recording; keep it simple here
                out.append(f"⚠ {name} has EXPIRED (${item.get('expiration_date')})")
            else:
                out.append(f"⏳ {item['name']} expires in {item['days_left']}d — "
                           f"consider using it in a meal.")
        return out

    def _course_alerts(self) -> list[str]:
        if not self.courses:
            return []
        snap = self.context.snapshot() if self.context else None
        if not snap:
            return []
        out = []
        now = int(datetime.now().timestamp())
        for c in snap["courses"]:
            for d in c["upcoming_deadlines"]:
                days = max(0, round((d - now) / 86400))
                if days > 7:
                    continue
                label = "today" if days == 0 else f"in {days}d"
                out.append(f"🎓 {c['code']}: deadline {label} ({_fmt(d)})")
        return out

    def _event_alerts(self) -> list[str]:
        snap = self.context.snapshot() if self.context else None
        if not snap:
            return []
        now = int(datetime.now().timestamp())
        out = []
        for e in snap["events_today"]:
            start = int(e["start_ts"])
            if now <= start <= now + 2 * 3600:
                hm = datetime.fromtimestamp(start).strftime("%H:%M")
                out.append(f"📅 {e['title']} at {hm}")
        return out

    # -------------------------------------------------- Phase 4.5 meal nudge
    def _meal_suggestion(self) -> list[str]:
        """A context-aware, *soft*, never-purchasing meal suggestion.

        Only fires on a meaningful reason — expiring ingredients that a meal
        could use, OR it being a typical meal time with enough room — and only
        if the hard budget is positive. It never records a meal, imports a
        recipe, or adds groceries (uses :meth:`FoodPlanner.peek`).
        """
        fp = getattr(self.container, "foodplan", None)
        if fp is None:
            return []
        b = fp.budget()
        budget = b.get("budget_minutes", 0)
        if budget <= 0:
            return []
        now_local = self.cfg.now_local()
        now_min = now_local.hour * 60 + now_local.minute
        meal_window = _MEAL_WINDOW(now_min)
        expiring: set[str] = set()
        inv = self.food if hasattr(self.food, "expiring") else \
            (self.chef.inventory if self.chef else None)
        if inv is not None:
            for item in inv.expiring(3):
                name = (item.get("name") or "").strip()
                if name:
                    expiring.add(name)
        if not expiring and meal_window is None:
            return []  # no meaningful reason — don't nag
        try:
            res = fp.peek()
        except Exception as exc:  # noqa: BLE001 — never break the cadence
            log.debug("proactive meal peek failed: %s", exc)
            return []
        plan = res.get("plan") or {}
        if not plan.get("recipe"):
            return []
        how = "Uses ingredients that will expire soon." if expiring else \
            (f"It's {meal_window} time and you have {budget} min free.")
        return [f"🍳 {plan['recipe']} (~{plan['time_minutes']}min). {how} "
                f"Want me to cook it?\n"
                f"[Cook this] / [Another option] / [Not tonight]"]

    # -------------------------------------------------- Phase 5.1 executive
    def _executive_alerts(self) -> list[str]:
        """Hard, executive-level nudges: an imminent deadline, a stalled task, or
        a schedule conflict. These are *deterministic* (read from the planner and
        context engine) and never mutate anything. Soft things (mood, meal) live
        elsewhere and already respect the throttle."""
        executive = getattr(self, "executive", None)
        if executive is None or not hasattr(executive, "_tasks_safe"):
            return []
        try:
            tasks = executive._tasks_safe()
        except Exception:  # noqa: BLE001
            return []
        out = []
        now = int(datetime.now().timestamp())
        for t in tasks:
            if t["status"] == "blocked":
                out.append(f"🔵 Blocked: {t['title']}")
                continue
            if t["status"] not in ("todo", "doing", "scheduled"):
                continue
            dl = t.get("deadline") or 0
            if dl and dl > now and (dl - now) <= 3 * 3600:
                hm = datetime.fromtimestamp(dl).strftime("%H:%M")
                out.append(f"⏰ Deadlines soon: {t['title']} due {hm}")
            elif dl and dl <= now:
                out.append(f"🔴 Overdue: {t['title']}")
        capped = []
        by_title: set[str] = set()
        for m in out:
            if m not in by_title:
                capped.append(m)
                by_title.add(m)
        # Cap executive nags independently (keeps the daily push clean).
        maxnot = int(getattr(self.cfg, "executive_max_notifications", 3))
        if maxnot >= 0 and len(capped) > maxnot:
            capped = capped[:maxnot]
        return capped

    # ---------------------------------------------------------- send
    def run(self) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "reason": "proactive disabled"}
        if self._in_quiet_hours():
            return {"ok": False, "reason": "quiet hours", "sent": 0, "messages": []}

        msgs = self.collect()

        # Phase 5.1: deliver today's due daily briefing/review first (idempotent
        # markers), so they are never lost even when there are no alert messages
        # this cadence. They are not subject to the alert cooldown.
        delivered = self._deliver_executive()
        sent = delivered["sent"]
        sent_msgs = list(delivered["messages"])

        if not msgs:
            return {"ok": True, "sent": sent, "messages": sent_msgs}

        # Throttle: dedup identical alerts, respect cooldown and per-cadence cap.
        cooldown = int(getattr(self.cfg, "notify_cooldown_minutes", 15)) * 60
        now = time.time()
        if self._state.get("last_push", 0) and (now - self._state["last_push"]) < cooldown:
            return {"ok": False, "reason": "cooldown", "sent": sent, "messages": msgs}
        msgs = self._dedup_filter(msgs)
        cap = int(getattr(self.cfg, "notify_max_per_cadence", 5))
        if cap >= 0 and len(msgs) > cap:
            msgs = msgs[:cap]

        if msgs:
            chat = self.cfg.notify_chat or self.cfg.digest_chat
            body = "\n".join(msgs)
            delta = 0
            if chat and self.cfg.telegram_token:
                if self._push(chat, body):
                    delta = 1
            else:
                log.info("proactive (%d messages no telegram configured):\n%s",
                         len(msgs), body)
            if delta:
                self._state["last_push"] = now
                self._state["last_messages"] = [(m, now) for m in msgs]
                self._save_state()
                sent += delta
            sent_msgs = msgs + sent_msgs

        if not sent_msgs:
            return {"ok": True, "sent": 0, "messages": []}
        return {"ok": True, "sent": sent, "messages": sent_msgs}

    def _deliver_executive(self) -> dict[str, Any]:
        """Send today's due daily briefing and/or review (idempotent)."""
        executive = getattr(self, "executive", None)
        if executive is None:
            return {"sent": 0, "messages": []}
        now_ts = int(datetime.now().timestamp())
        chat = self.cfg.notify_chat or self.cfg.digest_chat
        msgs = []
        sent = 0
        for kind, build in ((getattr(executive, "BRIEFING_KIND", "briefing"), executive.briefing),
                            (getattr(executive, "REVIEW_KIND", "review"), executive.review)):
            try:
                if not executive.is_due(kind, now_ts):
                    continue
                piece = build()  # type: ignore[misc]
                text = str(piece.get("text", ""))
                if not text:
                    continue
                if chat and self.cfg.telegram_token:
                    if self._push(chat, text):
                        sent += 1
                else:
                    log.info("executive %s (no telegram configured):\n%s", kind, text)
                executive.mark_delivered(kind, now_ts)
                msgs.append(text)
            except Exception as exc:  # noqa: BLE001 — never break the cadence
                log.warning("executive %s delivery failed: %s", kind, exc)
        return {"sent": sent, "messages": msgs}

    def _push(self, chat_id: int, text: str) -> bool:
        try:
            import requests
            r = requests.post(
                f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage",
                json={"chat_id": chat_id, "text": text}, timeout=15)
            return r.ok
        except Exception as exc:  # noqa: BLE001
            log.warning("proactive push failed: %s", exc)
            return False


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%b %-d") if ts else "?"


_MEAL_WINDOWS = {
    "breakfast": (6 * 60, 10 * 60),
    "lunch": (11 * 60, 14 * 60),
    "dinner": (17 * 60, 21 * 60),
}


def _MEAL_WINDOW(now_min: int) -> str | None:
    for meal, (lo, hi) in _MEAL_WINDOWS.items():
        if lo <= now_min <= hi:
            return meal
    return None
