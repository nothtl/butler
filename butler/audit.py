"""Phase 6: durable structured audit log.

The single, deterministic record of what Butler did — and, critically, what it
*did not* do. The LLM is never an actor on an external effect: it proposes, the
:class:`SafetyPolicy` classifies, and this module records the decision. A retry
shares a ``run_id`` so the trail is replayable and non-duplicated.

Privacy rule: ``detail`` is JSON but must never contain credentials or tokens
(the :func:`redact` helper strips known secret keys before persisting).
"""

from __future__ import annotations

import json
import time
from typing import Any

from .db import DB

SECRET_KEYS = {
    "api_key", "apikey", "token", "secret", "password", "passwd",
    "credential", "authorization", "access_token", "refresh_token",
    "client_secret", "auth", "key",
}


def redact(obj: Any) -> Any:
    """Recursively strip values whose key looks like a secret. Never raises."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in SECRET_KEYS:
                out[str(k)] = "[REDACTED]"
            else:
                out[str(k)] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        # Never persist raw bearer/token-shaped strings.
        if any(s in obj for s in ("Bearer ", "api_key=", "token=")):
            return "[REDACTED]"
    return obj


class Audit:
    """Append-only structured audit trail backed by the DB.

    ``note`` provides an optional piggyback logging channel so callers can
    attach a short human-readable line without going through the logger.
    """

    def __init__(self, db: DB):
        self.db = db

    # ------------------------------------------------------------------
    def record(self, action: str, *, run_id: str = "", actor: str = "",
               kind: str = "", target: str = "", decision: str = "allowed",
               reason: str = "", outcome: str = "", detail: Any = "",
               idem_key: str = "", ts: int | None = None) -> int:
        ts = ts if ts is not None else int(time.time())
        try:
            detail_str = json.dumps(redact(detail), ensure_ascii=False)
        except Exception:  # noqa: BLE001 — never fail the audit on bad metadata
            detail_str = ""
        return self.db.log_audit(
            run_id=run_id or "-", ts=ts, actor=actor or "-", action=action,
            kind=kind, target=target or "", decision=decision, reason=reason or "",
            outcome=outcome or "", detail=detail_str, idem_key=idem_key or "")

    # ------------------------------------------------------------------
    def allow(self, action: str, *, run_id: str = "", actor: str = "",
              kind: str = "", target: str = "", reason: str = "",
              detail: Any = "", idem_key: str = "") -> int:
        return self.record(action, run_id=run_id, actor=actor, kind=kind,
                           target=target, decision="allowed", reason=reason,
                           outcome="ok", detail=detail, idem_key=idem_key)

    def deny(self, action: str, *, run_id: str = "", actor: str = "",
             kind: str = "", target: str = "", reason: str = "",
             detail: Any = "") -> int:
        return self.record(action, run_id=run_id, actor=actor, kind=kind,
                           target=target, decision="denied", reason=reason,
                           outcome="not_applied", detail=detail)

    def degrade(self, action: str, *, run_id: str = "", actor: str = "",
                kind: str = "", target: str = "", reason: str = "",
                detail: Any = "") -> int:
        return self.record(action, run_id=run_id, actor=actor, kind=kind,
                           target=target, decision="degraded", reason=reason,
                           outcome="partial", detail=detail)

    # ------------------------------------------------------------------
    def recent(self, limit: int = 100, action: str = "",
               actor: str = "") -> list[dict[str, Any]]:
        rows = self.db.audit_recent(limit=limit, action=action, actor=actor)
        return [dict(r) for r in rows]

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.audit_by_run(run_id)]

    def count(self) -> int:
        return self.db.audit_count()

    def prune(self, before_ts: int) -> int:
        """Apply retention: drop rows finished before ``before_ts``."""
        return self.db.audit_prune(before_ts)
