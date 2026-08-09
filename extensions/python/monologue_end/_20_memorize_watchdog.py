# Memorize task watchdog (Phase 2). See README for full docstring.
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from agent import LoopData
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import (
    memorize_watchdog as mw,
    telemetry as tm,
)

log = logging.getLogger("memory_hardening.memorize_watchdog")


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


def _agent_id(agent):
    if agent is None:
        return None
    try:
        ctx = getattr(agent, "context", None)
        cid = getattr(ctx, "id", None) if ctx else None
        if cid:
            return str(cid)
    except Exception:
        pass
    return None


class MemorizeWatchdog(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("memorize_watchdog_enabled", True):
            return
        if self.agent is None:
            return

        soft_cap = float(cfg.get("memorize_watchdog_soft_cap_sec", 120.0))
        hard_warn = float(cfg.get("memorize_watchdog_hard_warn_sec", 300.0))

        try:
            seen = self.agent.get_data(mw.KEY_MEMORIZE_STARTED)
        except Exception:
            seen = None
        if not seen:
            try:
                self.agent.set_data(mw.KEY_MEMORIZE_STARTED, time.time())
                self.agent.set_data(mw.KEY_MEMORIZE_PHASE, "memorize")
                mw.MemorizeWatchdogRegistry.begin(self.agent, phase="memorize")
            except Exception as e:
                log.debug("begin failed: %s", e)

        try:
            status = mw.MemorizeWatchdogRegistry.check(
                self.agent, soft_cap_sec=soft_cap, hard_warn_sec=hard_warn
            )
            if status is not None:
                tm.record_health_warning()
                log.warning(
                    "memorize_watchdog[%s] agent=%s phase=%s elapsed=%.0fs cap=%.0fs",
                    status.get("level"),
                    _agent_id(self.agent) or "?",
                    status.get("phase"),
                    status.get("elapsed_sec", 0),
                    hard_warn if status.get("level") == "hard" else soft_cap,
                )
        except Exception as e:
            log.debug("check failed: %s", e)

        try:
            snap = mw.MemorizeWatchdogRegistry.snapshot()
            agent_key = mw.MemorizeWatchdogRegistry._key(self.agent)
            if agent_key not in snap:
                self.agent.set_data(mw.KEY_MEMORIZE_STARTED, None)
                self.agent.set_data(mw.KEY_MEMORIZE_PHASE, None)
        except Exception:
            pass
