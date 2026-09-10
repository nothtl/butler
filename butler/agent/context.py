"""Phase 7 / M1: deterministic context builder.

Wraps the existing :class:`butler.context.ContextEngine` into a single
:class:`ContextBundle` that reasoning and tools can consume. No LLM is involved
here — the bundle is pure DB + scheduler geometry, so it is reproducible and
instant.
"""

from __future__ import annotations

import logging
from typing import Any

from .models import ContextBundle

log = logging.getLogger("butler.agent.context")


class ContextBuilder:
    def __init__(self, container: Any):
        self.container = container
        self.engine = getattr(container, "context", None)

    def build(self, *, focus: str = "") -> ContextBundle:
        """Return the live context. Never raises; degrades to an empty bundle."""
        data: dict[str, Any] = {}
        text = ""
        sources: list[str] = []
        engine = self.engine
        if engine is not None and hasattr(engine, "snapshot"):
            try:
                data = dict(engine.snapshot())
                sources.append("context_engine")
            except Exception:  # noqa: BLE001 — context must never break a reply
                log.debug("context snapshot failed", exc_info=True)
        if engine is not None and hasattr(engine, "describe"):
            try:
                text = str(engine.describe())
            except Exception:  # noqa: BLE001
                text = ""
        if focus:
            data = dict(data)
            data["focus"] = focus
        return ContextBundle(
            now=int(data.get("now", 0) or 0),
            text=text,
            data=data,
            sources=sources,
        )
