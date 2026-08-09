# Memorize task watchdog (Phase 2).
#
# The _memory plugin runs memorize in a background thread via
# DeferredTask. If the embedding call hangs, the thread sits
# indefinitely. We cannot cancel a foreign thread from the main
# asyncio loop, but we can:
#
# 1. Stamp a monologue_start timestamp on the agent context.
# 2. On monologue_end, compute elapsed time and warn if the memorize
#    phase has been running longer than the soft cap.
# 3. Record into telemetry so the WebUI dashboard can surface it.

from __future__ import annotations

import logging
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
    warned_soft: bool = False
    warned_hard: bool = False


class MemorizeWatchdogRegistry:
    _states: Dict[str, MemorizeState] = {}

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
        if agent is None:
            return
        key = cls._key(agent)
        now = time.time()
        existing = cls._states.get(key)
        if existing is None:
            cls._states[key] = MemorizeState(
                started_at=now, last_seen_at=now, phase=phase
            )
        else:
            existing.last_seen_at = now
            if existing.phase != phase:
                existing.phase = f"{existing.phase}+{phase}"

    @classmethod
    def end(cls, agent) -> Optional[float]:
        if agent is None:
            return None
        key = cls._key(agent)
        st = cls._states.pop(key, None)
        if st is None:
            return None
        return time.time() - st.started_at

    @classmethod
    def check(
        cls,
        agent,
        *,
        soft_cap_sec: float = 120.0,
        hard_warn_sec: float = 300.0,
    ) -> Optional[Dict]:
        if agent is None:
            return None
        key = cls._key(agent)
        st = cls._states.get(key)
        if st is None:
            return None
        elapsed = time.time() - st.started_at
        info = {
            "phase": st.phase,
            "elapsed_sec": round(elapsed, 1),
            "soft_cap_sec": soft_cap_sec,
            "hard_warn_sec": hard_warn_sec,
            "warned_soft": st.warned_soft,
            "warned_hard": st.warned_hard,
        }
        if elapsed >= hard_warn_sec and not st.warned_hard:
            st.warned_hard = True
            log.warning(
                "memorize task over hard warn threshold (%.0fs) for %s",
                elapsed, st.phase,
            )
            info["level"] = "hard"
            return info
        if elapsed >= soft_cap_sec and not st.warned_soft:
            st.warned_soft = True
            log.info(
                "memorize task over soft cap (%.0fs) for %s",
                elapsed, st.phase,
            )
            info["level"] = "soft"
            return info
        return None

    @classmethod
    def snapshot(cls) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        now = time.time()
        for k, st in cls._states.items():
            out[k] = {
                "phase": st.phase,
                "started_at": st.started_at,
                "elapsed_sec": round(now - st.started_at, 1),
                "warned_soft": st.warned_soft,
                "warned_hard": st.warned_hard,
            }
        return out

    @classmethod
    def clear(cls) -> None:
        cls._states.clear()
