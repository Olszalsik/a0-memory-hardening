# Per-subdir circuit breaker (Phase 3).
# Like circuit_breaker.py but one breaker per memory subdir.
from __future__ import annotations
import threading
import time
from collections import deque
from typing import Dict, Optional

_lock = threading.Lock()
_breakers: Dict[str, dict] = {}

# Single source of truth for whether the feature defaults to on.
#
# Read by BOTH the gate (`message_loop_start/_40_per_subdir_breaker.py`) and
# the outcome recorder (`message_loop_prompts_after/_95_recall_telemetry.py`).
# They previously defaulted to True and False respectively, which is only
# latent because a live config.json carries the key: on a clean install,
# `helpers.plugins.get_plugin_config` does not merge `default_config.yaml`, so
# the gate ran while the recorder did not — no outcome was ever recorded, the
# breaker could never open, and the two disagreed about whether the feature was
# on. Keep this value in sync with `default_config.yaml`.
DEFAULT_ENABLED = True


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
                # Guards the single half_open trial. Mirrors
                # circuit_breaker.CircuitBreaker._half_open_in_flight.
                "half_open_in_flight": False,
            }
        b = _breakers[key]
        b["window_sec"] = window_sec
        b["threshold"] = threshold
        b["cooldown_sec"] = cooldown_sec
        return b


def _maybe_transition(b: dict) -> None:
    """open -> half_open once the cooldown elapsed, in place.

    Called from should_skip() as well as record(). Without it here this
    breaker LATCHES. should_skip() is what the recall gate consults every
    iteration; while it returns True the recall is skipped, so no outcome is
    ever recorded -- and record() was the only place that performed the
    open -> half_open transition. A subdir breaker that opened once therefore
    stayed open for the lifetime of the process, permanently disabling recall
    for that subdir until a manual reset.

    `record()` additionally clears half_open_in_flight, which should_skip()
    cannot do (it has no outcome to attribute the trial to).
    """
    if b["state"] != "open":
        return
    opened_at = b["opened_at"]
    if opened_at is None:
        return
    if (time.time() - opened_at) >= b["cooldown_sec"]:
        b["state"] = "half_open"


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
        b["half_open_in_flight"] = False
    elif b["state"] == "half_open":
        # A trial resolved. Success closes the breaker; any failure re-opens
        # it immediately without waiting for the threshold again.
        if outcome == "ok":
            b["state"] = "closed"
            b["events"].clear()
            b["half_open_in_flight"] = False
        else:
            b["state"] = "open"
            b["opened_at"] = now
            b["half_open_in_flight"] = False
    else:
        _maybe_transition(b)


def should_skip(subdir: str, **kw) -> bool:
    b = get(subdir, **kw)
    if b["state"] == "open":
        _maybe_transition(b)
    if b["state"] == "open":
        return True
    if b["state"] == "half_open":
        # Allow exactly ONE trial through. The in-flight flag is required for
        # the same reason it exists in the global CircuitBreaker: without it
        # every caller that runs before the trial resolves sees "half_open"
        # and starts its own trial, so a recovering subdir absorbs a burst of
        # concurrent searches instead of a single probe.
        if b["half_open_in_flight"]:
            return True
        b["half_open_in_flight"] = True
        return False
    return False


def snapshot() -> Dict:
    with _lock:
        return {
            subdir: {
                "state": b["state"],
                "opened_at": b["opened_at"],
                "half_open_in_flight": b.get("half_open_in_flight", False),
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
