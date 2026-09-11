"""LIVE (optional) DeepSeek structured-interpretation smoke test.

Run:  .venv/bin/python tests/run_live_deepseek_semantic.py

This makes REAL network calls to the configured OpenAI-compatible endpoint. It
is intentionally NOT part of the deterministic acceptance aggregate: the gate
must never depend on a live model. Exits 0 with BLOCKED when no key is
configured, and non-zero only on a real failure.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from butler.agent.interpret import resolve_interpreter  # noqa: E402
from butler.agent.schema import request_json_schema  # noqa: E402
from butler.agent.semantic import ActionKind, AgentRequest  # noqa: E402
from butler.core import Container  # noqa: E402

UTTERANCES = [
    "Track CS188 homework and tell me if deadlines change.",
    "Let me know when the basketball club posts something important.",
    "Keep tabs on new coursework in CS188.",
    "Don't schedule work after 10 PM.",
    "Use what I have in the fridge when deciding dinner.",
    "Remember I prefer studying in two-hour blocks.",
]


def main() -> int:
    container = Container()
    cfg = container.cfg
    if not (getattr(cfg, "llm_api_key", "") and getattr(cfg, "llm_base_url", "")):
        print("BLOCKED: no LLM key/base_url configured")
        return 0
    chat = getattr(container, "chat", None)
    if chat is None:
        print("BLOCKED: no chat client on the container")
        return 0

    print(f"endpoint: {cfg.llm_base_url}  model: {cfg.llm_model}")
    print(f"schema keys: {sorted(request_json_schema()['properties'])[:6]}...")

    # 1) a direct real request, reporting HTTP status and latency
    import requests
    url = cfg.llm_base_url.rstrip("/") + "/chat/completions"
    t0 = time.perf_counter()
    status = 0
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json={
                "model": cfg.llm_model,
                "messages": [
                    {"role": "system", "content": "Return only JSON."},
                    {"role": "user", "content": 'Return {"ok": true}'},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=60,
        )
        status = resp.status_code
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: live request error: {type(exc).__name__}: {exc}")
        return 1
    latency_ms = int((time.perf_counter() - t0) * 1000)
    print(f"live request: HTTP {status}, {latency_ms} ms, "
          f"json_object parsed={isinstance(parsed, dict)}")
    if status != 200 or not isinstance(parsed, dict):
        print("FAIL: live endpoint did not return valid JSON mode output")
        return 1

    # 2) the real Butler semantic path (Chat -> LLMInterpreter -> AgentRequest)
    interp = resolve_interpreter(container)
    print(f"semantic path available: {interp.available()}")
    passed = failed = 0
    for text in UTTERANCES:
        t0 = time.perf_counter()
        try:
            req = interp.interpret(text, topic={"topic_name": "Smoke"})
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {text!r}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        ms = int((time.perf_counter() - t0) * 1000)
        ok = (isinstance(req, AgentRequest)
              and req.action in ActionKind
              and 0.0 <= req.confidence <= 1.0)
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {text[:52]!r} -> "
              f"source={interp.last_source} action={req.action.value} "
              f"conf={req.confidence:.2f} {ms}ms")
    print(f"\nlive semantic: {passed} passed, {failed} failed")
    if failed:
        return 1
    print("RESULT: PASS (live DeepSeek structured interpretation)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
