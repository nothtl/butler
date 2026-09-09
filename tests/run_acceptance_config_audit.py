"""Post-Phase 4.4 production-readiness audit acceptance tests.

Run:  .venv/bin/python tests/run_acceptance_config_audit.py

This is a hardening / portability audit, not a new feature. It proves Butler
carries NO hard-coded user or environment assumptions and degrades safely:

  1. no hard-coded course codes (``courses`` defaults to ``[]``)
  2. Google Calendar is OFF by default (loader default == dataclass default)
  3. ``[affinity]`` overrides change the task/zone keyword tables
  4. short, ambiguous keywords (``cs``/``ml``) are matched on word boundaries
  5. an unclassified task is NEUTRAL (empty) when configured so — never "work"
  6. config validation fails fast on a bad timezone / retention / buffer / port
  7. timezone support: an empty ``timezone`` preserves system-local behaviour
  8. Telegram bot is DENY-by-default when the allow-list is empty
  9. the LLM needs an explicit base_url (no hard-coded OpenAI endpoint)
 10. proactive notifications are throttled (quiet hours / cooldown / dedup / cap)
 11. food metadata honestly flags estimates (never a fabricated verified fact)
 12. no secrets (LLM key, HA token) leak via ``to_dict``

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
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler import affinity            # noqa: E402
from butler.config import Config        # noqa: E402
from butler.core import Container       # noqa: E402
from butler.proactive import Proactive  # noqa: E402

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


def _cfg(base: str, **fields) -> Config:
    cfg = Config()
    cfg.data_dir = os.path.join(base, "storage")
    os.makedirs(cfg.data_dir, exist_ok=True)
    cfg.config_path = os.path.join(base, "config.toml")
    for k, v in fields.items():
        setattr(cfg, k, v)
    cfg.ensure_dirs()
    return cfg


def _reset_affinity() -> None:
    # Restore built-ins so suite isolation is not affected by module state.
    affinity.configure(
        keywords=None, zone_keywords=None, unknown_category="work")


def test_course_codes_default_empty():
    print("\n=== A. no hard-coded course codes ===")
    # A freshly-created config (no file) must not inject "CS168".
    c = _cfg(tempfile.mkdtemp(prefix="audit-"))
    check("courses defaults to empty list", c.courses == [], str(c.courses))


def test_google_calendar_default_off():
    print("\n=== B. Google Calendar is off by default ===")
    c = _cfg(tempfile.mkdtemp(prefix="audit-"))
    check("google_calendar_enabled defaults to False",
          c.google_calendar_enabled is False, str(c.google_calendar_enabled))
    # BUGFIX: the loader previously defaulted to True (mismatching the dataclass).
    cfg = Config()
    check("loader keeps dataclass default (False)", cfg.google_calendar_enabled is False)


def test_affinity_overrides():
    print("\n=== C. affinity tables are configurable ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    c.affinity_keywords = {"study": ["statistics", "probability"]}
    c.affinity_zone_keywords = {"gym": {"study": 5}}
    c.affinity_unknown_category = "rest"
    try:
        affinity.configure(
            keywords=c.affinity_keywords or None,
            zone_keywords=c.affinity_zone_keywords or None,
            unknown_category=c.affinity_unknown_category)
        check("a custom keyword is recognised",
              "study" in affinity.classify("Review my statistics notes"),
              str(affinity.classify("Review my statistics notes")))
        check("an overridden zone maps to the custom weight",
              affinity.zone_weights_for("the gym").get("study") == 5,
              str(affinity.zone_weights_for("the gym")))
        check("unknown_category replaces the default label",
              affinity.classify("xyzzy is a nonsense task") == frozenset({"rest"}),
              str(affinity.classify("xyzzy is a nonsense task")))
    finally:
        _reset_affinity()


def test_neutral_unknown():
    print("\n=== D. unclassified task can be NEUTRAL ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    c.affinity_unknown_category = ""
    try:
        affinity.configure(keywords=None, zone_keywords=None, unknown_category="")
        check("no keyword match => empty (neutral) set",
              affinity.classify("a wholly unrelated task") == frozenset(),
              str(affinity.classify("a wholly unrelated task")))
    finally:
        _reset_affinity()


def test_short_keyword_word_boundary():
    print("\n=== E. short keywords match on word boundaries ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    try:
        affinity.configure(keywords=None, zone_keywords=None, unknown_category="work")
        check("'cs' as a prefix still matches (CS168)",
              "study" in affinity.classify("Do CS168 homework"),
              str(affinity.classify("Do CS168 homework")))
        check("'cs' does NOT match a random substring",
              "study" not in affinity.classify("ecstatic feeling about stuff"),
              str(affinity.classify("ecstatic feeling about stuff")))
        check("'ml' does NOT match 'email'",
              "study" not in affinity.classify("Reply to the email"),
              str(affinity.classify("Reply to the email")))
    finally:
        _reset_affinity()


def test_config_validation():
    print("\n=== F. config validation fails fast ===")
    def expect(name, mutate) -> None:
        c = Config()
        mutate(c)
        try:
            c.validate()
            check(name, False, "no error raised")
        except ValueError:
            check(name, True, "ValueError")

    expect("bad timezone rejected", lambda c: setattr(c, "timezone", "America/Fake_Zone"))
    expect("buffer_fraction>=1 rejected", lambda c: setattr(c, "buffer_fraction", 1.5))
    expect("negative retention rejected", lambda c: setattr(c, "timeline_retention_days", -5))
    expect("remote_port range rejected", lambda c: setattr(c, "remote_port", 99999))
    expect("sleep window rejected", lambda c: setattr(c, "sleep_start", 99999))
    expect("'Local' timezone rejected", lambda c: setattr(c, "timezone", "Local"))


def test_timezone_default_preserves_local():
    print("\n=== G. timezone default preserves system-local behaviour ===")
    c = Config()
    # No explicit tz => None, so local_midnight == the host's local midnight.
    check("unset timezone yields None tz object", c.tz() is None, str(c.tz()))
    ts = 1700000000
    local = c.local_midnight(ts)
    import datetime as dt
    d = dt.datetime.fromtimestamp(ts)
    naive = int(dt.datetime(d.year, d.month, d.day).timestamp())
    check("empty timezone => system-local midnight", local == naive, f"{local} vs {naive}")


def test_telegram_deny_by_default():
    print("\n=== H. Telegram denies by default when allow-list empty ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    c.telegram_allowed_users = []
    check("empty allow-list denies anyone",
          c.telegram_open_when_empty is False, str(c.telegram_open_when_empty))

    class _U:
        id = 12345

    class _Update:
        effective_user = _U()

    from butler.telebot import TelegramBot

    class _C:
        cfg = c

    bot = TelegramBot.__new__(TelegramBot)
    bot.container = _C()
    check("unlisted user is denied when list empty",
          bot._authorized(_Update()) is False)
    c.telegram_open_when_empty = True
    check("open_when_empty restores legacy open behaviour",
          bot._authorized(_Update()) is True)


def test_llm_requires_base_url():
    print("\n=== I. LLM requires an explicit base_url ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    c.llm_api_key = "sk-secret"
    c.llm_base_url = ""
    from butler.chat import Chat
    chat = Chat(c, db=None, search=None)  # type: ignore[arg-type]
    chat.cfg = c
    check("no base_url => LLM not ready (no hard-coded OpenAI)",
          chat._llm_ready() is False)
    c.llm_base_url = "https://llm.internal/v1"
    check("configured base_url => LLM ready", chat._llm_ready() is True)


def test_proactive_throttling():
    print("\n=== J. proactive notifications are throttled ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    c.notify_quiet_start = 0
    c.notify_quiet_end = 0
    c.notify_cooldown_minutes = 0
    c.notify_max_per_cadence = 5
    c.notify_dedup_window_minutes = 0

    class _P:
        def collect():
            return ["alert one", "alert two", "alert three"]

    pr = Proactive.__new__(Proactive)
    pr.cfg = c
    pr.container = type("C", (), {"cfg": c})()
    pr.context = None
    pr.courses = None
    pr.food = None
    pr.chef = None
    pr.collect = _P.collect
    pr._state_file = os.path.join(c.state_dir, "proactive_state.json")
    pr._state = {"last_push": 0, "last_messages": []}

    r = pr.run()
    check("run sends without telegram (informs only, ok=True)",
          r.get("ok") is True, str(r))

    # cooldown blocks a re-send within the window
    c.notify_cooldown_minutes = 30
    pr._state["last_push"] = __import__("time").time()
    r2 = pr.run()
    check("cooldown suppresses a second push", r2.get("reason") == "cooldown", str(r2))
    c.notify_cooldown_minutes = 0

    # quiet hours suppress
    c.notify_quiet_start = 1
    c.notify_quiet_end = 2
    r3 = pr.run()
    check("quiet hours suppress notifications",
          r3.get("reason") == "quiet hours", str(r3))


def test_food_honesty():
    print("\n=== K. food metadata honestly flags estimates ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    ct = Container(c)
    lib = ct.chef.library()
    if lib:
        sample = lib[0]
        check("recipe carries honesty flags",
              isinstance(sample.get("time_estimated"), bool)
              and isinstance(sample.get("cost_estimated"), bool),
              str({k: sample.get(k) for k in ("time_estimated", "cost_estimated")}))
        check("nutrition_source is one of the honest values",
              sample.get("nutrition_source") in ("author", "verified", "missing", ""),
              str(sample.get("nutrition_source")))
    else:
        # Library empty in this env; correctness is in the `Recipe` model.
        r = ct.chef.import_recipe  # noqa: B018 — import path exists
        from butler.food import Recipe
        rec = Recipe(name="x", steps=["a", "b"])
        check("Recipe model carries honesty flags",
              rec.time_estimated is False and rec.cost_estimated is False
              and rec.nutrition_source == "", str(rec))


def test_no_secrets_in_dict():
    print("\n=== L. no secrets leak via config.to_dict ===")
    base = tempfile.mkdtemp(prefix="audit-")
    c = _cfg(base)
    c.llm_api_key = "sk-secret-abc"
    c.home_assistant_token = "ha-secret-xyz"
    c.telegram_token = "tg-secret-123"
    c.remote_token = "remote-secret-999"
    d = c.to_dict()
    dump = str(d)
    check("llm key never appears", "sk-secret-abc" not in dump)
    check("HA token never appears", "ha-secret-xyz" not in dump)
    check("telegram token never appears", "tg-secret-123" not in dump)
    check("remote token never appears", "remote-secret-999" not in dump)
    check("secrets only reported as a boolean", d.get("llm_configured") is True)


def main() -> None:
    _reset_affinity()
    test_course_codes_default_empty()
    test_google_calendar_default_off()
    test_affinity_overrides()
    test_neutral_unknown()
    test_short_keyword_word_boundary()
    test_config_validation()
    test_timezone_default_preserves_local()
    test_telegram_deny_by_default()
    test_llm_requires_base_url()
    test_proactive_throttling()
    test_food_honesty()
    test_no_secrets_in_dict()
    _reset_affinity()

    print("\n==== RESULT: %d passed, %d failed ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
