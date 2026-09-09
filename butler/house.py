"""Home Assistant connector (Phase 4.1: presence).

A reliable source of *where the user is* for the context engine and Telegram
intents. It uses only the HA REST API (state polling) — no websocket event
layer, so it is deterministic and easy to test offline.

Like :mod:`butler.gcal` it carries the whole HA surface so it can be exercised
without a network: every call goes through an injectable ``transport`` whose
contract is

    get(url, *, headers) -> (status_code, response_json)

``requests`` is the default transport; tests substitute a stub that returns
canned HA state lists, so the parsing and the zone/presence derivation are
unit-tested with no Home Assistant and no real access token.

Errors are typed (``HAAuthError`` / ``HAOutage``) so the caller can degrade
gracefully to "unknown" presence and keep every other subsystem working. The
access token is captured once from the config and is never logged, never
written to ``to_dict``, and never exposed to the decider/LLM — presence is
resolved to a plain ``{zone, status, battery, available}`` dict here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

log = logging.getLogger("butler.house")


class HAClientError(Exception):
    """Base error: Home Assistant could not be queried."""


class HAAuthError(HAClientError):
    """Missing/invalid access token or an authorisation failure."""


class HAOutage(HAClientError):
    """The HA backend is unavailable (network/HTTP/API problem)."""


def _safe_json(resp: Any) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


# States that mean "we have no usable location for this tracker".
_UNKNOWN_STATES = {"unknown", "unavailable", "none", ""}


def _is_coord(text: str) -> bool:
    """A raw lat/lon pair like ``'33.693,-75.123'`` is not a zone name."""
    text = (text or "").strip()
    return bool(re.fullmatch(r"\s*[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?\s*", text))


def _norm_zone(state: str, attrs: dict[str, Any]) -> str:
    """Derive a named zone from a device_tracker entity's state/attributes.

    Prefers an explicit ``location_name``/``zone`` attribute over the state,
    and never returns a raw GPS coordinate or an unknown/unavailable state.
    Returns ``""`` when unknown.
    """
    s = (state or "").strip().lower()
    if s in _UNKNOWN_STATES:
        return ""
    loc = attrs.get("location_name")
    if loc and not _is_coord(str(loc)):
        z = str(loc).strip()
        if z and z.lower() not in ("not_home", "nothome"):
            return z
    zone_attr = attrs.get("zone")
    if zone_attr and not _is_coord(str(zone_attr)):
        z = str(zone_attr).strip()
        if z and z.lower() not in ("not_home", "nothome"):
            return z
    if s and s != "not_home" and not _is_coord(s):
        return s
    return ""


def _status_of(state: str, zone: str) -> str:
    """home | away | unknown.

    'home' only when the tracker reports being at home; a named non-home zone
    (e.g. Library) or 'not_home' is 'away'; everything else is 'unknown'.
    """
    s = (state or "").strip().lower()
    if s in _UNKNOWN_STATES:
        return "unknown"
    if s == "not_home":
        return "away"
    if s == "home" or zone.lower() in ("home", ""):
        return "home" if s == "home" else "unknown"
    return "away"


def _battery_of(attrs: dict[str, Any]) -> int | None:
    for key in ("battery", "battery_level", "battery_pct"):
        val = attrs.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return int(val)
        if isinstance(val, str):
            try:
                return int(float(val))
            except ValueError:
                continue
    return None


class _DefaultTransport:
    """Adapts ``requests`` to the connector's (status, json) contract."""

    def get(self, url: str, headers: dict[str, Any] | None = None) -> tuple[int, Any]:
        r = requests.get(url, headers=headers, timeout=10)
        return r.status_code, _safe_json(r)


class HomeAssistant:
    def __init__(self, cfg: Any, http: Any | None = None):
        self.cfg = cfg
        self.http = http or _DefaultTransport()
        self._url = (getattr(cfg, "home_assistant_url", "") or "").rstrip("/")
        self._token = getattr(cfg, "home_assistant_token", "") or ""

    # --------------------------------------------------------------- gate
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "home_assistant_enabled", False)) and bool(self._url)

    def _auth(self) -> dict[str, str]:
        if not self._token:
            raise HAAuthError("Home Assistant token not configured "
                              "(BUTLER_HA_TOKEN / config `home_assistant.token`).")
        return {"Authorization": f"Bearer {self._token}"}

    # --------------------------------------------------------------- fetch
    def states(self) -> list[dict[str, Any]]:
        """Fetch every HA state as ``[{entity_id, state, attributes}, ...]``."""
        if not self.enabled():
            raise HAOutage("Home Assistant not enabled "
                           "(`home_assistant.enabled`/`home_assistant.url`).")
        code, data = self.http.get(self._url + "/api/states", headers=self._auth())
        if code in (401, 403):
            raise HAAuthError(f"Home Assistant authorisation failed (HTTP {code}). "
                              "Check the access token.")
        if code != 200 or not isinstance(data, list):
            err = data if isinstance(data, dict) else {}
            raise HAOutage(f"Home Assistant API unavailable (HTTP {code}): {err}")
        return [e for e in data if isinstance(e, dict)]

    # ------------------------------------------------------------ presence
    def presence(self, states: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Resolve zone-level presence. Never raises; always syntactically full.

        ``available`` is True whenever at least one device_tracker is reporting
        (state not unknown/unavailable); ``known`` is True when we can name a
        zone or a status. On any HA failure it degrades to ``unknown`` so the
        caller (context engine, Telegram) keeps working.
        """
        try:
            states = states if states is not None else self.states()
        except HAClientError as exc:
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant",
                    "error": str(exc)}

        trackers = [s for s in states
                    if str(s.get("entity_id", "")).startswith("device_tracker.")]
        if not trackers:
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant"}

        best: dict[str, Any] | None = None
        best_score = -1
        for ent in trackers:
            state = str(ent.get("state") or "")
            attrs = ent.get("attributes") or {}
            zone = _norm_zone(state, attrs)
            status = _status_of(state, zone)
            battery = _battery_of(attrs)
            available = state.lower() not in _UNKNOWN_STATES or status != "unknown"
            score = (
                (2 if zone else 0)
                + (2 if status != "unknown" else 0)
                + (1 if battery is not None else 0)
                + (1 if available else 0)
            )
            if score > best_score or (score == best_score and best and
                                      ent.get("entity_id", "") < best.get("entity_id", "")):
                best_score = score
                best = {"zone": zone, "status": status, "battery": battery,
                        "available": available, "entity_id": ent.get("entity_id", "")}

        if best is None:
            return {"known": False, "zone": "", "status": "unknown",
                    "battery": None, "available": False, "source": "home_assistant"}

        # Battery: prefer the primary tracker's, else the highest reported.
        battery = best["battery"]
        if battery is None:
            batts = [_battery_of(s.get("attributes") or {}) for s in trackers]
            battery = max((b for b in batts if b is not None), default=None)

        known = bool(best["zone"] or best["status"] != "unknown")
        return {"known": known, "zone": best["zone"],
                "status": best["status"], "battery": battery,
                "available": best["available"], "source": "home_assistant"}

    def status(self) -> dict[str, Any]:
        try:
            self.states()
        except HAClientError as exc:
            return {"ok": False, "connected": False, "error": str(exc)}
        return {"ok": True, "connected": True}
