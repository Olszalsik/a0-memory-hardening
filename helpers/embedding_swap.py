# Embedding hot-swap shadow validation (Phase 3).
# Framework for validating a new embedding model before switching.
# The actual parallel execution requires hooking into the embedding
# model call site; this module provides the comparison infrastructure.
from __future__ import annotations
import logging
import math
import time
from collections import deque
from typing import Dict, List, Optional

log = logging.getLogger("memory_hardening.embedding_swap")

_history: deque = deque(maxlen=64)
_active_swap: Optional[dict] = None
_stats: Dict[str, int] = {"swaps_initiated": 0, "swaps_completed": 0, "swaps_aborted": 0}


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def begin_swap(old_model: str, new_model: str, *, shadow_requests: int = 10) -> dict:
    global _active_swap
    _active_swap = {
        "old_model": old_model,
        "new_model": new_model,
        "shadow_requests": shadow_requests,
        "started_at": time.time(),
        "comparisons": [],
    }
    _stats["swaps_initiated"] += 1
    log.info("embedding_swap: initiated old=%s new=%s shadow=%d", old_model, new_model, shadow_requests)
    return _active_swap


def record_comparison(*, old_vec: List[float], new_vec: List[float]) -> float:
    if _active_swap is None:
        return 0.0
    sim = _cosine(old_vec, new_vec)
    _active_swap["comparisons"].append({"at": time.time(), "similarity": sim})
    return sim


def should_commit(*, consensus_min: float = 0.8) -> bool:
    if _active_swap is None:
        return False
    comps = _active_swap["comparisons"]
    if len(comps) < _active_swap["shadow_requests"]:
        return False
    sims = [c["similarity"] for c in comps]
    avg = sum(sims) / len(sims) if sims else 0.0
    return avg >= consensus_min


def commit() -> Optional[dict]:
    global _active_swap
    if _active_swap is None:
        return None
    rec = dict(_active_swap)
    rec["committed_at"] = time.time()
    _history.append(rec)
    _active_swap = None
    _stats["swaps_completed"] += 1
    log.info("embedding_swap: committed")
    return rec


def abort(reason: str = "") -> None:
    global _active_swap
    if _active_swap is None:
        return
    _active_swap["aborted_at"] = time.time()
    _active_swap["abort_reason"] = reason
    _history.append(_active_swap)
    _active_swap = None
    _stats["swaps_aborted"] += 1
    log.warning("embedding_swap: aborted reason=%s", reason)


def snapshot() -> Dict:
    return {
        "active": _active_swap,
        "stats": dict(_stats),
        "history": list(_history),
    }


def reset() -> None:
    global _active_swap
    _active_swap = None
    _history.clear()
    for k in _stats:
        _stats[k] = 0
