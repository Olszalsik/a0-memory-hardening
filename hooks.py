# Lifecycle hooks for the memory_hardening plugin (v0.7.0).
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("memory_hardening.hooks")


def install() -> None:
    """Plugin lifecycle hook: called once when the plugin is enabled.

    The Agent Zero v2.5 framework invokes this via
    `helpers.plugins.call_plugin_hook("memory_hardening", "install")`
    when the plugin is enabled. Renamed from `initialize` to match the
    framework's expected hook name; without this rename, the framework
    silently no-ops and our setup code never ran.
    """
    try:
        log.debug("memory_hardening plugin initialised")
    except Exception as e:
        log.warning("memory_hardening initialize failed: %s", e)


def uninstall() -> None:
    """Plugin lifecycle hook: called when the plugin is disabled or the
    process is stopping.

    Renamed from `shutdown` to match the framework's expected hook name;
    without this rename, the framework never called our cleanup code
    and process-global registries leaked across plugin enable/disable
    cycles (WatchdogRegistry recall tasks were never cancelled, embedding
    swaps never reset, etc.).

    Cancels every tracked recall task and resets process-global registries.
    """
    try:
        from usr.plugins.memory_hardening.helpers.watchdog import (
            WatchdogRegistry,
        )
        WatchdogRegistry.cancel_all()
    except Exception as e:
        log.warning("watchdog shutdown failed: %s", e)
    try:
        from usr.plugins.memory_hardening.helpers.memorize_canceller import (
            reset as mc_reset,
        )
        from usr.plugins.memory_hardening.helpers.embedding_swap import (
            reset as es_reset,
        )
        from usr.plugins.memory_hardening.helpers.rate_limiter import (
            reset as rl_reset,
        )
        from usr.plugins.memory_hardening.helpers.per_subdir_breaker import (
            reset as psb_reset,
        )
        from usr.plugins.memory_hardening.helpers.coroutine_guard import (
            reset as cg_reset,
        )
        from usr.plugins.memory_hardening.helpers.history_clamp import (
            reset as hc_reset,
        )
        from usr.plugins.memory_hardening.helpers.recall_wait_guard import (
            apply_recall_wait_guard as rwg_restore,
            reset_state as rwg_reset,
        )
        mc_reset()
        es_reset()
        rl_reset()
        psb_reset()
        cg_reset()
        hc_reset()
        # Restore the original RecallWait.execute (un-guard) and reset telemetry.
        try:
            rwg_restore(enabled=False)
            rwg_reset()
        except Exception:
            pass
    except Exception as e:
        log.warning("phase 3 shutdown failed: %s", e)

    # The AGENTS.md contract is that EVERY helper module exposes reset() so
    # uninstall clears its process-global state. That contract was not kept:
    # the list below was missing, so a disable/enable cycle left stale state
    # behind — most visibly an OPEN global circuit breaker (never reset), plus
    # per-subdir breaker events, the last FAISS hash cache, the quarantine
    # scan result, the index-GC access map and auto-recover's history/lock
    # dicts, all of which kept growing or reporting across cycles.
    #
    # Each helper is reset independently: one import failure must not skip the
    # rest, which is what the single shared try/except above used to allow.
    for module_name, attr in (
        ("telemetry", "reset"),
        ("circuit_breaker", "reset_instance"),
        ("recall_patch", "reset_state"),
        ("recall_gate", "reset"),
        ("faiss_health", "reset"),
        ("quarantine", "reset"),
        ("index_gc", "reset"),
        ("auto_recover", "reset"),
        ("memorize_watchdog", "reset"),
    ):
        try:
            mod = importlib.import_module(
                f"usr.plugins.memory_hardening.helpers.{module_name}"
            )
            fn = getattr(mod, attr, None)
            if callable(fn):
                fn()
            else:
                log.warning(
                    "uninstall: %s has no %s(); state may leak across "
                    "disable/enable",
                    module_name,
                    attr,
                )
        except Exception as e:
            log.warning("uninstall: %s reset failed: %s", module_name, e)
