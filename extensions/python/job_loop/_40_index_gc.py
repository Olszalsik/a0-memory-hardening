# Index cache GC extension (Phase 2). See README for full docstring.
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import index_gc as igc

log = logging.getLogger("memory_hardening.index_gc")


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


_last_run_at: float = 0.0


class IndexGC(Extension):
    async def execute(self, **kwargs):
        global _last_run_at
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("index_gc_enabled", True):
            return

        interval = float(cfg.get("index_gc_interval_sec", 300.0))
        now = time.time()
        if (now - _last_run_at) < interval:
            return
        _last_run_at = now

        idle_min = float(cfg.get("index_gc_idle_min", 30.0))
        max_entries = int(cfg.get("index_gc_max_entries", 16))

        try:
            res = igc.gc_once(idle_min=idle_min, max_entries=max_entries)
            igc.mark_gc_run()
            if res.get("evicted_idle") or res.get("evicted_overflow"):
                log.info(
                    "index_gc done: inspected=%d idle_evicted=%d overflow_evicted=%d remaining=%d",
                    res.get("inspected"),
                    res.get("evicted_idle"),
                    res.get("evicted_overflow"),
                    res.get("remaining"),
                )
        except Exception as e:
            log.warning("index_gc pass failed: %s", e)
