"""Phase 4.1 acceptance tests for Butler Home Assistant presence.

Run:  .venv/bin/python tests/run_acceptance_p41.py

Proves Home Assistant is a reliable source of zone-level presence:
  1. the client connects and sends the access token as a Bearer header
  2. zone presence is parsed from a device_tracker (zone/status)
  3. battery is reported alongside presence
  4. an unknown/unavailable device degrades to \"unknown\" (still a full dict)
  5. zone-level presence is preferred over a raw GPS coordinate
  6. a HA outage degrades to unknown without crashing
  7. an authorisation failure raises HAAuthError at the connector and degrades
  8. the context engine snapshot exposes a 'presence' block
  9. \"where am i\" resolves to a presence reply
 10. no location is fabricated when HA is unavailable/disabled
 11. the scheduler/planner are unaffected by HA presence or its failure
 12. the access token never leaks through to_dict / presence output

Run everything:
  .venv/bin/python tests/test_schedule.py
  .venv/bin/python tests/run_acceptance.py
  .venv/bin/python tests/run_acceptance_p3.py
  .venv/bin/python tests/run_acceptance_p35.py
  .venv/bin/python tests/run_acceptance_p36.py
  .venv/bin/python tests/run_acceptance_p37.py
  .venv/bin/python tests/run_acceptance_p41.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.config import Config  # noqa: E402
from butler.core import Container  # noqa: E402
from butler.context import presence_battery  # noqa: E402
from butler.house import HAAuthError, HAOutage, HomeAssistant  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def _tracker(eid: str, state: str, **attrs) -> dict:
    return {"entity_id": eid, "state": state, "attributes": attrs}


class FakeHA:
    """Deterministic (status, json) HTTP double for the connector."""

    def __init__(self, states=None, status: int = 200):
        self._states = states if states is not None else []
        self._status = status
        self.requests: list[dict] = []

    def get(self, url: str, headers=None) -> tuple[int, object]:
        self.requests.append({"url": url, "headers": headers})
        return self._status, self._states


class RaisingHA(HomeAssistant):
    def states(self):
        raise HAOutage("backend down")


def _cfg(base: str, enabled: bool = True, url: str = "http://ha.local:8123",
         token: str = "secret-token") -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.home_assistant_enabled = enabled
    cfg.home_assistant_url = url
    cfg.home_assistant_token = token
    cfg.ensure_dirs()
    return cfg


def main() -> int:
    base = tempfile.mkdtemp(prefix="butler-p41-")
    try:
        # ============ 4.1.1 Connector connects, sends Bearer token ==========
        print("\n=== 4.1.1 Connector + Bearer auth ===")
        cfg = _cfg(base)
        transport = FakeHA(states=[_tracker("device_tracker.phone", "home", battery=85)])
        ha = HomeAssistant(cfg, http=transport)
        seen = ha.states()
        check("client fetches and returns HA states",
              len(seen) == 1 and seen[0]["entity_id"] == "device_tracker.phone",
              str(seen[0].get("entity_id")))
        auth = (transport.requests[-1]["headers"] or {}).get("Authorization", "")
        check("access token sent as a Bearer header", auth == "Bearer secret-token", auth)

        # ============ 4.1.2 Zone presence parsed ============
        print("\n=== 4.1.2 Zone presence ===")
        lib = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "Library", location_name="Library",
                     battery=72, source_type="gps")]))
        p = lib.presence()
        check("zone parsed from the device state/location_name",
              p.get("zone") == "Library" and p.get("status") == "away", str(p))
        check("presence records a known location", p.get("known") is True)

        # home presence
        home = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "home", location_name="Home")]))
        ph = home.presence()
        check("home state resolves to status=home",
              ph.get("status") == "home" and ph.get("zone") in ("home", "Home"),
              str(ph))

        # ============ 4.1.3 Battery reported ============
        print("\n=== 4.1.3 Battery ===")
        p = lib.presence()
        check("battery % is reported alongside presence",
              p.get("battery") == 72 and "72" in presence_battery(p),
              f"battery={p.get('battery')}")
        no_bat = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "Library", location_name="Library")]))
        check("battery omitted when no battery attribute", no_bat.presence().get("battery") is None)

        # ============ 4.1.4 Unknown / unavailable degrade ============
        print("\n=== 4.1.4 Unknown / unavailable ===")
        unk = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "unknown")]))
        pu = unk.presence()
        check("unknown state -> status unknown, full dict",
              pu.get("status") == "unknown" and pu.get("known") is False
              and "known" in pu and "zone" in pu, str(pu))
        check("unknown state does not fabricate a zone",
              pu.get("zone") in ("", None), str(pu.get("zone")))
        unavail = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.phone", "unavailable", battery=90)]))
        check("unavailable device -> status unknown", unavail.presence().get("status") == "unknown")

        # ============ 4.1.5 Zone over raw GPS ============
        print("\n=== 4.1.5 Zone-level preferred over GPS ===")
        gps = HomeAssistant(cfg, http=FakeHA(states=[
            _tracker("device_tracker.gps", "33.693,-75.123", source_type="gps"),
            _tracker("device_tracker.phone", "Library", location_name="Library",
                     battery=72)]))
        pg = gps.presence()
        check("zone device chosen over a raw GPS coordinate",
              pg.get("zone") == "Library" and pg.get("status") == "away", str(pg))

        # ============ 4.1.6 Outage degrades without crashing ============
        print("\n=== 4.1.6 Outage ===")
        outage = HomeAssistant(cfg, http=FakeHA(status=503, states={"error": "down"}))
        try:
            outage.states()
            raised = False
        except HAOutage:
            raised = True
        check("connector raises HAOutage on a 503", raised)
        po = outage.presence()
        check("presence degrades to unknown on outage",
              po.get("status") == "unknown" and po.get("known") is False, str(po))

        # ============ 4.1.7 Authorisation failure ============
        print("\n=== 4.1.7 Auth failure ===")
        auth_broken = HomeAssistant(cfg, http=FakeHA(status=401, states={"error": "unauthorized"}))
        try:
            auth_broken.states()
            auth_raised = False
        except HAAuthError:
            auth_raised = True
        check("connector raises HAAuthError on a 401", auth_raised)
        pa = auth_broken.presence()
        check("presence degrades to unknown on auth failure",
              pa.get("status") == "unknown" and pa.get("known") is False, str(pa))

        # ============ 4.1.8 Context snapshot exposes presence ============
        print("\n=== 4.1.8 Context snapshot has presence ===")
        ictr = Container(_cfg(base))
        ictr.ha = HomeAssistant(_cfg(base), http=FakeHA(states=[
            _tracker("device_tracker.phone", "Library", location_name="Library",
                     battery=72)]))
        snap = ictr.context.snapshot()
        check("snapshot returns a presence block", "presence" in snap, str(list(snap)))
        sp = snap["presence"]
        check("snapshot presence carries zone/status/battery",
              sp.get("zone") == "Library" and sp.get("status") == "away"
              and sp.get("battery") == 72, str(sp))

        # ============ 4.1.9 \"where am i\" resolves to presence ============
        print("\n=== 4.1.9 Where am I intent ===")
        intent = ictr.decider.parse("where am i")
        check("parse returns where_am_i intent", intent.kind == "where_am_i", intent.kind)
        res = ictr.decider.resolve(intent)
        check("resolve returns a presence payload", res.get("kind") == "where_am_i"
              and "presence" in res, str(res.get("kind")))
        check("reply names a real location (Library)", "Library" in res["presence"].get("zone", ""))
        a_intent = ictr.decider.parse("what's around me")
        check("parse returns around_me intent", a_intent.kind == "around_me", a_intent.kind)

        # ============ 4.1.10 No fabricated location ============
        print("\n=== 4.1.10 No fabricated location ===")
        big = Container(_cfg(base))  # no stub: default real transport, but token/enabled?
        # Force HA to be unavailable: clone cfg with no token + disabled, no stub.
        dead = Container(_cfg(base, enabled=False, token=""))
        dsnap = dead.context.snapshot()
        dp = dsnap["presence"]
        check("unconfigured HA -> presence unknown", dp.get("status") == "unknown"
              and dp.get("zone") in ("", None), str(dp))
        # The natural reply must not invent a place.
        from butler.context import describe_location
        reply = describe_location(dp)
        check("reply does not fabricate a location",
              "can't tell" in reply.lower() and "Library" not in reply
              and "home" not in reply.lower(), reply)

        # ============ 4.1.11 Scheduler / planner unaffected ============
        print("\n=== 4.1.11 Scheduler unaffected by presence ===")
        sctr = Container(_cfg(base))
        sctr.ha = RaisingHA(sctr.cfg)  # presence is always unknown/offline
        sctr.planner.add_task("Deep work", est_minutes=60, deadline=0)
        day_before = sctr.planner.plan_day()
        check("planning still succeeds with an offline HA",
              day_before.get("ok") is True or "slots" in day_before, str(day_before))
        # presence failure must not break a snapshot either.
        s_snap = sctr.context.snapshot()
        check("snapshot survives an HA outage (presence unknown)",
              s_snap["presence"].get("status") == "unknown", str(s_snap["presence"]))
        check("snapshot still reports free time on HA failure",
              isinstance(s_snap.get("free_minutes_today"), int))

        # ============ 4.1.12 No secret leak ============
        print("\n=== 4.1.12 No token leak ===")
        cfgd = _cfg(base)
        dd = cfgd.to_dict()
        check("to_dict never exposes the HA token",
              "secret-token" not in str(dd) and dd.get("home_assistant_configured") is True)
        leakha = HomeAssistant(cfgd, http=FakeHA(states=[
            _tracker("device_tracker.phone", "Library", location_name="Library", battery=72)]))
        leak_out = str(leakha.presence()) + str(leakha.states())
        check("presence/states output never carries the token",
              "secret-token" not in leak_out)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
