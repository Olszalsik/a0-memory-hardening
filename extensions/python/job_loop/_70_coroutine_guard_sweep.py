# Periodic coroutine-hygiene sweep (Phase 4, 2026-07-19).
#
# Closes any unawaited coroutines that escaped _model_fallback's own
# close path, e.g. coroutines cancelled by user extension code that
# did its own asyncio.wait_for without going through
# `memory_hardening.helpers.coroutine_guard.close_inner_coro`.
#
# Runs once per minute. The scan is O(n) on the running task count,
# but at idle (no model calls) that count is small (websocket
# dispatcher, state monitor, a handful of plugin jobs).
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import (
    coroutine_guard as cg,
    telemetry as tm,
)

log = logging.getLogger("memory_hardening.coroutine_guard_sweep")

# Module-level throttle: we only sweep every 60 seconds regardless of
# how often the framework calls us. The framework invokes the
# job_loop extension point on its own schedule; throttling here keeps
# the sweep predictable.
_LAST_SWEEP_AT: float = 0.0
_SWEEP_INTERVAL_S: float = 60.0


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return {}


class CoroutineGuardSweep(Extension):
    """Periodic best-effort coroutine hygiene sweep.

    Disabled when ``hardening_enabled`` is False OR when
    ``coroutine_guard_enabled`` (a new Phase-4 key) is explicitly
    False. Defaults to enabled when hardening is on.
    """

    def execute(self, **kwargs: Any) -> None:
        global _LAST_SWEEP_AT

        try:
            cfg = _read_cfg(self.agent)
        except Exception:
            cfg = {}

        if not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("coroutine_guard_enabled", True):
            return

        now = time.monotonic()
        if (now - _LAST_SWEEP_AT) < _SWEEP_INTERVAL_S:
            return
        _LAST_SWEEP_AT = now

        try:
            closed = cg.scan_unawaited_coroutines()
        except Exception as e:
            log.debug("coroutine_guard sweep failed: %s", e)
            return

        if closed > 0:
            log.info("coroutine_guard sweep closed %d leaked coroutine(s)", closed)
            try:
                tm.record(
                    "info",
                    f"coroutine_guard sweep closed {closed} leaked coroutine(s)",
                )
            except Exception:
                pass
