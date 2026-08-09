# Per-iteration watchdog initialiser for memory recall tasks.
#
# Runs at `message_loop_start` (priority 10). On every loop iteration we:
#
# 1. Read the current agent's `_recall_memories_task` data slot (set by
#    the built-in `_memory` extension on iteration N where N % interval == 0).
# 2. If a task is present and not yet tracked, register it with the
#    watchdog. This is how we get visibility on the task immediately
#    after the _memory plugin creates it.
# 3. If a previously tracked task is now done, mark it completed so the
#    registry can garbage-collect it.
#
# This is purely observational - the _memory plugin still owns the
# task's lifecycle. We do not call .cancel() here; cancellation happens
# in the cleanup extension (`monologue_end/_10_task_cleanup.py`) and the
# periodic health probe (`job_loop/_30_memory_health.py`).
#
# Master kill switch: plugin config `hardening_enabled: false`.

from __future__ import annotations

import logging
from typing import Any, Optional

from agent import LoopData
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import watchdog as wd_helper

log = logging.getLogger("memory_hardening.ml_start")


def _read_cfg(agent) -> Optional[dict]:
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


def _agent_id(agent) -> Optional[str]:
    if agent is None:
        return None
    try:
        ctx = getattr(agent, "context", None)
        ctx_id = getattr(ctx, "id", None) if ctx else None
        if ctx_id:
            return str(ctx_id)
    except Exception:
        pass
    return None


class WatchdogInit(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs: Any) -> None:
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("watchdog_enabled", True):
            return
        if self.agent is None:
            return

        task = None
        try:
            task = self.agent.get_data(wd_helper.RECALL_TASK_KEY)
        except Exception as e:
            log.debug("get_data failed: %s", e)
            return

        if task is None:
            return

        # If we already have a watchdog for this context, check completion.
        existing = wd_helper.WatchdogRegistry.get(self.agent)
        if existing is not None:
            if existing.task is task and existing.task.done():
                wd_helper.WatchdogRegistry.mark_completed(self.agent)
            return

        # If the task is already done, no need to track.
        try:
            if task.done():
                return
        except Exception:
            return

        # New task - register it.
        iter_val = None
        try:
            iter_val = self.agent.get_data(wd_helper.RECALL_ITER_KEY)
        except Exception:
            pass

        hard_cap = float(cfg.get("watchdog_hard_cap_sec", 90.0))
        wd_helper.WatchdogRegistry.track(
            self.agent,
            task,
            hard_cap_sec=hard_cap,
            iteration=iter_val,
        )
        log.debug(
            "watchdog registered: agent=%s iter=%s hard_cap=%.0fs",
            _agent_id(self.agent),
            iter_val,
            hard_cap,
        )
