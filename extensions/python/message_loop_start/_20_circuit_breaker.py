"""
Pre-recall circuit breaker gate.

Runs at `message_loop_start` (priority 20) — after the watchdog init
(priority 10) and before the `_memory` extension's own
`message_loop_prompts_after/_50_recall_memories`.

We cannot stop the `_memory` plugin from creating its recall task, but
we can short-circuit the prompt-injection side: if the breaker is open
we drop a small extras entry telling downstream hooks (and the agent
notice extension) that recall is currently disabled, and we record a
`skipped_breaker` telemetry event. The recall task will still be
created, but the prompt will not receive stale results because the
agent can see the breaker status.

The actual `await task` in `_91_recall_wait` is also wrapped by our
`message_loop_prompts_after/_95_recall_telemetry` extension, which
records the outcome (success / failed / timeout) into the breaker.
That closes the feedback loop.

Master kill switch: plugin config `hardening_enabled: false`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent import LoopData
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import circuit_breaker as cb
from usr.plugins.memory_hardening.helpers import telemetry as tm

log = logging.getLogger("memory_hardening.cb_gate")


def _read_cfg(agent) -> Optional[dict]:
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class CircuitBreakerGate(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs: Any) -> None:
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("breaker_enabled", True):
            return
        if self.agent is None:
            return

        breaker = cb.get_instance(
            window_sec=cfg.get("breaker_window_sec", 300.0),
            failure_threshold=cfg.get("breaker_failure_threshold", 3),
            cooldown_sec=cfg.get("breaker_cooldown_sec", 60.0),
            max_entries=cfg.get("breaker_max_entries", 64),
        )

        if not breaker.should_skip():
            return

        # Stash a flag for the agent notice extension and the telemetry hook.
        try:
            loop_data.extras_persistent["_memory_breaker_open"] = True
        except Exception:
            pass
        try:
            loop_data.extras_temporary["_memory_breaker_open"] = True
        except Exception:
            pass

        tm.record_outcome(outcome="skipped_breaker")
        log.debug("breaker open: recall skipped for this iteration")
