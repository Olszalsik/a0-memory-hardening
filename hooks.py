# Lifecycle hooks for the memory_hardening plugin (v0.6.0).
from __future__ import annotations

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
