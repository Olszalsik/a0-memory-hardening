# Memorize hard cancel (Phase 3). See helpers/memorize_canceller.py for full notes.
from __future__ import annotations
import logging
import time
from typing import Any, Optional
from agent import LoopData
from helpers.extension import Extension
from helpers import plugins
from usr.plugins.memory_hardening.helpers import (
    memorize_canceller as mc,
    memorize_watchdog as mw,
    telemetry as tm,
)

log = logging.getLogger("memory_hardening.memorize_canceller")


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class MemorizeCanceller(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("memorize_hard_cancel_enabled", False):
            return
        if self.agent is None:
            return
        key = mw.MemorizeWatchdogRegistry._key(self.agent)
        snap = mw.MemorizeWatchdogRegistry.snapshot()
        state = snap.get(key)
        if state and state.get("warned_hard"):
            mc.request_cancel(key)
            tm.record_health_warning()
            log.warning("memorize_canceller: requested hard cancel for %s", key)
        scan_sec = float(cfg.get("memorize_hard_cancel_scan_sec", 60))
        stuck = mc.scan_stuck_threads(threshold_sec=scan_sec)
        if stuck:
            tm.record_health_warning()
            log.warning("memorize_canceller: detected %d stuck thread(s)", len(stuck))
