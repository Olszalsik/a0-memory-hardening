# Adaptive recall interval (Phase 3). Adjusts memory_recall_interval
# in the _memory plugin based on observed p99 latency.
from __future__ import annotations
import logging
import time
from typing import Any, Optional
from helpers.extension import Extension
from helpers import plugins
from usr.plugins.memory_hardening.helpers import adaptive_interval as ai, telemetry as tm

log = logging.getLogger("memory_hardening.adaptive_interval")


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class AdaptiveInterval(Extension):
    async def execute(self, **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("adaptive_interval_enabled", False):
            return
        # Read the _memory plugin's current interval from its config
        try:
            mem_cfg = plugins.get_plugin_config("_memory", self.agent) or {}
            current = int(mem_cfg.get("memory_recall_interval", 3))
        except Exception:
            current = 3
        new = ai.adjust(
            min_interval=int(cfg.get("adaptive_interval_min", 2)),
            max_interval=int(cfg.get("adaptive_interval_max", 15)),
            target_p99_ms=float(cfg.get("adaptive_interval_target_p99_ms", 5000)),
            current_interval=current,
        )
        if new != current:
            try:
                # Persist the new interval to the _memory plugin config
                plugins.set_plugin_config("_memory", self.agent, {"memory_recall_interval": new})
                log.info("adaptive_interval: %d -> %d (p99=%sms)", current, new, ai.p99_ms())
            except Exception as e:
                log.debug("adaptive_interval set failed: %s", e)
