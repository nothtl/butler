"""Phase 6: centralized retry, rate limiting and a runaway-failure
circuit breaker.

Rather than every subsystem retrying with its own ad-hoc loop, there is one
policy:

* ``retry`` — exponential backoff capped at ``retry_max_delay``, bounded by
  ``retry_max`` attempts. A retry keeps the SAME ``run_id`` and SAME idempotency
  key, so a crashed attempt never duplicates its side effect.
* ``rate limit`` — a token bucket over the *consequent-external* class so a
  storm (a burst of deletes / syncs, an LLM runaway) cannot overwhelm the system.
* ``circuit breaker`` — a class that fails repeatedly is opened (paused) for a
  cooldown, then half-left to recover. This keeps a transient external outage
  from turning into a retry avalanche.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from .config import Config

T = TypeVar("T")


@dataclass
class RateBucket:
    capacity: float
    tokens: float
    last: float
    rate: float  # tokens per second


class RetryPolicy:
    """Central retry + rate-limit + circuit-breaker state (per-class)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._buckets: dict[str, RateBucket] = {}
        # class -> {failures: int, opened_at: float}
        self._breakers: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------- rate limiting
    def _bucket(self, key: str) -> RateBucket:
        now = time.time()
        bucket = self._buckets.get(key)
        if bucket is None:
            rate = self._rate_per_sec()
            bucket = RateBucket(capacity=self.cfg.rate_limit_capacity,
                                tokens=self.cfg.rate_limit_capacity,
                                last=now, rate=rate)
            self._buckets[key] = bucket
            return bucket
        elapsed = max(0.0, now - bucket.last)
        bucket.tokens = min(bucket.capacity,
                            bucket.tokens + elapsed * bucket.rate)
        bucket.last = now
        return bucket

    def _rate_per_sec(self) -> float:
        window = max(1, self.cfg.rate_limit_window)
        return float(self.cfg.rate_limit_capacity) / float(window)

    def acquire_permits(self, key: str, n: int = 1) -> bool:
        b = self._bucket(key)
        if b.tokens >= n:
            b.tokens -= n
            return True
        return False

    def rate_exceeded(self, key: str) -> bool:
        return self._bucket(key).tokens < 1

    # --------------------------------------------------------- retry
    def delay(self, attempt: int) -> float:
        """Backoff for ``attempt`` (1-based): base * 2^(attempt-1), capped."""
        base = self.cfg.retry_base_delay
        cap = self.cfg.retry_max_delay
        if base <= 0:
            return 0.0
        d = base * (2 ** max(0, attempt - 1))
        return min(d, cap)

    # ---------------------------------------------------- circuit breaker
    def breaker_open(self, key: str = "external") -> bool:
        state = self._breakers.get(key)
        if not state:
            return False
        if int(state["failures"]) < self.cfg.breaker_threshold:
            return False
        cooldown = self.cfg.breaker_cooldown
        return (time.time() - state["opened_at"]) < cooldown

    def breaker_fail(self, key: str = "external") -> None:
        state = self._breakers.setdefault(
            key, {"failures": 0.0, "opened_at": time.time()})
        state["failures"] = float(state["failures"]) + 1
        if float(state["failures"]) >= self.cfg.breaker_threshold:
            state["opened_at"] = time.time()

    def breaker_success(self, key: str = "external") -> None:
        self._breakers.pop(key, None)

    def breaker_reset(self, key: str = "external") -> None:
        self._breakers.pop(key, None)

    # ------------------------------------------------------------- wrapper
    def run(self, fn: Callable[[], T], *, key: str = "external",
            label: str = "") -> T:
        """Run ``fn`` with bounded exponential-backoff retries.

        A retry is only retried for a *transient* exception (``Retryable``).
        ``label`` is used to forward the error so the caller can decide how to
        surface it (never as false success).
        """
        label = label or getattr(fn, "__name__", "op")
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.retry_max + 1):
            try:
                result = fn()
                self.breaker_success(key)
                return result
            except Retryable as exc:
                last_exc = exc
                if attempt >= self.cfg.retry_max:
                    break
                if self.breaker_open(key):
                    break
                self.breaker_fail(key)
                time.sleep(self.delay(attempt))
            except Exception as exc:  # non-transient: don't mask it
                self.breaker_fail(key)
                raise
        raise RetryExhausted(label, last_exc)


class Retryable(Exception):
    """Raise for a transient failure that RetryPolicy may retry."""


class RetryExhausted(Exception):
    def __init__(self, label: str, exc: Exception | None):
        self.label = label
        self.status = "failed"
        self.error = str(exc) if exc else "retries exhausted"
        super().__init__(f"{label}: retries exhausted ({self.error})")


def is_transient(exc: Exception) -> bool:
    """Generic transient detector for adapter code (timeouts, 5xx, 429)."""
    msg = str(exc).lower()
    return any(k in msg for k in ("timeout", "timed out", "connection reset",
                                  "temporarily", "try again", "too many",
                                  "busy", "backend unavailable", "429", "500",
                                  "502", "503", "504"))
