# GET /api/plugins/memory_hardening/stats (v0.5.3)
from __future__ import annotations
from typing import Any, Dict
from helpers.api import ApiHandler, Request

from usr.plugins.memory_hardening.helpers import (
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
    recall_wait_guard as rwg,
    telemetry as tm,
    watchdog as wd,
)


class Stats(ApiHandler):
    @staticmethod
    def get_methods():
        return ["GET", "POST"]

    async def process(self, input: Dict[str, Any], request: Request) -> Dict[str, Any]:
        # v0.5.3 fix: the framework calls process(input_data, request) and
        # json-dumps a returned dict. The old (request, response) signature
        # plus response.set_body() raised AttributeError on every call, so
        # this endpoint (and the WebUI dashboard) always returned 500.
        try:
            from helpers.plugins import get_plugin_config
            cfg = get_plugin_config("memory_hardening") or {}
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
            # v0.6.0 — quarantine scan snapshot (last_scan was never
            # previously exposed, so /stats always showed null)
            "quarantine": qu.snapshot(),
            # Phase 3 fields
            "rate_limiter": rl.snapshot(),
            "per_subdir_breaker": psb.snapshot(),
            "memorize_canceller": mc.snapshot(),
            "embedding_swap": es.snapshot(),
            # Phase 4 (2026-07-19) — coroutine lifecycle hygiene
            "coroutine_guard": {
                "ticks": cg.get_tick_snapshot(),
            },
            # v0.3.2 (2026-07-26) — recall method patch for _memory v1.2.0 regression
            "recall_patch": rp.get_state(),
            # v0.5.2 (2026-08-26) — recall-wait TimeoutError guard
            "recall_wait_guard": rwg.get_state(),
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
                # Phase 3 defaults align with default_config.yaml (recommended: ON)
                "per_subdir_breaker_enabled": cfg.get("per_subdir_breaker_enabled", True),
                "memorize_hard_cancel_enabled": cfg.get("memorize_hard_cancel_enabled", True),
                "quarantine_enabled": cfg.get("quarantine_enabled", False),
                "embedding_swap_enabled": cfg.get("embedding_swap_enabled", False),
                # v0.3.2
                "recall_patch_enabled": cfg.get("recall_patch_enabled", True),
                # v0.5.2
                "recall_wait_guard_enabled": cfg.get("recall_wait_guard_enabled", True),
                # v0.5.0
                "history_clamp_enabled": cfg.get("history_clamp_enabled", True),
            },
        }
        return payload
