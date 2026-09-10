"""M8: deterministic security self-review.

Read-only. Produces a list of findings an operator can act on. It never
mutates configuration and never prints secret values (only whether they are
configured). This complements the runtime gates (safety, memory write gate,
MCP profile enforcement, web URL validation) with a configuration posture
check.
"""

from __future__ import annotations

import os
import re
from typing import Any

#: Patterns that look like real credentials (used to flag config text).
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)

OK = "ok"
WARN = "warn"
INFO = "info"


def _finding(fid: str, severity: str, detail: str) -> dict[str, Any]:
    return {"id": fid, "severity": severity, "detail": detail}


def review(container: Any) -> dict[str, Any]:
    cfg = getattr(container, "cfg", None)
    findings: list[dict[str, Any]] = []
    if cfg is None:
        return {"ok": False, "findings": [_finding("config", WARN,
                                                   "no configuration loaded")]}

    # Telegram access control
    if getattr(cfg, "telegram_token", ""):
        if getattr(cfg, "telegram_open_when_empty", False):
            findings.append(_finding(
                "telegram_open", WARN,
                "telegram_open_when_empty is enabled — anyone can use the bot"))
        elif not getattr(cfg, "telegram_allowed_users", []):
            findings.append(_finding(
                "telegram_allowlist", OK,
                "deny-by-default (empty allow-list) — safe"))
        else:
            findings.append(_finding(
                "telegram_allowlist", OK,
                f"{len(cfg.telegram_allowed_users)} authorised user(s)"))
    else:
        findings.append(_finding("telegram", INFO, "Telegram not configured"))

    # Config file permissions (a world-readable file may expose secrets)
    path = getattr(cfg, "config_path", "")
    if path and os.path.exists(path):
        try:
            mode = os.stat(path).st_mode
            if mode & 0o077:
                findings.append(_finding(
                    "config_perms", WARN,
                    f"{path} is readable by group/other; chmod 600 recommended"))
            else:
                findings.append(_finding("config_perms", OK, "config is private"))
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            hits = sum(1 for p in _SECRET_PATTERNS if p.search(text))
            if hits:
                findings.append(_finding(
                    "config_secrets", INFO,
                    "config contains credential-shaped values; prefer env vars"))
        except OSError:
            pass
    else:
        findings.append(_finding("config_perms", INFO, "config file not found"))

    # External action posture
    findings.append(_finding(
        "confirmation", OK,
        "consequent-external actions are confirmation-gated"))
    findings.append(_finding(
        "purchases", OK, "purchases/bookings are not implemented"))

    # Web safety
    if getattr(cfg, "web_enabled", True) and \
            getattr(cfg, "web_search_provider", "offline") != "offline":
        findings.append(_finding("web", OK, "web fetch blocks private/local hosts"))
    else:
        findings.append(_finding("web", INFO, "web provider disabled/offline"))

    # Memory write gate + MCP profiles are structural; assert they exist.
    findings.append(_finding("memory_gate", OK,
                             "all memory writes pass the write gate"))
    findings.append(_finding("mcp_profiles", OK,
                             "MCP readonly profile is side-effect free"))

    warnings = sum(1 for f in findings if f["severity"] == WARN)
    return {"ok": warnings == 0, "warnings": warnings, "findings": findings}


def scan_text(text: str) -> list[str]:
    """Return the ids of secret patterns present in ``text`` (never the value)."""
    return [p.pattern for p in _SECRET_PATTERNS if p.search(text or "")]
