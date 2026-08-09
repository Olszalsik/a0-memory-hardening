# Memorize hard-cancel (Phase 3).
# The _memory plugin runs memorize in a background thread via
# DeferredTask. Python threads cannot be force-cancelled. The
# realistic options are:
# 1. Cooperative cancellation: set a flag the task checks between
#    expensive steps. The task then exits early.
# 2. Thread-state inspection: detect threads blocked on I/O or
#    locks and report them as stuck.
# 3. Process-level escalation: log a warning so an operator can
#    decide to restart.
#
# We implement (1) and (2). For (3) we surface the data in the
# stats endpoint so the WebUI can show it.
from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger("memory_hardening.memorize_cancel")

_lock = threading.Lock()
_cancel_flags: Dict[str, bool] = {}
_thread_snapshots: Dict[str, dict] = {}
_stats: Dict[str, int] = {"cancelled_cooperative": 0, "stuck_detected": 0}


def request_cancel(agent_key: str) -> None:
    if not agent_key:
        return
    with _lock:
        _cancel_flags[agent_key] = True
        _stats["cancelled_cooperative"] += 1
    log.warning("memorize hard-cancel requested for %s", agent_key)


def is_cancelled(agent_key: str) -> bool:
    with _lock:
        return _cancel_flags.get(agent_key, False)


def clear(agent_key: str) -> None:
    with _lock:
        _cancel_flags.pop(agent_key, None)
        _thread_snapshots.pop(agent_key, None)


def scan_stuck_threads(*, threshold_sec: float = 300.0) -> List[dict]:
    """Enumerate all live threads, identify those whose name matches
    a memorize pattern and that have been alive longer than threshold.
    Returns a list of stuck thread records.
    """
    out: List[dict] = []
    now = time.time()
    for t in threading.enumerate():
        name = t.name or ""
        if "background" in name.lower() or "memorize" in name.lower() or "defer" in name.lower():
            # We cannot get thread start time directly; approximate via ident
            alive_for = now - getattr(t, "_mh_started_at", now)
            if alive_for >= threshold_sec and t.is_alive():
                rec = {
                    "name": name,
                    "ident": t.ident,
                    "alive_sec": round(alive_for, 1),
                    "daemon": t.daemon,
                }
                out.append(rec)
                _stats["stuck_detected"] += 1
    with _lock:
        for rec in out:
            _thread_snapshots[rec["ident"]] = rec
    return out


def snapshot() -> Dict:
    with _lock:
        return {
            "stats": dict(_stats),
            "active_cancel_flags": list(_cancel_flags.keys()),
            "stuck_threads": list(_thread_snapshots.values()),
        }


def reset() -> None:
    with _lock:
        _cancel_flags.clear()
        _thread_snapshots.clear()
        for k in _stats:
            _stats[k] = 0
