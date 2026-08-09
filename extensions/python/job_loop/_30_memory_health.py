# Periodic memory health probe. See README for full docstring.
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import (
    faiss_health as fh,
    telemetry as tm,
    watchdog as wd,
)

log = logging.getLogger("memory_hardening.health")

_last_run_at: float = 0.0
_last_faiss_check_at: float = 0.0


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class MemoryHealth(Extension):
    async def execute(self, **kwargs):
        global _last_run_at, _last_faiss_check_at
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("health_probe_enabled", True):
            return

        interval = float(cfg.get("health_probe_interval_sec", 120.0))
        now = time.time()
        if (now - _last_run_at) < interval:
            return
        _last_run_at = now

        stuck_threshold = float(cfg.get("health_stuck_task_sec", 45.0))
        cancelled = wd.WatchdogRegistry.reap_stale(stuck_threshold)
        if cancelled:
            for _ in range(cancelled):
                tm.record_stuck_task_cancelled()
            log.warning("health probe: cancelled %d stuck recall task(s)", cancelled)

        # FAISS health probe (Phase 2)
        faiss_interval = interval
        if cfg.get("faiss_health_enabled", True) and (now - _last_faiss_check_at) >= faiss_interval:
            _last_faiss_check_at = now
            try:
                min_size = int(cfg.get("faiss_health_min_size_bytes", 1024))
                max_age = int(cfg.get("faiss_health_max_age_days", 90))
                report = fh.probe_all(min_size_bytes=min_size, max_age_days=max_age)
                if report.get("warning_count", 0) > 0:
                    for r in report.get("results", []):
                        if r.get("warning"):
                            tm.record_health_warning()
                            log.warning(
                                "FAISS index warning: path=%s warn=%s",
                                r.get("path"), r.get("warning"),
                            )
            except Exception as e:
                log.debug("faiss_health probe failed: %s", e)

        snap = tm.snapshot()
        log.debug(
            "memory health: uptime=%.0fs recalls=%d timeouts=%d fails=%d skipped_br=%d cancelled=%d",
            snap["uptime_sec"],
            snap["counters"]["recall_started"],
            snap["counters"]["recall_timeout"],
            snap["counters"]["recall_failed"],
            snap["counters"]["recall_skipped_breaker"],
            snap["counters"]["stuck_tasks_cancelled"],
        )
