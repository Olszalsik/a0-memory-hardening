# Per-subdir circuit breaker (Phase 3).
# Like circuit_breaker.py but one breaker per memory subdir.
from __future__ import annotations
import threading
import time
from collections import deque
from typing import Dict, Optional

_lock = threading.Lock()
_breakers: Dict[str, dict] = {}


def get(subdir: str, *, window_sec: float, threshold: int, cooldown_sec: float) -> dict:
    key = subdir or "_default_"
    with _lock:
        if key not in _breakers:
            _breakers[key] = {
                "events": deque(maxlen=64),
                "state": "closed",
                "opened_at": None,
                "window_sec": window_sec,
                "threshold": threshold,
                "cooldown_sec": cooldown_sec,
            }
        b = _breakers[key]
        b["window_sec"] = window_sec
        b["threshold"] = threshold
        b["cooldown_sec"] = cooldown_sec
        return b


def record(subdir: str, outcome: str, **kw) -> None:
    b = get(subdir, **kw)
    now = time.time()
    b["events"].append({"ts": now, "outcome": outcome})
    cutoff = now - b["window_sec"]
    while b["events"] and b["events"][0]["ts"] < cutoff:
        b["events"].popleft()
    failures = sum(1 for e in b["events"] if e["outcome"] in ("failed", "timeout"))
    if b["state"] == "closed" and failures >= b["threshold"]:
        b["state"] = "open"
        b["opened_at"] = now
    elif b["state"] == "open" and b["opened_at"] is not None and (now - b["opened_at"]) >= b["cooldown_sec"]:
        b["state"] = "half_open"


def should_skip(subdir: str, **kw) -> bool:
    b = get(subdir, **kw)
    if b["state"] == "open":
        return True
    if b["state"] == "half_open":
        return False  # allow one trial
    return False


def snapshot() -> Dict:
    with _lock:
        return {
            subdir: {
                "state": b["state"],
                "opened_at": b["opened_at"],
                "failure_count": sum(1 for e in b["events"] if e["outcome"] in ("failed", "timeout")),
                "event_count": len(b["events"]),
            }
            for subdir, b in _breakers.items()
        }


def reset(subdir: Optional[str] = None) -> None:
    with _lock:
        if subdir is None:
            _breakers.clear()
        elif subdir in _breakers:
            del _breakers[subdir]
