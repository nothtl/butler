"""Google Calendar connector (Phase 2).

Uses OAuth 2.0 *device flow* so it works on a headless Pi:

  * ``connect(cfg)`` prints a URL + one-time code, then polls until the user
    authorises in a browser, and stores the refresh token locally.
  * ``sync_google_events(cfg)`` lists events (past week .. next 30 days, with
    recurrences expanded) and returns them as ``{title, start, end}``.

Only ``requests`` is needed. Nothing here decides where tasks go — it only
feeds hard commitments into the planner's event source.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

log = logging.getLogger("butler.gcal")

SCOPES = "openid https://www.googleapis.com/auth/calendar"
AUTH_HOST = "https://oauth2.googleapis.com"
CAL_API = "https://www.googleapis.com/calendar/v3"


class GCalError(Exception):
    pass


def _creds(cfg: Any) -> tuple[str, str]:
    path = cfg.google_calendar_credentials
    if not path or not os.path.exists(path):
        raise GCalError(f"No client credentials at {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for section in ("installed", "web", "desktop"):
        if section in data:
            c = data[section]
            return c["client_id"], c["client_secret"]
    raise GCalError("client_secret.json has no client_id/client_secret")


def _token_path(cfg: Any) -> str:
    return os.path.join(cfg.state_dir, "gcal_token.json")


def _save_token(cfg: Any, token: dict[str, Any]) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    with open(_token_path(cfg), "w", encoding="utf-8") as fh:
        json.dump(token, fh)


def _load_token(cfg: Any) -> dict[str, Any]:
    path = _token_path(cfg)
    if not os.path.exists(path):
        raise GCalError("Not connected. Run `butler calendar connect` first.")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _refresh(cfg: Any, token: dict[str, Any]) -> dict[str, Any]:
    cid, csec = _creds(cfg)
    resp = requests.post(f"{AUTH_HOST}/token", data={
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id": cid, "client_secret": csec,
    }, timeout=30).json()
    if "access_token" not in resp:
        raise GCalError(f"Token refresh failed: {resp.get('error_description', resp)}")
    token.update(access_token=resp["access_token"],
                 expires_at=int(time.time()) + int(resp.get("expires_in", 3600)))
    _save_token(cfg, token)
    return token


def _access(cfg: Any) -> str:
    token = _load_token(cfg)
    if token.get("expires_at", 0) - 120 < int(time.time()):
        token = _refresh(cfg, token)
    return token["access_token"]


def connect(cfg: Any) -> dict[str, Any]:
    """Run interactive device flow. Prints code/URL; blocks until authorised."""
    cid, csec = _creds(cfg)
    code = requests.post(f"{AUTH_HOST}/device/code", data={
        "client_id": cid, "scope": SCOPES,
    }, timeout=30).json()
    if "device_code" not in code:
        raise GCalError(f"Device code request failed: {code}")
    print(f"1. Open: {code['verification_url']}")
    print(f"2. Enter this one-time code: {code['user_code']}")
    print("   Waiting for authorisation in your browser...\n")

    interval = int(code.get("interval", 5))
    deadline = int(time.time()) + int(code.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(max(1, interval))
        try:
            tok = requests.post(f"{AUTH_HOST}/token", data={
                "client_id": cid, "client_secret": csec,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": code["device_code"],
            }, timeout=30).json()
        except requests.RequestException:
            continue
        if tok.get("access_token"):
            token = {
                "access_token": tok["access_token"],
                "refresh_token": tok.get("refresh_token", ""),
                "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
                "client_id": cid, "client_secret": csec,
            }
            if not token["refresh_token"]:
                raise GCalError("No refresh token returned; re-run with full scope.")
            _save_token(cfg, token)
            return {"ok": True, "message": "Connected to Google Calendar."}
        if tok.get("error") == "authorization_pending":
            continue
        if tok.get("error") in ("slow_down",):
            interval += 5
            continue
        if tok.get("error") == "access_denied":
            raise GCalError("Authorisation was denied.")
    raise GCalError("Device flow timed out.")


def _pick_start(e: dict[str, Any]) -> datetime | None:
    s = e.get("start", {})
    if s.get("dateTime"):
        return datetime.fromisoformat(s["dateTime"].replace("Z", "+00:00"))
    if s.get("date"):
        return datetime.fromisoformat(s["date"]).replace(tzinfo=timezone.utc)
    return None


def sync_google_events(cfg: Any) -> list[dict[str, Any]]:
    access = _access(cfg)
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=7)).isoformat()
    time_max = (now + timedelta(days=30)).isoformat()
    params = {
        "timeMin": time_min, "timeMax": time_max,
        "singleEvents": "true", "maxResults": 2500, "orderBy": "startTime",
    }
    resp = requests.get(f"{CAL_API}/calendars/primary/events",
                        headers={"Authorization": f"Bearer {access}"},
                        params=params, timeout=30).json()
    if "items" not in resp:
        raise GCalError(f"Calendar API error: {resp.get('error', resp)}")
    out: list[dict[str, Any]] = []
    for it in resp["items"]:
        start = _pick_start(it)
        if start is None:
            continue
        end = _pick_start({"start": it.get("end", {})}) or start + timedelta(hours=1)
        title = it.get("summary") or "(no title)"
        out.append({
            "title": title, "external_id": it.get("id", ""),
            "start": int(start.timestamp()), "end": int(end.timestamp()),
        })
    return out
