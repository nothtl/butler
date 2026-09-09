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

import logging
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

    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "proactive_enabled", True))

    # ---------------------------------------------------------- checks
    def collect(self) -> list[str]:
        msgs: list[str] = []
        msgs += self._food_alerts()
        msgs += self._course_alerts()
        msgs += self._event_alerts()
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

    # ---------------------------------------------------------- send
    def run(self) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "reason": "proactive disabled"}
        msgs = self.collect()
        if not msgs:
            return {"ok": True, "sent": 0, "messages": []}
        chat = self.cfg.notify_chat or self.cfg.digest_chat
        body = "\n".join(msgs)
        sent = 0
        if chat and self.cfg.telegram_token:
            if self._push(chat, body):
                sent = 1
        else:
            log.info("proactive (%d messages no telegram configured):\n%s", len(msgs), body)
        return {"ok": True, "sent": sent, "messages": msgs}

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
