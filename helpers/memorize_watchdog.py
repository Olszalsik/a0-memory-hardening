# Memorize task watchdog (Phase 2).
#
# The _memory plugin runs memorize in a background thread via
# DeferredTask (`MemorizeMemories.memorize` / `MemorizeSolutions.memorize`
# in plugins/_memory/extensions/python/monologue_end/_50|_51_*.py). If
# the embedding call hangs, the coroutine sits indefinitely.
#
# v0.5.4 redesign: the old implementation stamped a timestamp on the
# first monologue_end and never closed it, so `elapsed` measured the
# context's TOTAL wall time since the first turn -- any chat alive
# longer than the soft cap triggered false "memorize over cap" alarms,
# and `warned_hard` stayed set forever (making the hard canceller fire
# on every subsequent turn).
#
# The watchdog extension now binds the framework's actual upstream
# extension classes (the same synthetic-module resolution the
# recall-wait guard uses) and wraps their `memorize` coroutine, so
# `begin()`/`end()` bracket the REAL memorize work. `begin()` is
# refcounted because fragments and solutions can run concurrently;
# `elapsed` in check() is the true in-flight memorize duration, and the
# per-run warned flags reset when a run ends.
#
# 1. On memorize start, stamp the start time (refcounted).
# 2. On memorize completion, record the real duration.
# 3. check() (called from monologue_end) warns only about a run that is
#    STILL active and has genuinely exceeded the caps.
# 4. Record into telemetry so the WebUI dashboard can surface it.

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

log = logging.getLogger("memory_hardening.memorize_wd")


KEY_MEMORIZE_STARTED = "_mh_memorize_started_at"
KEY_MEMORIZE_PHASE = "_mh_memorize_phase"


@dataclass
class MemorizeState:
    started_at: float
    last_seen_at: float
    phase: str = "memorize"
    active_runs: int = 1
    warned_soft: bool = False
    warned_hard: bool = False


class MemorizeWatchdogRegistry:
    _states: Dict[str, MemorizeState] = {}
    _lock = threading.Lock()

    @classmethod
    def _key(cls, agent) -> str:
        try:
            ctx = getattr(agent, "context", None)
            ctx_id = getattr(ctx, "id", None) if ctx else None
            if ctx_id:
                return f"ctx:{ctx_id}"
        except Exception:
            pass
        return f"agent:{id(agent)}"

    @classmethod
    def begin(cls, agent, phase: str = "memorize") -> None:
        """Stamp the start of a memorize run (thread-safe, refcounted).

        Concurrent memorize runs for the same context (fragments +
        solutions) share one state; the start time and warned flags are
        taken from the FIRST run that begins.
        """
        if agent is None:
            return
        key = cls._key(agent)
        now = time.time()
        with cls._lock:
            existing = cls._states.get(key)
            if existing is not None and existing.active_runs > 0:
                existing.active_runs += 1
                existing.last_seen_at = now
                return
            cls._states[key] = MemorizeState(
                started_at=now, last_seen_at=now, phase=phase, active_runs=1
            )

    @classmethod
    def end(cls, agent) -> Optional[float]:
        """Close one memorize run. Returns the run's real duration when
        this was the last concurrent run, None otherwise (overlapping
        runs still active). Thread-safe.
        """
        if agent is None:
            return None
        key = cls._key(agent)
        now = time.time()
        with cls._lock:
            st = cls._states.get(key)
            if st is None:
                return None
            st.active_runs -= 1
            if st.active_runs > 0:
                st.last_seen_at = now
                return None
            del cls._states[key]
            return now - st.started_at

    @classmethod
    def check(
        cls,
        agent,
        *,
        soft_cap_sec: float = 120.0,
        hard_warn_sec: float = 300.0,
    ) -> Optional[Dict]:
        """Report on the ACTIVE memorize run for this context, if its
        true elapsed time has crossed a cap. Warns once per run.
        """
        if agent is None:
            return None
        key = cls._key(agent)
        with cls._lock:
            st = cls._states.get(key)
            if st is None or st.active_runs <= 0:
                return None
            elapsed = time.time() - st.started_at
            if elapsed >= hard_warn_sec and not st.warned_hard:
                st.warned_hard = True
                level = "hard"
            elif elapsed >= soft_cap_sec and not st.warned_soft:
                st.warned_soft = True
                level = "soft"
            else:
                return None
        log.warning(
            "memorize task over %s threshold (%.0fs) for %s",
            level, elapsed, st.phase,
        )
        return {
            "phase": st.phase,
            "elapsed_sec": round(elapsed, 1),
            "soft_cap_sec": soft_cap_sec,
            "hard_warn_sec": hard_warn_sec,
            "warned_soft": st.warned_soft,
            "warned_hard": st.warned_hard,
            "level": level,
        }

    @classmethod
    def snapshot(cls) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        now = time.time()
        with cls._lock:
            for k, st in cls._states.items():
                if st.active_runs <= 0:
                    continue
                out[k] = {
                    "phase": st.phase,
                    "started_at": st.started_at,
                    "elapsed_sec": round(now - st.started_at, 1),
                    "active_runs": st.active_runs,
                    "warned_soft": st.warned_soft,
                    "warned_hard": st.warned_hard,
                }
        return out

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._states.clear()