# Per-subdir circuit breaker (Phase 3).
from __future__ import annotations
import logging
from typing import Any, Optional
from agent import LoopData
from helpers.extension import Extension
from helpers import plugins
from usr.plugins.memory_hardening.helpers import per_subdir_breaker as psb, telemetry as tm

log = logging.getLogger("memory_hardening.per_subdir_breaker")


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


class PerSubdirBreaker(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        # Default comes from psb.DEFAULT_ENABLED, which
        # message_loop_prompts_after/_95_recall_telemetry.py also reads, so the
        # gate and the recorder cannot disagree.
        #
        # Read via getattr with an inline fallback, NEVER as a bare attribute.
        # The framework loads extension files fresh on every dispatch but
        # caches imported HELPER modules in sys.modules for the life of the
        # process. So after a plugin update, this new extension code can run
        # against a previously-imported copy of the helper module — and a bare
        # `psb.DEFAULT_ENABLED` then raises AttributeError straight out of
        # execute(), through call_extensions_async, into agent.monologue, and
        # kills the whole agent loop. That is not theoretical: it is exactly
        # what happened on 2026-09-26 after this constant was introduced.
        # A stale module degrades to the documented default instead.
        default_enabled = getattr(psb, "DEFAULT_ENABLED", True)
        if not cfg.get("per_subdir_breaker_enabled", default_enabled):
            return
        if self.agent is None:
            return
        subdir = _get_subdir(self.agent)
        kw = {
            "window_sec": float(cfg.get("per_subdir_breaker_window_sec", 180)),
            "threshold": int(cfg.get("per_subdir_breaker_threshold", 2)),
            "cooldown_sec": float(cfg.get("per_subdir_breaker_cooldown_sec", 90)),
        }
        if psb.should_skip(subdir, **kw):
            tm.record_outcome(outcome="skipped_breaker")
            try:
                # v0.5.3 fix: params_temporary, not extras_persistent --
                # extras are rendered into the LLM prompt every iteration
                # and persistent entries are never cleared.
                loop_data.params_temporary["_memory_subdir_breaker_open"] = subdir
            except Exception:
                pass
            log.debug("per_subdir_breaker: open for subdir=%s", subdir)
