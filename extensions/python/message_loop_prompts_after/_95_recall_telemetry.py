# Recall outcome observer.
#
# Runs at `message_loop_prompts_after` (priority 95) - after the built-in
# `_memory._91_recall_wait` has already awaited the recall task. We read
# the task from agent data, inspect its outcome, and:
#
# 1. Record the outcome (success / no_results / failed / timeout) in the
#    in-memory telemetry store.
# 2. Feed the outcome into the circuit breaker so it can learn.
# 3. Mark the watchdog's task as completed so the registry can drop it.
# 4. If the recall was successful, record the latency in milliseconds.
#
# This extension is a pure observer - it does not cancel, modify, or
# re-await the task. All cancellation is owned by the cleanup extension
# and the health probe.
#
# Master kill switch: plugin config `hardening_enabled: false`.

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from agent import LoopData
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import (
    circuit_breaker as cb,
    index_gc as igc,
    telemetry as tm,
    watchdog as wd,
)

log = logging.getLogger("memory_hardening.recall_telemetry")


def _recall_subdir(agent) -> str:
    """Mirror _get_subdir() from the message_loop_start extensions: the
    FAISS subdir this recall is serving, used to mark the index cache hot
    for index_gc."""
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


class RecallTelemetry(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs: Any) -> None:
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if self.agent is None:
            return

        if not cfg.get("telemetry_enabled", True) and not cfg.get("breaker_enabled", True):
            return

        # Mark this subdir's index as in use so index_gc only evicts
        # indexes that have actually gone stale (v0.5.3).
        try:
            igc.touch(_recall_subdir(self.agent))
        except Exception:
            pass

        # Pull the task back out of agent data
        task = None
        try:
            task = self.agent.get_data(wd.RECALL_TASK_KEY)
        except Exception as e:
            log.debug("get_data failed: %s", e)
            return

        wd_record = wd.WatchdogRegistry.get(self.agent)
        if task is None:
            # Either recall was not triggered on this iteration, or it
            # finished and the _memory plugin cleared the data slot.
            if wd_record is not None and wd_record.completed:
                wd.WatchdogRegistry.mark_completed(self.agent)
            return

        # Inspect the task state
        outcome = "success"
        latency_ms = None
        error_msg = None
        try:
            if not task.done():
                # v0.5.3 fix: with _memory's delayed-recall mode,
                # _91_recall_wait intentionally leaves the task pending on
                # the iteration that created it. That is healthy, not a
                # failure -- record nothing here and let the outcome be
                # picked up on the iteration where _91 actually awaits it.
                return
            else:
                # v0.5.3 fix: _memory never clears the task slot, so a done
                # task would otherwise be re-inspected on every subsequent
                # iteration, re-recording the outcome with ever-growing
                # latency. Only record the first inspection.
                if wd_record is not None and wd_record.completed:
                    return
                if wd_record is not None:
                    latency_ms = max(0.0, (time.time() - wd_record.started_at) * 1000.0)
                try:
                    exc = task.exception()
                except asyncio.CancelledError:
                    outcome = "failed"
                    error_msg = "task cancelled"
                    exc = None
                if exc is not None and outcome == "success":
                    if isinstance(exc, TimeoutError) or "TimeoutError" in repr(exc):
                        outcome = "timeout"
                    else:
                        outcome = "failed"
                    error_msg = repr(exc)[:512]
        except Exception as e:
            log.debug("task inspect failed: %s", e)
            outcome = "failed"
            error_msg = repr(e)[:512]

        # Did the recall return any results?
        try:
            extras = loop_data.extras_persistent
            has_results = bool(extras.get("memories") or extras.get("solutions"))
        except Exception:
            has_results = True
        if outcome == "success" and not has_results:
            outcome = "no_results"

        # Record telemetry
        if cfg.get("telemetry_enabled", True):
            tm.record_outcome(
                outcome=outcome,
                latency_ms=latency_ms,
                error=error_msg,
                agent_id=_agent_id(self.agent),
                max_samples=int(cfg.get("telemetry_max_samples", 100)),
            )

        # Feed the breaker
        if cfg.get("breaker_enabled", True):
            try:
                breaker = cb.get_instance(
                    window_sec=cfg.get("breaker_window_sec", 300.0),
                    failure_threshold=cfg.get("breaker_failure_threshold", 3),
                    cooldown_sec=cfg.get("breaker_cooldown_sec", 60.0),
                    max_entries=cfg.get("breaker_max_entries", 64),
                )
                breaker.record(outcome)
            except Exception as e:
                log.debug("breaker record failed: %s", e)

        # Mark watchdog completed so the registry can drop it
        wd.WatchdogRegistry.mark_completed(self.agent)

        if outcome in ("failed", "timeout"):
            log.warning(
                "memory recall %s (latency=%sms): %s",
                outcome,
                round(latency_ms, 1) if latency_ms is not None else "?",
                error_msg or "",
            )
        else:
            log.debug(
                "memory recall %s (latency=%sms)",
                outcome,
                round(latency_ms, 1) if latency_ms is not None else "?",
            )
