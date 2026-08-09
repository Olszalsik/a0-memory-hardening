# Per-subdir rate limiter (Phase 3).
from __future__ import annotations
import logging
from typing import Any, Optional
from agent import LoopData
from helpers.extension import Extension
from helpers import plugins
from usr.plugins.memory_hardening.helpers import rate_limiter as rl, telemetry as tm

log = logging.getLogger("memory_hardening.rate_limiter")


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


def _get_subdir(agent) -> str:
    try:
        cfg = plugins.get_plugin_config("_memory", agent) or {}
        if cfg.get("project_memory_isolation", True):
            from helpers import projects
            pn = projects.get_context_project_name(agent.context)
            if pn:
                return f"projects/{pn}"
        return cfg.get("agent_memory_subdir", "") or "default"
    except Exception:
        return "default"


class RateLimiter(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("rate_limiter_enabled", True):
            return
        if self.agent is None:
            return
        subdir = _get_subdir(self.agent)
        max_pm = int(cfg.get("rate_limiter_max_per_min", 20))
        burst = int(cfg.get("rate_limiter_burst", 5))
        if not rl.try_acquire(subdir, max_per_min=max_pm, burst=burst):
            tm.record_outcome(outcome="skipped_breaker")
            try:
                loop_data.extras_persistent["_memory_rate_limited"] = True
            except Exception:
                pass
            log.debug("rate_limiter: throttled subdir=%s", subdir)
