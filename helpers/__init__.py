"""
Helpers for the memory_hardening plugin.

This package contains the shared, framework-agnostic building blocks
used by the extension hooks and API endpoints:

- telemetry:  in-memory metrics store for recall outcomes
- watchdog:   per-agent registry of recall tasks with hard cap
- circuit_breaker: sliding-window failure counter with skip cooldown
- coroutine_guard (Phase 4, 2026-07-19): closes leaked litellm
  coroutines on ``asyncio.wait_for`` cancellation paths; tick
  callback consumed by ``_model_fallback`` so the WebUI dashboard
  can show that the event loop is still alive during long cycle
  sleeps.

All helpers are pure Python (no asyncio loops, no framework imports)
so they are testable in isolation and side-effect free at import time.
"""

from __future__ import annotations

__all__ = [
    "telemetry",
    "watchdog",
    "circuit_breaker",
    "coroutine_guard",
]
