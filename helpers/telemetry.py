"""
In-memory telemetry store for memory recall outcomes.

Stores rolling-window counters and a bounded latency sample list.
Process-global; safe for concurrent reads/writes (a small lock protects
the samples list; counters use atomic GIL increments).

Exposed via:
  - usr.plugins.memory_hardening.helpers.telemetry.record_outcome(...)
  - usr.plugins.memory_hardening.helpers.telemetry.snapshot()
  - GET /api/plugins/memory_hardening/stats
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


_MAX_SAMPLES_DEFAULT = 100

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "started_at": time.time(),
    "counters": {
        "recall_started": 0,
        "recall_succeeded": 0,
        "recall_failed": 0,
        "recall_timeout": 0,
        "recall_skipped_breaker": 0,
        "recall_skipped_disabled": 0,
        "recall_no_results": 0,
        "stuck_tasks_cancelled": 0,
        "health_warnings": 0,
    },
    "latencies_ms": Deque(),  # type: ignore[var-annotated]
    "last_outcome_at": None,
    "last_outcome": None,
    "last_error": None,
    "agents_seen": set(),  # type: ignore[var-annotated]
}


def _cap_samples(cap: int) -> None:
    # trim to keep the deque small
    while len(_state["latencies_ms"]) > cap:
        _state["latencies_ms"].popleft()


def record_started(agent_id: Optional[str] = None) -> None:
    _state["counters"]["recall_started"] += 1
    _state["last_outcome_at"] = time.time()
    _state["last_outcome"] = "started"
    if agent_id:
        _state["agents_seen"].add(agent_id)


def record_outcome(
    *,
    outcome: str,
    latency_ms: Optional[float] = None,
    error: Optional[str] = None,
    agent_id: Optional[str] = None,
    max_samples: int = _MAX_SAMPLES_DEFAULT,
) -> None:
    """Record a recall outcome.

    outcome: one of:
      - 'success'           -> recall completed and returned results
      - 'no_results'        -> recall completed but found nothing
      - 'failed'            -> recall raised a generic exception
      - 'timeout'           -> recall hit an asyncio.TimeoutError
      - 'skipped_breaker'   -> recall skipped because breaker was open
      - 'skipped_disabled'  -> recall skipped because plugin disabled
    """
    counter_key = {
        "success": "recall_succeeded",
        "no_results": "recall_no_results",
        "failed": "recall_failed",
        "timeout": "recall_timeout",
        "skipped_breaker": "recall_skipped_breaker",
        "skipped_disabled": "recall_skipped_disabled",
    }.get(outcome)
    if counter_key:
        _state["counters"][counter_key] += 1
    _state["last_outcome_at"] = time.time()
    _state["last_outcome"] = outcome
    if error:
        _state["last_error"] = error
    if agent_id:
        _state["agents_seen"].add(agent_id)
    if latency_ms is not None and latency_ms >= 0:
        with _lock:
            _state["latencies_ms"].append(float(latency_ms))
            _cap_samples(max_samples)


def record_stuck_task_cancelled() -> None:
    _state["counters"]["stuck_tasks_cancelled"] += 1


def record_health_warning() -> None:
    _state["counters"]["health_warnings"] += 1


def snapshot() -> Dict[str, Any]:
    """Return a JSON-serialisable snapshot of the current telemetry."""
    with _lock:
        samples: List[float] = list(_state["latencies_ms"])
    counters = dict(_state["counters"])
    if samples:
        sorted_samples = sorted(samples)
        n = len(sorted_samples)
        p50 = sorted_samples[n // 2]
        p95 = sorted_samples[min(n - 1, int(n * 0.95))]
        p99 = sorted_samples[min(n - 1, int(n * 0.99))]
        avg = sum(sorted_samples) / n
        latency_summary = {
            "count": n,
            "avg_ms": round(avg, 2),
            "p50_ms": round(p50, 2),
            "p95_ms": round(p95, 2),
            "p99_ms": round(p99, 2),
            "max_ms": round(sorted_samples[-1], 2),
        }
    else:
        latency_summary = {
            "count": 0,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    started = _state["started_at"]
    return {
        "uptime_sec": round(time.time() - started, 2),
        "started_at": started,
        "counters": counters,
        "latency": latency_summary,
        "last_outcome": _state["last_outcome"],
        "last_outcome_at": _state["last_outcome_at"],
        "last_error": _state["last_error"],
        "agents_seen": len(_state["agents_seen"]),
    }


def reset() -> None:
    """Clear all counters and samples. Used by tests and the reset API."""
    with _lock:
        _state["counters"] = {k: 0 for k in _state["counters"]}
        _state["latencies_ms"].clear()
        _state["last_outcome_at"] = None
        _state["last_outcome"] = None
        _state["last_error"] = None
        _state["agents_seen"].clear()
