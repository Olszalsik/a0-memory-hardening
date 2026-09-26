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
# We implement (2) properly and expose the (1) flag. Honest status of each:
#
# (2) Thread-state inspection — WORKS. See scan_stuck_threads.
# (1) Cooperative cancellation — the flag is set and readable via
#     is_cancelled(), but nothing inside the memorize task polls it. The
#     `_memory` plugin owns that coroutine and there is no seam to inject a
#     poll into without modifying upstream, so today the flag is advisory
#     only. It is NOT documented as working cancellation, and `stats`
#     reports it as `active_cancel_flags` rather than as a cancellation count.
# (3) Process-level escalation — surfaced on /stats for the WebUI.

from __future__ import annotations
import logging
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger("memory_hardening.memorize_cancel")

_lock = threading.Lock()
_cancel_flags: Dict[str, bool] = {}
_thread_snapshots: Dict[int, dict] = {}
_stats: Dict[str, int] = {"cancelled_cooperative": 0, "stuck_detected": 0}
# ident -> monotonic timestamp of the first scan that saw it alive.
# See scan_stuck_threads for why this is necessary.
_first_seen: Dict[int, float] = {}


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
    """Report background threads that have been continuously observed alive
    for at least ``threshold_sec``.

    HOW THE DURATION IS MEASURED
    ----------------------------
    `threading.Thread` exposes no start timestamp, and the previous
    implementation used ``getattr(t, "_mh_started_at", now)`` — an attribute
    nothing in the repository ever assigned. The default therefore always
    won, ``alive_for`` was always ``0``, and the ``>= threshold_sec`` test
    could never fire: this function structurally returned ``[]`` on every
    call, and ``stuck_detected`` was permanently 0.

    Instead we keep our own first-seen map, keyed by thread ident:

        first call for an ident  -> record the observation time
        later calls              -> alive_for = now - first_seen

    A thread absent from an enumeration has finished (or been replaced), so
    its entry is dropped and a thread that goes away and comes back with the
    same ident is re-based rather than inheriting an old age. Call this
    repeatedly (monologue_end and the job_loop both do) and the measurement
    is a genuine continuous-observation window.

    Ident reuse is a known limitation: CPython recycles thread idents, so a
    long-dead thread's ident can be handed to a new thread. Re-basing on
    first sight bounds the error to the reused ident's first observation.

    Returns a list of stuck thread records.
    """
    global _first_seen
    out: List[dict] = []
    now = time.monotonic()
    live: Dict[int, float] = {}

    for t in threading.enumerate():
        name = (t.name or "").lower()
        if "background" not in name and "memorize" not in name and "defer" not in name:
            continue
        if not t.is_alive():
            continue
        ident = t.ident
        if ident is None:
            continue
        first_seen = _first_seen.get(ident)
        if first_seen is None:
            first_seen = now
            _first_seen[ident] = first_seen
        live[ident] = first_seen
        alive_for = now - first_seen
        if alive_for >= threshold_sec:
            rec = {
                "name": t.name or "",
                "ident": ident,
                "alive_sec": round(alive_for, 1),
                "daemon": t.daemon,
            }
            out.append(rec)

    # Anything not seen this pass is gone: drop it so it cannot age.
    _first_seen = live

    with _lock:
        for rec in out:
            _thread_snapshots[rec["ident"]] = rec
        _stats["stuck_detected"] += len(out)
    return out


def snapshot() -> Dict:
    with _lock:
        return {
            "stats": dict(_stats),
            "active_cancel_flags": list(_cancel_flags.keys()),
            "stuck_threads": list(_thread_snapshots.values()),
        }


def reset() -> None:
    global _first_seen
    with _lock:
        _cancel_flags.clear()
        _thread_snapshots.clear()
        _first_seen = {}
        for k in _stats:
            _stats[k] = 0
