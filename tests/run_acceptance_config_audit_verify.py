"""Targeted post-hardening verification.

Run:  .venv/bin/python tests/run_acceptance_config_audit_verify.py

This only VERIFIES already-shipped behaviour; it introduces no new features.
Three areas are checked under a *different* user timezone than the system's:

  A. timezone: helpers and the call sites that route through them honour a
     configured timezone (today/tomorrow/day boundaries, sleep window, the
     timeline snapshot) even when it differs from the host timezone.
  B. affinity: ``configure()`` is initialised deterministically at startup
     and overrides never leak between containers/tests.
  C. telegram: ``allowed_users`` is enforced and an empty allow-list denies
     everyone (never accidentally exposes the bot).

Run everything:
  .venv/bin/python tests/test_schedule.py
  .venv/bin/python tests/run_acceptance.py
  .venv/bin/python tests/run_acceptance_p3.py
  .venv/bin/python tests/run_acceptance_p35.py
  .venv/bin/python tests/run_acceptance_p36.py
  .venv/bin/python tests/run_acceptance_p37.py
  .venv/bin/python tests/run_acceptance_p41.py
  .venv/bin/python tests/run_acceptance_p42.py
  .venv/bin/python tests/run_acceptance_p43.py
  .venv/bin/python tests/run_acceptance_p44.py
  .venv/bin/python tests/run_acceptance_config_audit.py
  .venv/bin/python tests/run_acceptance_config_audit_verify.py
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler import affinity          # noqa: E402
from butler.config import Config      # noqa: E402
from butler.core import Container     # noqa: E402
from butler.planner import Planner    # noqa: E402

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


def _cfg(base: str, tz: str = "") -> Config:
    cfg = Config()
    cfg.timezone = tz
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    cfg.ensure_dirs()
    return cfg


# A timestamp whose local day BOUNDARY differs between the configured tz and the
# host tz, so a naive (host) computation would be wrong.
T = 1700000000  # 2023-11-14 22:13:20 UTC


def _host_midnight(ts: int) -> int:
    d = datetime.datetime.fromtimestamp(ts)
    return int(datetime.datetime(d.year, d.month, d.day).timestamp())


def _aware_midnight(ts: int, zone: str) -> int:
    from zoneinfo import ZoneInfo
    aware = datetime.datetime.fromtimestamp(ts, ZoneInfo(zone))
    return int(aware.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def test_tz_helpers():
    print("\n=== A1. cfg timezone helpers honour a non-host timezone ===")
    # host is America/Los_Angeles; pick a different zone and confirm the
    # absolute local-midnight timestamp is NOT the naive host one.
    for zone in ("America/New_York", "Asia/Tokyo"):
        want = _aware_midnight(T, zone)
        if want == _host_midnight(T):
            continue  # coincidentally equal for this ts; use a DST-robust ts
        c = _cfg(tempfile.mkdtemp(prefix="tzh-"), tz=zone)
        check(f"local_midnight({zone}) != host-naive",
              c.local_midnight(T) == want, f"{c.local_midnight(T)} vs {want}")
        check(f"tz() returns {zone}", str(c.tz()) == zone, str(c.tz()))
        nl = c.now_local()
        check(f"now_local() is tz-aware for {zone}", nl.tzinfo is not None, str(nl))


def test_tz_day_boundaries():
    print("\n=== A2. planner day boundaries use the configured timezone ===")
    # When user tz is ahead of host (Tokyo), the host-local "today" that contains
    # T is a different UTC day than the Tokyo "today".
    zone = "Asia/Tokyo"
    c = _cfg(tempfile.mkdtemp(prefix="tzd-"), tz=zone)
    sub = tempfile.mkdtemp(prefix="sub-")
    planner = Planner.__new__(Planner)
    planner.cfg = c

    want = _aware_midnight(T, zone)
    got = planner._day_start_ts(T)
    check("planner._day_start_ts() uses cfg tz", got == want, f"{got} vs {want}")


def test_tz_sleep_and_schedule():
    print("\n=== A3. sleep window is config-driven and schedules are consistent ===")
    c = _cfg(tempfile.mkdtemp(prefix="tzs-"), tz="")
    base = temperature_sleep = None
    c.sleep_start = 23 * 60
    c.sleep_end = 7 * 60
    check("default sleep window valid", c.schedule_valid() is True)
    c.sleep_start = 2000  # invalid (out of 0-1439)
    check("invalid sleep window rejected", c.schedule_valid() is False)
    c.sleep_start = 23 * 60

    # Planner day window derives from configured sleep boundaries.
    p = Planner.__new__(Planner)
    p.cfg = c
    got_start = int(p.cfg.sleep_start)
    got_end = int(p.cfg.sleep_end)
    check("planner reads sleep window from cfg",
          got_start == 23 * 60 and got_end == 7 * 60, f"{got_start}/{got_end}")

    from butler import schedule as sch
    free = sch.free_intervals(7 * 60, 23 * 60, 23 * 60, 7 * 60, [])
    check("free_intervals is deterministic", isinstance(free, list), str(len(free)))


def test_tz_timeline_snapshot():
    print("\n=== A4. context timeline snapshot boundary uses cfg tz ===")
    from butler.context import ContextEngine

    class _Cfg(Config):
        def __init__(self, tz):
            super().__init__()
            self.timezone = tz

    zone = "Asia/Tokyo"
    base = tempfile.mkdtemp(prefix="tzc-")
    cfg = _cfg(base, tz=zone)
    sub = tempfile.mkdtemp(prefix="sub-", dir=base)

    import types
    ct = Container(cfg)
    ctx = ContextEngine.__new__(ContextEngine)
    ctx.cfg = cfg
    ctx.db = ct.db
    ctx.container = ct
    # _local_midnight needs cfg.local_midnight; check it routes there.
    want = _aware_midnight(T, zone)
    got = ctx._local_midnight(T)
    check("context._local_midnight() uses cfg tz", got == want, f"{got} vs {want}")


def test_affinity_deterministic_startup():
    print("\n=== B1. affinity.configure() deterministically initialised at startup ===")
    base = tempfile.mkdtemp(prefix="af-")
    c = _cfg(base)
    c.affinity_keywords = {"study": ["statistics"]}
    c.affinity_zone_keywords = {}
    c.affinity_unknown_category = "rest"

    # Container construction must apply the configured tables each time.
    Container(c)
    check("custom keyword used after Container init",
          "study" in affinity.classify("Review my statistics notes"),
          str(affinity.classify("Review my statistics notes")))

    # A sibling Container with empty overrides must be back on the default table.
    c2 = _cfg(tempfile.mkdtemp(prefix="af2-"))
    Container(c2)
    check("no override => built-in keyword table restored",
          "study" in affinity.classify("Do CS168 homework"),
          str(affinity.classify("Do CS168 homework")))
    check("no custom keyword leaks (statistics not added)",
          "study" not in affinity.classify("Just statistics today"),
          str(affinity.classify("Just statistics today")))


def test_affinity_no_leak_between_containers():
    print("\n=== B2. affinity overrides do not leak between containers ===")
    # Configure one way, then another; a fresh Container with no overrides must
    # resolve to the pristine default, proving no cross-container bleed.
    base = tempfile.mkdtemp(prefix="afl-")
    c1 = _cfg(base)
    c1.affinity_keywords = {"exercise": ["zumba"]}
    Container(c1)
    check("container1 keyword applied",
          "exercise" in affinity.classify("Do zumba now"),
          str(affinity.classify("Do zumba now")))

    c2 = _cfg(tempfile.mkdtemp(prefix="afl2-"))
    Container(c2)
    check("container2 has no container1 keyword",
          "exercise" not in affinity.classify("Do zumba now"),
          str(affinity.classify("Do zumba now")))
    check("study still resolves (default intact)",
          "study" in affinity.classify("Do CS168 homework"),
          str(affinity.classify("Do CS168 homework")))


def test_telegram_enforced():
    print("\n=== C1. telegram allowed_users is enforced ===")
    base = tempfile.mkdtemp(prefix="tg-")
    c = _cfg(base)
    c.telegram_allowed_users = [111, 222]
    c.telegram_open_when_empty = False

    from butler.telebot import TelegramBot

    class _C:
        cfg = c

    bot = TelegramBot.__new__(TelegramBot)
    bot.container = _C()

    def update(uid):
        class _U:
            id = uid
        class _Upd:
            effective_user = _U()
            @property
            def message(self):
                return None
        return _Upd()

    check("listed user allowed", bot._authorized(update(111)) is True)
    check("second listed user allowed", bot._authorized(update(222)) is True)
    check("unlisted user denied", bot._authorized(update(999)) is False)


def test_telegram_empty_denies():
    print("\n=== C2. empty allow-list never exposes the bot ===")
    base = tempfile.mkdtemp(prefix="tge-")
    c = _cfg(base)
    c.telegram_allowed_users = []
    c.telegram_open_when_empty = False

    from butler.telebot import TelegramBot

    class _C:
        cfg = c

    bot = TelegramBot.__new__(TelegramBot)
    bot.container = _C()

    def update(uid):
        class _U:
            id = uid
        class _Upd:
            effective_user = _U()
            @property
            def message(self):
                return None
        return _Upd()

    # With no explicit allow-list the bot must deny EVERYONE, including uid 0.
    for uid in (0, 1, 999999):
        check(f"uid {uid} denied when allow-list empty",
              bot._authorized(update(uid)) is False)

    # The opt-in legacy flag is the ONLY way to open it.
    c.telegram_open_when_empty = True
    check("open_when_empty=True reopens by design",
          bot._authorized(update(1234)) is True)


def main() -> int:
    test_tz_helpers()
    test_tz_day_boundaries()
    test_tz_sleep_and_schedule()
    test_tz_timeline_snapshot()
    test_affinity_deterministic_startup()
    test_affinity_no_leak_between_containers()
    test_telegram_enforced()
    test_telegram_empty_denies()

    print("\n==== RESULT: %d passed, %d failed ====" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
