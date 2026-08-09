"""
Sliding-window circuit breaker for memory recall failures.

Tracks recent recall outcomes (timeout, failed, success) in a bounded
deque. When the failure count inside a sliding window exceeds the
configured threshold, the breaker opens and recall is short-circuited
for a cooldown period.

A separate per-iteration skip counter prevents repeated re-arming of
the breaker on the next message loop iteration. The breaker closes
automatically after the cooldown elapses and at least one new outcome
has been recorded.

Process-global; one breaker per process. There is no need for
per-context breakers because all contexts share the same FAISS index
process-wide.

Exposed via:
  - record(outcome)
  - should_skip()
  - reset()
  - state()
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

log = logging.getLogger("memory_hardening.breaker")


class CircuitBreaker:
    """Sliding-window breaker.

    States:
      - 'closed'     : normal operation, all calls go through
      - 'open'       : breaker tripped; should_skip() returns True
      - 'half_open'  : cooldown elapsed; one trial allowed; success closes

    Transitions:
      closed -> open    : failure_count in window >= failure_threshold
      open -> half_open : (now - opened_at) >= cooldown_sec
      half_open -> closed: trial succeeded
      half_open -> open  : trial failed; opened_at = now
    """

    def __init__(
        self,
        *,
        window_sec: float = 300.0,
        failure_threshold: int = 3,
        cooldown_sec: float = 60.0,
        max_entries: int = 64,
    ) -> None:
        self.window_sec = float(window_sec)
        self.failure_threshold = int(failure_threshold)
        self.cooldown_sec = float(cooldown_sec)
        self.max_entries = int(max_entries)
        self._events: Deque[Dict[str, Any]] = deque(maxlen=max_entries)
        self._state: str = "closed"
        self._opened_at: Optional[float] = None
        self._half_open_in_flight: bool = False

    # ----- public API ------------------------------------------------

    def record(self, outcome: str) -> None:
        """Record a recall outcome. outcome is one of:
        'success' | 'no_results' | 'failed' | 'timeout'.
        Successes do not count as failures but they do push the
        window forward and may close a half_open breaker.
        """
        now = time.time()
        self._events.append({"ts": now, "outcome": outcome})
        # trim to window
        self._trim(now)
        if outcome in ("failed", "timeout"):
            self._on_failure(now)
        else:
            self._on_success(now)

    def should_skip(self) -> bool:
        """Return True if the breaker is open and recall should be skipped."""
        self._maybe_transition()
        if self._state == "open":
            return True
        if self._state == "half_open":
            # allow exactly one trial at a time
            if self._half_open_in_flight:
                return True
            self._half_open_in_flight = True
            return False
        return False

    def reset(self) -> None:
        self._events.clear()
        self._state = "closed"
        self._opened_at = None
        self._half_open_in_flight = False

    def state(self) -> Dict[str, Any]:
        self._maybe_transition()
        return {
            "state": self._state,
            "opened_at": self._opened_at,
            "cooldown_remaining_sec": (
                max(0.0, self.cooldown_sec - (time.time() - self._opened_at))
                if (self._state == "open" and self._opened_at is not None)
                else 0.0
            ),
            "failure_count_in_window": self._failures_in_window(),
            "window_sec": self.window_sec,
            "failure_threshold": self.failure_threshold,
            "cooldown_sec": self.cooldown_sec,
            "event_count": len(self._events),
        }

    # ----- internals -------------------------------------------------

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._events and self._events[0]["ts"] < cutoff:
            self._events.popleft()

    def _failures_in_window(self) -> int:
        return sum(1 for e in self._events if e["outcome"] in ("failed", "timeout"))

    def _on_failure(self, now: float) -> None:
        if self._state == "half_open":
            # trial failed -> back to open
            self._state = "open"
            self._opened_at = now
            self._half_open_in_flight = False
            return
        if (
            self._state == "closed"
            and self._failures_in_window() >= self.failure_threshold
        ):
            self._state = "open"
            self._opened_at = now
            log.warning(
                "memory_hardening breaker opened: %d failures in %.0fs window",
                self._failures_in_window(),
                self.window_sec,
            )

    def _on_success(self, now: float) -> None:
        if self._state == "half_open":
            self._state = "closed"
            self._opened_at = None
            self._half_open_in_flight = False
            log.info("memory_hardening breaker closed after successful trial")
        elif self._state == "open":
            # if cooldown already elapsed and a success was recorded
            # without a should_skip() call, still close
            self._maybe_transition()

    def _maybe_transition(self) -> None:
        if self._state == "open" and self._opened_at is not None:
            if (time.time() - self._opened_at) >= self.cooldown_sec:
                self._state = "half_open"
                self._half_open_in_flight = False


# ------------------------------------------------------------------------
# Process-global singleton
# ------------------------------------------------------------------------

_instance: Optional[CircuitBreaker] = None
_instance_params: Optional[Dict[str, Any]] = None


def _build_instance(
    *,
    window_sec: float = 300.0,
    failure_threshold: int = 3,
    cooldown_sec: float = 60.0,
    max_entries: int = 64,
) -> CircuitBreaker:
    return CircuitBreaker(
        window_sec=window_sec,
        failure_threshold=failure_threshold,
        cooldown_sec=cooldown_sec,
        max_entries=max_entries,
    )


def get_instance(
    *,
    window_sec: float = 300.0,
    failure_threshold: int = 3,
    cooldown_sec: float = 60.0,
    max_entries: int = 64,
) -> CircuitBreaker:
    """Return the process-global breaker, rebuilding it if the config
    parameters have changed since the last call (e.g. user updated the
    plugin config in the WebUI)."""
    global _instance, _instance_params
    new_params = {
        "window_sec": float(window_sec),
        "failure_threshold": int(failure_threshold),
        "cooldown_sec": float(cooldown_sec),
        "max_entries": int(max_entries),
    }
    if _instance is None or _instance_params != new_params:
        _instance = _build_instance(**new_params)
        _instance_params = new_params
    return _instance


def reset_instance() -> None:
    global _instance, _instance_params
    if _instance is not None:
        _instance.reset()
    _instance = None
    _instance_params = None
