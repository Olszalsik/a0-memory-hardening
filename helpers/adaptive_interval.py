# Adaptive recall interval (Phase 3).
# Watches p99 recall latency and widens memory_recall_interval when
# the breaker is tripping, narrows back when stable.
from __future__ import annotations
import time
from collections import deque
from typing import Dict, Optional

_latencies: deque = deque(maxlen=64)
_last_adjust_at: float = 0.0
_current_interval: int = 3
_history: list = []


def record_latency_ms(ms: float) -> None:
    if ms is not None and ms >= 0:
        _latencies.append(ms)


def p99_ms() -> Optional[float]:
    if not _latencies:
        return None
    s = sorted(_latencies)
    return s[min(len(s) - 1, int(len(s) * 0.99))]


def adjust(
    *,
    min_interval: int = 2,
    max_interval: int = 15,
    target_p99_ms: float = 5000.0,
    cool_down_sec: float = 60.0,
    current_interval: int = 3,
) -> int:
    global _last_adjust_at, _current_interval
    now = time.time()
    if (now - _last_adjust_at) < cool_down_sec:
        return _current_interval
    p99 = p99_ms()
    if p99 is None:
        return _current_interval
    new = _current_interval
    if p99 > target_p99_ms * 1.5:
        new = min(max_interval, _current_interval + 1)
    elif p99 < target_p99_ms * 0.7 and _current_interval > min_interval:
        new = max(min_interval, _current_interval - 1)
    if new != _current_interval:
        _history.append({"at": now, "from": _current_interval, "to": new, "p99_ms": p99})
        _current_interval = new
    _last_adjust_at = now
    return new


def get_current() -> int:
    return _current_interval


def snapshot() -> Dict:
    return {
        "current_interval": _current_interval,
        "p99_ms": p99_ms(),
        "sample_count": len(_latencies),
        "history": list(_history[-20:]),
    }


def reset() -> None:
    global _current_interval, _last_adjust_at
    _latencies.clear()
    _history.clear()
    _current_interval = 3
    _last_adjust_at = 0.0
