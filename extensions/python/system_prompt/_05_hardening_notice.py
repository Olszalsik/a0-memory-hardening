# Optional one-line system prompt notice.
#
# Runs at `system_prompt` (priority 5) - runs very early in prompt
# construction. Only emits a section if the agent_notice_enabled flag
# is set AND the circuit breaker is currently open. Default off, so
# this is a no-op for the common case.
#
# Master kill switch: plugin config `hardening_enabled: false`.

from __future__ import annotations

import logging
from typing import Any, Optional

from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import circuit_breaker as cb

log = logging.getLogger("memory_hardening.notice")


def _read_cfg(agent) -> Optional[dict]:
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class HardeningNotice(Extension):
    def execute(self, **kwargs: Any) -> None:
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("agent_notice_enabled", False):
            return
        if not cfg.get("breaker_enabled", True):
            return

        breaker = cb.get_instance(
            window_sec=cfg.get("breaker_window_sec", 300.0),
            failure_threshold=cfg.get("breaker_failure_threshold", 3),
            cooldown_sec=cfg.get("breaker_cooldown_sec", 60.0),
            max_entries=cfg.get("breaker_max_entries", 64),
        )
        state = breaker.state()
        if state["state"] not in ("open", "half_open"):
            return

        # The `system_prompt` extension contract passes a list of prompt
        # fragments (see agent.py and upstream _memory's
        # system_prompt/_20_behaviour_prompt.py, which appends to it).
        # v0.5.4 fix: this extension used to write to a `data` kwarg that
        # does not exist, so the notice was silently discarded.
        try:
            prompts = kwargs.get("system_prompt")
            if not isinstance(prompts, list):
                return
            notice = (
                "Memory recall is temporarily disabled because the memory "
                "search pipeline has been failing. Past memories will not "
                "be included in this turn. (memory_hardening: breaker "
                + state["state"]
                + ", cooldown remaining "
                + str(int(state["cooldown_remaining_sec"]))
                + "s)"
            )
            prompts.append(notice)
        except Exception as e:
            log.debug("notice emit failed: %s", e)
