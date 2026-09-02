# Memorize task watchdog (Phase 2). See helpers/memorize_watchdog.py for
# the v0.5.4 redesign notes.
#
# On the first invocation this extension resolves the framework's ACTUAL
# upstream memorize classes (MemorizeMemories / MemorizeSolutions at
# monologue_end priorities 50/51) through the synthetic-module resolver
# -- the same authoritative list the dispatcher iterates -- and wraps
# their `memorize` coroutine so the watchdog brackets the real
# memorize work instead of guessing from monologue timestamps.
#
# After the wrap is installed, every monologue_end checks whether the
# previous turn's memorize run is STILL in flight and genuinely over
# the soft cap / hard warn threshold (one warning per run).
from __future__ import annotations

import logging
from typing import Any, Optional

from agent import LoopData
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import (
    memorize_watchdog as mw,
    telemetry as tm,
)

log = logging.getLogger("memory_hardening.memorize_watchdog")

# The upstream extension classes whose background `memorize` coroutine we
# time. (class name, module basename suffix, canonical import fallback)
_TARGETS = (
    (
        "MemorizeMemories",
        "_50_memorize_fragments",
        "plugins._memory.extensions.python.monologue_end._50_memorize_fragments",
    ),
    (
        "MemorizeSolutions",
        "_51_memorize_solutions",
        "plugins._memory.extensions.python.monologue_end._51_memorize_solutions",
    ),
)

_installed = False


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


def _agent_id(agent):
    if agent is None:
        return None
    try:
        ctx = getattr(agent, "context", None)
        cid = getattr(ctx, "id", None) if ctx else None
        if cid:
            return str(cid)
    except Exception:
        pass
    return None


def _wrap_class(cls, hard_warn_default: float) -> bool:
    """Wrap cls.memorize with real-duration begin/end. Idempotent per
    class. Returns True when the wrap was applied on this call."""
    if getattr(cls, "_mh_memorize_wrapped", False):
        return False

    original = cls.memorize

    async def timed_memorize(self, *args, **kwargs):
        mw.MemorizeWatchdogRegistry.begin(self.agent, phase=type(self).__name__)
        try:
            return await original(self, *args, **kwargs)
        finally:
            duration = mw.MemorizeWatchdogRegistry.end(self.agent)
            if duration is not None:
                log.debug("memorize run finished in %.1fs (%s)", duration,
                          type(self).__name__)
                try:
                    cfg = _read_cfg(self.agent) or {}
                    hard_warn = float(cfg.get(
                        "memorize_watchdog_hard_warn_sec", hard_warn_default))
                except Exception:
                    hard_warn = hard_warn_default
                if duration > hard_warn:
                    tm.record_health_warning()
                    log.warning(
                        "memorize run over hard warn threshold: %.0fs (%s)",
                        duration, type(self).__name__,
                    )

    cls.memorize = timed_memorize
    cls._mh_memorize_wrapped = True
    return True


def _install_wraps(agent) -> None:
    global _installed
    if _installed:
        return
    try:
        from usr.plugins.memory_hardening.helpers.extension_class import (
            resolve_extension_class,
        )

        wrapped_any = False
        for class_name, module_suffix, canonical in _TARGETS:
            cls = resolve_extension_class(
                agent, "monologue_end", class_name, module_suffix, canonical
            )
            if cls is None:
                log.debug("memorize watchdog: %s not resolvable", class_name)
                continue
            if _wrap_class(cls, 300.0):
                wrapped_any = True
                log.debug("memorize watchdog: wrapped %s.memorize", class_name)
        if wrapped_any:
            _installed = True
    except Exception as e:
        log.debug("memorize watchdog install failed: %s", e)


class MemorizeWatchdog(Extension):
    async def execute(self, loop_data: LoopData = LoopData(), **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("memorize_watchdog_enabled", True):
            return
        if self.agent is None:
            return

        _install_wraps(self.agent)

        soft_cap = float(cfg.get("memorize_watchdog_soft_cap_sec", 120.0))
        hard_warn = float(cfg.get("memorize_watchdog_hard_warn_sec", 300.0))

        try:
            status = mw.MemorizeWatchdogRegistry.check(
                self.agent, soft_cap_sec=soft_cap, hard_warn_sec=hard_warn
            )
            if status is not None:
                tm.record_health_warning()
                log.warning(
                    "memorize_watchdog[%s] agent=%s phase=%s elapsed=%.0fs cap=%.0fs",
                    status.get("level"),
                    _agent_id(self.agent) or "?",
                    status.get("phase"),
                    status.get("elapsed_sec", 0),
                    hard_warn if status.get("level") == "hard" else soft_cap,
                )
        except Exception as e:
            log.debug("check failed: %s", e)