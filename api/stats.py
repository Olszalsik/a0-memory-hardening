# GET /api/plugins/memory_hardening/stats (v0.5.0)
from __future__ import annotations
import json
from helpers.api import ApiHandler, Request, Response

from usr.plugins.memory_hardening.helpers import (
    adaptive_interval as ai,
    auto_recover as ar,
    circuit_breaker as cb,
    coroutine_guard as cg,
    embedding_swap as es,
    faiss_health as fh,
    history_clamp as hc,
    index_gc as igc,
    memorize_canceller as mc,
    memorize_watchdog as mw,
    per_subdir_breaker as psb,
    rate_limiter as rl,
    quarantine as qu,
    recall_patch as rp,
    telemetry as tm,
    watchdog as wd,
)


class Stats(ApiHandler):
    @staticmethod
    def get_methods():
        return ["GET", "POST"]

    async def process(self, request: Request, response: Response):
        try:
            cfg = self.plugin.get_config(self.agent) if hasattr(self, "plugin") else {}
        except Exception:
            cfg = {}
        try:
            breaker = cb.get_instance(
                window_sec=cfg.get("breaker_window_sec", 300.0),
                failure_threshold=cfg.get("breaker_failure_threshold", 3),
                cooldown_sec=cfg.get("breaker_cooldown_sec", 60.0),
                max_entries=cfg.get("breaker_max_entries", 64),
            )
            breaker_state = breaker.state()
        except Exception as e:
            breaker_state = {"error": str(e)}

        faiss_report = None
        if cfg.get("faiss_health_enabled", True):
            try:
                faiss_report = fh.probe_all(
                    min_size_bytes=int(cfg.get("faiss_health_min_size_bytes", 1024)),
                    max_age_days=int(cfg.get("faiss_health_max_age_days", 90)),
                )
            except Exception as e:
                faiss_report = {"error": str(e)}

        payload = {
            "telemetry": tm.snapshot(),
            "breaker": breaker_state,
            "watchdogs": wd.WatchdogRegistry.snapshot(),
            "memorize_watchdogs": mw.MemorizeWatchdogRegistry.snapshot(),
            "index_gc": igc.snapshot(),
            "faiss_health": faiss_report,
            "auto_recover": ar.history(),
            # Phase 3 fields
            "rate_limiter": rl.snapshot(),
            "per_subdir_breaker": psb.snapshot(),
            "adaptive_interval": ai.snapshot(),
            "memorize_canceller": mc.snapshot(),
            "embedding_swap": es.snapshot(),
            # Phase 4 (2026-07-19) — coroutine lifecycle hygiene
            "coroutine_guard": {
                "ticks": cg.get_tick_snapshot(),
            },
            # v0.3.2 (2026-07-26) — recall method patch for _memory v1.2.0 regression
            "recall_patch": rp.get_state(),
            # v0.5.0 (2026-08-10) — memory history clamp (merged from _memory_resilience)
            "history_clamp": hc.get_state(),
            "config": {
                "hardening_enabled": cfg.get("hardening_enabled", True),
                "watchdog_enabled": cfg.get("watchdog_enabled", True),
                "breaker_enabled": cfg.get("breaker_enabled", True),
                "telemetry_enabled": cfg.get("telemetry_enabled", True),
                "health_probe_enabled": cfg.get("health_probe_enabled", True),
                "faiss_health_enabled": cfg.get("faiss_health_enabled", True),
                "agent_notice_enabled": cfg.get("agent_notice_enabled", False),
                "memorize_watchdog_enabled": cfg.get("memorize_watchdog_enabled", True),
                "index_gc_enabled": cfg.get("index_gc_enabled", True),
                "auto_recover_enabled": cfg.get("auto_recover_enabled", True),
                "dashboard_enabled": cfg.get("dashboard_enabled", True),
                # Phase 3
                "rate_limiter_enabled": cfg.get("rate_limiter_enabled", True),
                "per_subdir_breaker_enabled": cfg.get("per_subdir_breaker_enabled", False),
                "adaptive_interval_enabled": cfg.get("adaptive_interval_enabled", False),
                "memorize_hard_cancel_enabled": cfg.get("memorize_hard_cancel_enabled", False),
                "quarantine_enabled": cfg.get("quarantine_enabled", False),
                "embedding_swap_enabled": cfg.get("embedding_swap_enabled", False),
                # v0.3.2
                "recall_patch_enabled": cfg.get("recall_patch_enabled", True),
                # v0.5.0
                "history_clamp_enabled": cfg.get("history_clamp_enabled", True),
            },
        }
        response.set_body(json.dumps(payload, indent=2))
        return response
