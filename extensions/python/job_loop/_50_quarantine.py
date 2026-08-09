# Cross-chat quarantine (Phase 3). Periodically archives stale indexes.
from __future__ import annotations
import logging
import time
from typing import Any, Optional
from helpers.extension import Extension
from helpers import plugins
from usr.plugins.memory_hardening.helpers import quarantine as qu, telemetry as tm

log = logging.getLogger("memory_hardening.quarantine")

_last_run_at: float = 0.0


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


class QuarantineScan(Extension):
    async def execute(self, **kwargs):
        global _last_run_at
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("quarantine_enabled", False):
            return
        interval = float(cfg.get("quarantine_scan_interval_sec", 3600))
        now = time.time()
        if (now - _last_run_at) < interval:
            return
        _last_run_at = now
        try:
            result = qu.scan(
                max_age_days=int(cfg.get("quarantine_max_age_days", 90)),
                archive_dir=cfg.get("quarantine_archive_dir", "tmp/memory/archive"),
            )
            if result.get("candidates"):
                tm.record_health_warning()
                log.info("quarantine: %d stale indexes, manifest=%s",
                         len(result["candidates"]), result.get("manifest"))
        except Exception as e:
            log.warning("quarantine scan failed: %s", e)
