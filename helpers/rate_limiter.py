# Per-subdir token-bucket rate limiter (Phase 3).
# Caps FAISS recall rate per memory subdir to prevent thrashing.
from __future__ import annotations
import threading
import time
from typing import Dict, Optional

_lock = threading.Lock()
_buckets: Dict[str, dict] = {}
_stats: Dict[str, int] = {"allowed": 0, "throttled": 0}


def _bucket(subdir: str, *, max_per_min: int, burst: int) -> dict:
    now = time.time()
    if subdir not in _buckets:
        _buckets[subdir] = {"tokens": float(burst), "last": now, "rate": max_per_min / 60.0}
    b = _buckets[subdir]
    elapsed = now - b["last"]
    b["tokens"] = min(float(burst), b["tokens"] + elapsed * b["rate"])
    b["last"] = now
    b["rate"] = max_per_min / 60.0
    return b


def try_acquire(subdir: str, *, max_per_min: int = 20, burst: int = 5) -> bool:
    if not subdir:
        return True
    with _lock:
        b = _bucket(subdir, max_per_min=max_per_min, burst=burst)
        if b["tokens"] >= 1.0:
            b["tokens"] -= 1.0
            _stats["allowed"] += 1
            return True
        _stats["throttled"] += 1
        return False


def release(subdir: str) -> None:
    pass  # token bucket: no explicit release


def snapshot() -> Dict:
    with _lock:
        return {
            "subdirs_tracked": list(_buckets.keys()),
            "stats": dict(_stats),
            "buckets": {
                k: {"tokens": round(v["tokens"], 2), "rate_per_sec": round(v["rate"], 4)}
                for k, v in _buckets.items()
            },
        }


def reset() -> None:
    with _lock:
        _buckets.clear()
        for k in _stats:
            _stats[k] = 0
