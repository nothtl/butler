"""Acceptance: Telegram/HTTP credential redaction in logs.

Run:  .venv/bin/python tests/run_acceptance_logging_redaction.py

Deterministic and offline. Verifies that the Telegram bot token (and other
credentials) can never reach a log handler in cleartext.
"""

from __future__ import annotations

import io
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.logging import (  # noqa: E402
    ButlerHandler, RedactingFilter, RedactingFormatter, configure_bot_logging,
    redact)

PASS = 0
FAIL = 0

# A synthetic token shaped exactly like a real Telegram bot token.
TOKEN = "1234567890:AAFakeTokenForRedactionTests_0000"
API_KEY = "sk-THIS-IS-A-SYNTHETIC-TEST-KEY-000000000000"


def check(name: str, cond: bool, note: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {note}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {note}")


def test_redact_function() -> None:
    print("\n== redact() ==")
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    out = redact(url)
    check("R1 token removed from a request URL",
          TOKEN not in out and "bot<redacted>" in out, out)
    check("R2 bare token removed", TOKEN not in redact(f"token {TOKEN}"))
    check("R3 token=value removed",
          TOKEN not in redact(f"token={TOKEN}"))
    check("R4 api_key=value removed",
          API_KEY not in redact(f"api_key={API_KEY}"))
    check("R5 Authorization bearer removed",
          TOKEN not in redact(f"Authorization: Bearer {TOKEN}"))
    check("R6 exception text is redacted",
          TOKEN not in redact(f"HTTPError: {url} failed"))
    check("R7 multiple occurrences removed",
          TOKEN not in redact(f"{url} and {TOKEN} and {url}"))
    # no over-redaction of benign text
    check("R8 benign URL unchanged",
          redact("https://example.com/getUpdates")
          == "https://example.com/getUpdates")
    check("R9 ordinary prose unchanged",
          redact("the bot is online and the meeting is at 12:30")
          == "the bot is online and the meeting is at 12:30")
    check("R10 version string unchanged", redact("v1.2.3") == "v1.2.3")
    check("R11 empty input safe", redact("") == "")


def test_filter_and_formatter() -> None:
    print("\n== filter / formatter ==")
    rec = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=1,
        msg="HTTP Request: POST %s", args=(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates",), exc_info=None)
    RedactingFilter().filter(rec)
    check("F1 filter scrubs record args",
          TOKEN not in rec.getMessage())
    rec2 = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"raw {TOKEN}", args=(), exc_info=None)
    RedactingFilter().filter(rec2)
    check("F2 filter scrubs record.msg", TOKEN not in rec2.getMessage())
    fmt = RedactingFormatter("%(message)s")
    rec3 = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"late {TOKEN}", args=(), exc_info=None)
    check("F3 formatter scrubs final output",
          TOKEN not in fmt.format(rec3))
    # exception text (computed at format time) is scrubbed
    try:
        raise RuntimeError(f"boom {TOKEN}")
    except RuntimeError:
        rec4 = logging.LogRecord(
            name="x", level=logging.ERROR, pathname=__file__, lineno=1,
            msg="failed", args=(), exc_info=sys.exc_info())
    check("F4 formatter scrubs tracebacks",
          TOKEN not in fmt.format(rec4))
    buf = io.StringIO()
    h = ButlerHandler(stream=buf)
    h.handle(logging.LogRecord(
        name="butler", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"structured {TOKEN}", args=(), exc_info=None))
    check("F5 ButlerHandler scrubs structured lines",
          TOKEN not in buf.getvalue(), buf.getvalue())
    # percent-formatting must survive redaction of an arg that is a token
    buf2 = io.StringIO()
    h2 = logging.StreamHandler(buf2)
    h2.setFormatter(RedactingFormatter("%(message)s"))
    h2.addFilter(RedactingFilter())
    rec5 = logging.LogRecord(
        name="butler.app", level=logging.ERROR, pathname=__file__, lineno=1,
        msg="upstream token=%s failed", args=(TOKEN,), exc_info=None)
    h2.handle(rec5)
    check("F6 token in a %-arg is redacted without breaking formatting",
          TOKEN not in buf2.getvalue() and "upstream token=" in buf2.getvalue(),
          buf2.getvalue())
    # a token in the msg with no args is redacted by the filter
    rec6 = logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg=f"literal {TOKEN}", args=(), exc_info=None)
    RedactingFilter().filter(rec6)
    check("F7 literal token in msg redacted", TOKEN not in rec6.getMessage())


def test_http_loggers_quieted() -> None:
    print("\n== configure_bot_logging() ==")
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_levels = {n: logging.getLogger(n).level for n in
                    ("httpx", "httpcore", "urllib3", "requests", "telegram")}
    try:
        configure_bot_logging()
        check("C1 httpx quieted above INFO",
              not logging.getLogger("httpx").isEnabledFor(logging.INFO))
        check("C2 httpcore quieted",
              not logging.getLogger("httpcore").isEnabledFor(logging.INFO))
        check("C3 telegram quieted",
              not logging.getLogger("telegram").isEnabledFor(logging.INFO))
        check("C4 requests quieted",
              not logging.getLogger("requests").isEnabledFor(logging.INFO))
        # capture root output and emit an INFO httpx record
        buf = io.StringIO()
        cap = logging.StreamHandler(buf)
        cap.setFormatter(RedactingFormatter("%(message)s"))
        cap.addFilter(RedactingFilter())
        root.addHandler(cap)
        logging.getLogger("httpx").info(
            "HTTP Request: POST %s",
            f"https://api.telegram.org/bot{TOKEN}/getUpdates")
        # bypass the level filter to prove redaction even if it were emitted
        logging.getLogger().info(
            "HTTP Request: POST %s",
            f"https://api.telegram.org/bot{TOKEN}/getUpdates")
        check("C5 no token reaches the handler output",
              TOKEN not in buf.getvalue(), buf.getvalue()[:80])
        check("C6 the emitted line is redacted",
              "bot<redacted>" in buf.getvalue())
        # direct handler on root is a redacting stream handler
        check("C7 root has exactly one configured handler",
              len(root.handlers) == 2)  # configured + our capture
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for n, lv in saved_levels.items():
            logging.getLogger(n).setLevel(lv)


def test_cli_wiring() -> None:
    print("\n== CLI wiring ==")
    import inspect
    from butler import cli as clim
    src = inspect.getsource(clim.dispatch)
    check("W1 bot command uses configure_bot_logging",
          "configure_bot_logging" in src)
    check("W2 raw basicConfig removed from bot command",
          "_logging.basicConfig" not in src)


def main() -> int:
    test_redact_function()
    test_filter_and_formatter()
    test_http_loggers_quieted()
    test_cli_wiring()
    print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
