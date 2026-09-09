"""Google Calendar connector (Phase 3.7).

A reliable source of *hard* constraint events for the planner.

It deliberately carries the entire Google surface so it can be exercised
offline: every network call goes through an injectable ``transport`` whose
contract is a pair of methods

    post(url, *, data, headers) -> (status_code, response_json)
    get (url, *, params, headers) -> (status_code, response_json)

``requests`` is the default transport; tests substitute a stub that returns
canned Google responses, so the connector's parsing, token refresh, timezone
math and recurrence handling are all unit-tested with no network and no
``google-api-python-client``.

What it provides:

* ``connect()``        -- OAuth 2.0 *device flow* (works on a headless Pi):
                          prints a URL + one-time code, polls until the user
                          authorises, persists the refresh token locally.
* token persistence & transparent refresh in ``state_dir/gcal_token.json``.
* ``list_events()``    -- list calendar events (past week .. next 30 days)
                          with recurring series expanded into instances, each
                          normalised to an absolute-UTC ``{external_id, title,
                          start_ts, end_ts, all_day, status, ...}`` record using
                          the calendar/event timezone for correct timestamps.

Nothing here decides where tasks go -- it only hands the planner hard
commitments. Errors are typed (``GCalAuthError`` / ``GCalOutage``) so the
planner can degrade gracefully and keep its existing DB state intact.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("butler.gcal")

SCOPES = "openid https://www.googleapis.com/auth/calendar"
AUTH_HOST = "https://oauth2.googleapis.com"
CAL_API = "https://www.googleapis.com/calendar/v3"
FIELD_RE = re.compile(r"\[[^\]]*\]$")

FEED_PAST_DAYS = 7
FEED_FUTURE_DAYS = 30


class GCalError(Exception):
    """Base error: Google Calendar could not be used for this request."""


class GCalAuthError(GCalError):
    """Missing/expired credentials or an authorisation failure."""


class GCalOutage(GCalError):
    """The calendar backend is unavailable (network/HTTP/API problem)."""


def _safe_json(resp: Any) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


class _DefaultTransport:
    """Adapts ``requests`` to the connector's (status, json) contract."""

    def post(self, url: str, data: dict[str, Any] | None = None,
             headers: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        r = requests.post(url, data=data, headers=headers, timeout=30)
        return r.status_code, _safe_json(r)

    def get(self, url: str, params: dict[str, Any] | None = None,
            headers: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        r = requests.get(url, params=params, headers=headers, timeout=30)
        return r.status_code, _safe_json(r)


# --------------------------------------------------------------------- creds
def _creds(cfg: Any) -> tuple[str, str]:
    path = getattr(cfg, "google_calendar_credentials", "")
    if not path or not os.path.exists(path):
        raise GCalAuthError(f"No client credentials at {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for section in ("installed", "web", "desktop"):
        if section in data:
            c = data[section]
            return c["client_id"], c["client_secret"]
    raise GCalAuthError("client_secret.json has no client_id/client_secret")


def _token_path(cfg: Any) -> str:
    return os.path.join(cfg.state_dir, "gcal_token.json")


def _save_token(cfg: Any, token: dict[str, Any]) -> None:
    os.makedirs(cfg.state_dir, exist_ok=True)
    path = _token_path(cfg)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(token, fh)
    os.replace(tmp, path)


def _load_token(cfg: Any) -> dict[str, Any]:
    path = _token_path(cfg)
    if not os.path.exists(path):
        raise GCalAuthError("Not connected. Run `butler calendar connect` first.")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _connected(cfg: Any) -> bool:
    return os.path.exists(_token_path(cfg))


# ------------------------------------------------------------------ parse
def _parse_iso(raw: str) -> datetime | None:
    """``datetime.fromisoformat`` tolerant of Google's Z/[TZ] suffixes."""
    if not raw:
        return None
    s = (raw or "").strip()
    s = FIELD_RE.sub("", s)
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _tz_aware(dt: datetime, tzname: str) -> datetime:
    """Resolve a possibly-naive datetime to absolute UTC."""
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        return dt.astimezone(timezone.utc)
    if tzname:
        try:
            return dt.replace(tzinfo=ZoneInfo(tzname)).astimezone(timezone.utc)
        except Exception:  # noqa: BLE001  (unknown tz string)
            pass
    return dt.replace(tzinfo=timezone.utc)


class GoogleCalendar:
    def __init__(self, cfg: Any, http: Any | None = None):
        self.cfg = cfg
        self.http = http or _DefaultTransport()
        self._client_id, self._client_secret = _creds(cfg)
        self._tz: str | None = None

    # ------------------------------------------------------------- token
    def _load(self) -> dict[str, Any]:
        return _load_token(self.cfg)

    def _save(self, token: dict[str, Any]) -> None:
        _save_token(self.cfg, token)

    def access(self) -> str:
        token = self._load()
        if (token.get("expires_at", 0) - 120 < int(time.time())
                and token.get("refresh_token")):
            token = self.refresh(token)
        tok = token.get("access_token", "")
        if not tok:
            raise GCalAuthError("No access token stored. Re-run `butler calendar connect`.")
        return tok

    def refresh(self, token: dict[str, Any] | None = None) -> dict[str, Any]:
        token = token or self._load()
        code, data = self.http.post(f"{AUTH_HOST}/token", data={
            "grant_type": "refresh_token",
            "refresh_token": token.get("refresh_token") or "",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        })
        if code == 400 or "invalid_grant" in str(data.get("error", "")):
            raise GCalAuthError("Refresh token no longer valid. Re-run "
                                "`butler calendar connect`.")
        if "access_token" not in data:
            raise GCalAuthError(f"Token refresh failed: "
                                f"{data.get('error_description', data)}")
        token.update(
            access_token=data["access_token"],
            expires_at=int(time.time()) + int(data.get("expires_in", 3600)),
        )
        self._save(token)
        return token

    # ----------------------------------------------------------- connect
    def connect(self) -> dict[str, Any]:
        """Interactive device flow. Prints URL/code; blocks until authorised."""
        code_http = self.http.post(f"{AUTH_HOST}/device/code", data={
            "client_id": self._client_id, "scope": SCOPES,
        })
        coderesp = code_http[1] if isinstance(code_http, tuple) else code_http
        if "device_code" not in coderesp:
            raise GCalError(f"Device code request failed: {coderesp}")
        print(f"1. Open: {coderesp['verification_url']}")
        print(f"2. Enter this one-time code: {coderesp['user_code']}")
        print("   Waiting for authorisation in your browser...\n")

        interval = int(coderesp.get("interval", 5))
        deadline = int(time.time()) + int(coderesp.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(max(1, interval))
            try:
                raw = self.http.post(f"{AUTH_HOST}/token", data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": coderesp["device_code"],
                })
            except GCalError:
                continue
            tok = raw[1] if isinstance(raw, tuple) else raw
            if tok.get("access_token"):
                token = {
                    "access_token": tok["access_token"],
                    "refresh_token": tok.get("refresh_token", ""),
                    "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)),
                }
                if not token["refresh_token"]:
                    raise GCalAuthError("No refresh token returned; re-run with full scope.")
                self._save(token)
                return {"ok": True, "message": "Connected to Google Calendar."}
            if tok.get("error") == "authorization_pending":
                continue
            if tok.get("error") == "slow_down":
                interval += min(5, interval)
                continue
            if tok.get("error") == "access_denied":
                raise GCalAuthError("Authorisation was denied.")
        raise GCalError("Device flow timed out.")

    # ---------------------------------------------------------- timezone
    def _cal_timezone(self) -> str:
        if self._tz is not None:
            return self._tz
        code, data = self.http.get(f"{CAL_API}/calendars/primary",
                                   headers=self._auth())
        self._tz = (data.get("timeZone") or "UTC") if code == 200 else "UTC"
        return self._tz

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access()}"}

    def _timezone_for(self, start: dict[str, Any], end: dict[str, Any]) -> str:
        return start.get("timeZone") or end.get("timeZone") or self._cal_timezone()

    # ---------------------------------------------------------- normalize
    def _normalize(self, it: dict[str, Any]) -> dict[str, Any] | None:
        start = it.get("start", {})
        end = it.get("end", {})
        tz = self._timezone_for(start, end)
        all_day = 0
        if "date" in start:
            all_day = 1
            s = _parse_iso(start.get("date"))
            if s is None:
                return None
            sl = _tz_aware(s, tz)
            e = _parse_iso(end.get("date")) if end.get("date") else None
            el = _tz_aware(e, tz) if e else sl + timedelta(days=1)
        else:
            s = _parse_iso(start.get("dateTime"))
            if s is None:
                return None
            sl = _tz_aware(s, tz)
            e = _parse_iso(end.get("dateTime")) if end.get("dateTime") else None
            el = _tz_aware(e, tz) if e else sl + timedelta(hours=1)
        if el <= sl:
            el = sl + timedelta(hours=1)
        return {
            "external_id": it.get("id", ""),
            "recurring_id": it.get("recurringEventId", ""),
            "title": it.get("summary") or "(no title)",
            "all_day": all_day,
            "start_ts": int(sl.timestamp()),
            "end_ts": int(el.timestamp()),
            "location": it.get("location") or "",
            "status": it.get("status", "confirmed"),
            "timezone": tz,
        }

    # ------------------------------------------------------------- events
    def list_events(self) -> list[dict[str, Any]]:
        access = self.access()
        now = datetime.now(timezone.utc)
        params = {
            "timeMin": (now - timedelta(days=FEED_PAST_DAYS)).isoformat(),
            "timeMax": (now + timedelta(days=FEED_FUTURE_DAYS)).isoformat(),
            "singleEvents": "true",
            "maxResults": 2500,
            "orderBy": "startTime",
            "showDeleted": "false",
        }
        code, data = self.http.get(f"{CAL_API}/calendars/primary/events",
                                   params=params,
                                   headers={"Authorization": f"Bearer {access}"})
        if code in (401, 403):
            raise GCalAuthError(f"Google Calendar authorisation failed (HTTP {code}). "
                                "Re-run `butler calendar connect`.")
        if code != 200 or "items" not in data:
            err = data.get("error", data) if isinstance(data, dict) else data
            raise GCalOutage(f"Calendar API unavailable (HTTP {code}): {err}")
        out: list[dict[str, Any]] = []
        for it in data["items"]:
            n = self._normalize(it)
            if n and n["external_id"]:
                out.append(n)
        return out

    def status(self) -> dict[str, Any]:
        try:
            self.access()
        except GCalError as exc:
            return {"ok": False, "connected": False, "error": str(exc)}
        return {"ok": True, "connected": True}


# ------------------------------------------------------- module-level API
def connect(cfg: Any, http: Any | None = None) -> dict[str, Any]:
    return GoogleCalendar(cfg, http).connect()


def sync_google_events(cfg: Any, http: Any | None = None) -> list[dict[str, Any]]:
    return GoogleCalendar(cfg, http).list_events()
