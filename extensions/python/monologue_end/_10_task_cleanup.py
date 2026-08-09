# Per-context recall task cleanup.
#
# Runs at `monologue_end` (priority 10). When the agent's monologue
# ends, we cancel any watchdog-tracked recall task and await its
# termination with a hard cap. This prevents the "stuck event loop"
# failure mode where the task outlives the agent context.
#
# This is the only place where the watchdog actively cancels a task.
# All other extensions observe.
#
# Master kill switch: plugin config `hardening_enabled: false`.

from __future__ import annotations

import logging
from typing import Any, Optional

from agent import LoopData
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import (
    telemetry as tm,
    watchdog as wd,
)

log = logging.getLogger("memory_hardening.cleanup")


def _read_cfg(agent) -> Optional[dict]:
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class TaskCleanup(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs: Any) -> None:
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("watchdog_enabled", True):
            return
        if self.agent is None:
            return

        await_cap = float(cfg.get("watchdog_await_cap_sec", 60.0))
        try:
            completed = await wd.WatchdogRegistry.cleanup(
                self.agent, await_cap_sec=await_cap
            )
        except Exception as e:
            log.debug("watchdog cleanup raised: %s", e)
            return

        if not completed:
            tm.record_stuck_task_cancelled()
            log.warning(
                "recall task did not stop within %.0fs; force-cancelled",
                await_cap,
            )
