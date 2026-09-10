"""N4: a single runtime settings layer over the existing configuration.

Butler already had three places settings could live: the TOML config, per-topic
`topic_settings`, and the `app_settings` key/value table. N4 consolidates the
*system* settings into one thin service so that natural language, `/settings`
and the CLI all change the same underlying state, and so the values survive a
restart.

This is **not** a second settings system: it validates a small safe whitelist and
applies it to the existing :class:`~butler.config.Config`; overrides are
persisted in the existing `app_settings` table.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

log = logging.getLogger("butler.settings")

#: Safe, user-facing system settings. Anything not listed is read-only.
SPECS: dict[str, dict[str, Any]] = {
    "work_start": {"type": "time", "label": "Day starts",
                   "attr": "sleep_end", "group": "scheduling"},
    "work_end": {"type": "time", "label": "Day ends",
                 "attr": "sleep_start", "group": "scheduling"},
    "quiet_start": {"type": "time", "label": "Quiet hours start",
                    "attr": "notify_quiet_start", "group": "notifications"},
    "quiet_end": {"type": "time", "label": "Quiet hours end",
                  "attr": "notify_quiet_end", "group": "notifications"},
    "proactive_enabled": {"type": "bool", "label": "Proactive alerts",
                          "attr": "proactive_enabled", "group": "notifications"},
    "proactive_min_priority": {"type": "choice", "label": "Alert threshold",
                               "attr": "proactive_min_priority",
                               "choices": ["critical", "high", "medium", "low"],
                               "group": "notifications"},
    "memory_enabled": {"type": "bool", "label": "Memory",
                       "attr": "memory_enabled", "group": "memory"},
    "web_enabled": {"type": "bool", "label": "Web knowledge",
                    "attr": "web_enabled", "group": "integrations"},
    "tracker_enabled": {"type": "bool", "label": "Tracking",
                        "attr": "tracker_enabled", "group": "integrations"},
    "confirm_calendar": {"type": "bool", "label": "Ask before calendar changes",
                         "attr": "", "group": "scheduling", "default": True},
    "location_precision": {"type": "choice", "label": "Location precision",
                           "attr": "", "choices": ["zone", "off"],
                           "group": "location", "default": "zone"},
}

GROUPS: dict[str, str] = {
    "scheduling": "📅 Scheduling",
    "notifications": "🔔 Notifications",
    "memory": "🧠 Memory",
    "integrations": "🔗 Integrations",
    "location": "📍 Location",
}

_STORE_KEY = "runtime_settings"


def _now() -> int:
    return int(time.time())


def parse_time(text: str) -> int | None:
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", (text or "").lower())
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2) or 0)
    ap = m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def fmt_time(minutes: int) -> str:
    return f"{int(minutes) // 60:02d}:{int(minutes) % 60:02d}"


class SettingsService:
    def __init__(self, container: Any):
        self.container = container
        self.cfg = getattr(container, "cfg", None)
        self.db = getattr(container, "db", None)
        self.audit = getattr(container, "audit", None)

    # ------------------------------------------------------------- storage
    def _load(self) -> dict[str, Any]:
        raw = ""
        try:
            raw = self.db.get_setting(_STORE_KEY, "") if self.db is not None else ""
        except Exception:  # noqa: BLE001
            raw = ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except (TypeError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        if self.db is None:
            return
        self.db.set_setting(_STORE_KEY, json.dumps(data, sort_keys=True))

    def apply_to_config(self) -> None:
        """Apply persisted overrides to the live Config (called at startup)."""
        cfg = self.cfg
        if cfg is None:
            return
        for key, value in self._load().items():
            self._set_attr(key, value)

    def _set_attr(self, key: str, value: Any) -> None:
        spec = SPECS.get(key)
        if spec is None or self.cfg is None:
            return
        attr = spec.get("attr")
        if not attr:
            return
        try:
            if spec["type"] == "time":
                setattr(self.cfg, attr, int(value))
            elif spec["type"] == "bool":
                setattr(self.cfg, attr, bool(value))
            else:
                setattr(self.cfg, attr, str(value))
        except Exception:  # noqa: BLE001
            log.debug("settings apply failed for %s", key, exc_info=True)

    # ------------------------------------------------------------- reads
    def get(self, key: str) -> Any:
        spec = SPECS.get(key)
        if spec is None:
            return None
        attr = spec.get("attr")
        if attr and self.cfg is not None and hasattr(self.cfg, attr):
            return getattr(self.cfg, attr)
        return self._load().get(key, spec.get("default"))

    def snapshot(self) -> dict[str, Any]:
        return {k: self.get(k) for k in SPECS}

    def render(self) -> str:
        """A concise, grouped, secret-free system settings view."""
        lines = ["⚙️ Butler Settings", ""]
        for group, title in GROUPS.items():
            keys = [k for k, s in SPECS.items() if s["group"] == group]
            if not keys:
                continue
            lines.append(title)
            for k in keys:
                spec = SPECS[k]
                val = self.get(k)
                if spec["type"] == "time":
                    val_s = fmt_time(int(val or 0))
                elif spec["type"] == "bool":
                    val_s = "on" if val else "off"
                else:
                    val_s = str(val)
                lines.append(f"  {spec['label']}: {val_s}")
            lines.append("")
        lines.append("Secrets are never shown. Change these by typing naturally, "
                     "e.g. \"don't schedule work after 10 PM\".")
        return "\n".join(lines)

    # ------------------------------------------------------------- writes
    def set(self, key: str, value: Any, *, now: int | None = None) -> dict[str, Any]:
        spec = SPECS.get(key)
        if spec is None:
            return {"ok": False, "error": f"unknown setting {key!r}"}
        if spec["type"] == "time":
            try:
                value = int(value)
            except (TypeError, ValueError):
                return {"ok": False, "error": "expected a time"}
            if not (0 <= value <= 1439):
                return {"ok": False, "error": "time out of range"}
        elif spec["type"] == "bool":
            value = bool(value)
        elif spec["type"] == "choice":
            if str(value) not in spec.get("choices", []):
                return {"ok": False, "error": f"expected one of {spec['choices']}"}
            value = str(value)
        data = self._load()
        data[key] = value
        self._save(data)
        self._set_attr(key, value)
        self._audit(key, value)
        return {"ok": True, "key": key, "value": value}

    def _audit(self, key: str, value: Any) -> None:
        audit = getattr(self.container, "audit", None)
        if audit is None:
            return
        try:
            audit.record("settings_change", actor="settings", target=key,
                         decision="allowed", outcome="ok",
                         detail={"key": key, "value": value})
        except Exception:  # noqa: BLE001
            log.debug("settings audit failed", exc_info=True)

    # ------------------------------------------------------------- NL parse
    def parse(self, text: str) -> dict[str, Any] | None:
        """Deterministically map a natural-language settings request."""
        low = (text or "").lower()
        if not re.search(r"\b(set|make|change|don'?t|do not|stop|disable|enable|"
                         r"quiet|after|before|hours?|threshold|precision|"
                         r"location|proactiv\w*|memory|web)\b", low):
            return None
        changes: dict[str, Any] = {}
        # quiet hours: "quiet hours 11 PM to 7 AM"
        m = re.search(r"quiet hours?\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*"
                      r"(?:to|until|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", low)
        if m:
            s, e = parse_time(m.group(1)), parse_time(m.group(2))
            if s is not None and e is not None:
                changes["quiet_start"], changes["quiet_end"] = s, e
        # no work after/before
        m = re.search(r"(?:don'?t|do not|no|stop|never)\s+(?:schedule\s+)?"
                      r"(?:work\s+)?after\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", low)
        if m:
            v = parse_time(m.group(1))
            if v is not None:
                changes["work_end"] = v
        m = re.search(r"(?:don'?t|do not|no|stop|never)\s+(?:schedule\s+)?"
                      r"(?:work\s+)?before\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", low)
        if m:
            v = parse_time(m.group(1))
            if v is not None:
                changes["work_start"] = v
        # proactive low priority
        if re.search(r"(don'?t|do not|stop)\b[^.]*\b(proactiv|notif|alert|"
                     r"message)\w*[^.]*\blow", low) or \
                re.search(r"\blow[- ]priority\b[^.]*\b(proactiv|notif|alert)",
                          low):
            changes["proactive_min_priority"] = "medium"
        if re.search(r"\bdisable\b[^.]*\bproactiv", low) or \
                re.search(r"\bstop\b[^.]*\bproactiv", low):
            changes["proactive_enabled"] = False
        if re.search(r"\benable\b[^.]*\bproactiv", low):
            changes["proactive_enabled"] = True
        if re.search(r"\b(disable|turn off)\b[^.]*\bweb\b", low):
            changes["web_enabled"] = False
        if re.search(r"\b(enable|turn on)\b[^.]*\bweb\b", low):
            changes["web_enabled"] = True
        if re.search(r"\b(disable|turn off)\b[^.]*\bmemory\b", low):
            changes["memory_enabled"] = False
        if re.search(r"\b(enable|turn on)\b[^.]*\bmemory\b", low):
            changes["memory_enabled"] = True
        if re.search(r"location\b[^.]*\b(zone|off)\b", low):
            changes["location_precision"] = "off" if "off" in low else "zone"
        if re.search(r"ask before\b[^.]*\bcalendar\b", low):
            changes["confirm_calendar"] = True
        if not changes:
            return None
        return {"ok": True, "changes": changes,
                "summary": self._summary(changes)}

    def _summary(self, changes: dict[str, Any]) -> str:
        bits = []
        for k, v in changes.items():
            spec = SPECS.get(k, {})
            label = spec.get("label", k)
            if spec.get("type") == "time":
                v = fmt_time(int(v))
            elif spec.get("type") == "bool":
                v = "on" if v else "off"
            bits.append(f"{label}: {v}")
        return "I'll update — " + ", ".join(bits) + "."
