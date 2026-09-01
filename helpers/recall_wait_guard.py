"""Recall-wait TimeoutError guard.

Wraps ``RecallWait.execute`` (the built-in ``_memory`` plugin's
``_91_recall_wait.py``) so that an ``asyncio.TimeoutError`` raised by the
30-second recall budget (``asyncio.wait_for(..., timeout=SEARCH_TIMEOUT)``
in ``_50_recall_memories.py``) cannot propagate out of the message-loop
prompt phase and kill the agent monologue loop.

Why this is needed
------------------
``_50_recall_memories`` schedules the recall as::

    task = asyncio.create_task(
        asyncio.wait_for(self.search_memories(...), timeout=SEARCH_TIMEOUT)
    )

and ``_91_recall_wait`` later does ``await task``. When the 30s budget
fires, ``asyncio.wait_for`` cancels the inner coroutine and raises
``asyncio.TimeoutError``. The stock ``RecallWait.execute`` has no
try/except around ``await task``, so the exception propagates through
``prepare_prompt`` -> ``monologue`` and tears down the whole agent loop.
Historical logs show ~1268 such crashes.

This guard *wraps* (does not replace) the original ``execute`` so it
survives upstream changes to the method body. It is idempotent and
reversible: disable via ``recall_wait_guard_enabled: false`` in config
and the original method is restored.

Telemetry is exposed via ``get_state()`` for the ``/stats`` API endpoint,
alongside the existing ``recall_patch`` telemetry. The downstream
``_95_recall_telemetry`` observer still sees the timed-out task
(``task.exception()`` is the ``TimeoutError``) and feeds the circuit
breaker, so the breaker still learns to back off -- this guard only
keeps the loop alive long enough for that to happen.

Framework imports (``RecallWait``, ``LoopData``) are deferred to
function-call time so that this module can be imported in bare test
environments without the full Agent Zero stack.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("memory_hardening.recall_wait_guard")

STATE = {
    "applied": 0,
    "already_guarded": 0,
    "restored": 0,
    "timeouts_caught": 0,
    "errors_caught": 0,
    "import_errors": 0,
    "patch_attempts": 0,
    "last_status": "never_run",
    "last_error": "",
    "last_class_module": "",
}


def _snapshot() -> dict:
    return dict(STATE)


_GUARDED_FLAG = "_mh_wait_guarded"
_ORIGINAL_ATTR = "_mh_original_execute"


def apply_recall_wait_guard(enabled: bool = True, agent=None) -> dict:
    """Wrap ``RecallWait.execute`` so a recall timeout cannot kill the loop.

    Idempotent. When ``enabled`` is False and the guard is currently
    active, the original ``execute`` is restored. ``agent`` (optional)
    lets the resolver use the dispatcher's per-agent class list -- the
    same list ``call_extensions_async`` iterates, so the wrap lands on
    the class the framework actually instantiates (v0.5.2 wrapped the
    canonical-import phantom class and never fired live). Returns a
    telemetry snapshot.
    """
    STATE["patch_attempts"] += 1

    try:
        from usr.plugins.memory_hardening.helpers.extension_class import (
            resolve_extension_class,
        )

        RecallWait = resolve_extension_class(
            agent,
            "message_loop_prompts_after",
            "RecallWait",
            "_91_recall_wait",
            "plugins._memory.extensions.python.message_loop_prompts_after._91_recall_wait",
        )
    except Exception as e:
        STATE["import_errors"] += 1
        STATE["last_error"] = repr(e)[:512]
        STATE["last_status"] = "import_error"
        return _snapshot()

    if RecallWait is None:
        STATE["import_errors"] += 1
        STATE["last_status"] = "class_not_found"
        return _snapshot()

    STATE["last_class_module"] = str(getattr(RecallWait, "__module__", ""))

    try:
        from agent import LoopData
    except Exception:
        class LoopData:  # bare-env fallback: wrapper default only
            pass

    # --- disable / restore path -------------------------------------------
    if not enabled:
        if getattr(RecallWait, _GUARDED_FLAG, False):
            original = getattr(RecallWait, _ORIGINAL_ATTR, None)
            if original is not None:
                RecallWait.execute = original
                try:
                    delattr(RecallWait, _GUARDED_FLAG)
                except AttributeError:
                    pass
                try:
                    delattr(RecallWait, _ORIGINAL_ATTR)
                except AttributeError:
                    pass
                STATE["restored"] += 1
                STATE["last_status"] = "disabled_restored"
            else:
                STATE["last_status"] = "disabled_no_original"
        else:
            STATE["last_status"] = "disabled"
        return _snapshot()

    # --- already guarded --------------------------------------------------
    if getattr(RecallWait, _GUARDED_FLAG, False):
        STATE["already_guarded"] += 1
        STATE["last_status"] = "already_guarded"
        return _snapshot()

    original = RecallWait.execute

    async def safe_execute(self, loop_data=LoopData(), **kwargs):
        """Guarded wrapper around the upstream RecallWait.execute."""
        try:
            return await original(self, loop_data, **kwargs)
        except asyncio.CancelledError:
            # Never swallow loop-shutdown cancellation.
            raise
        except asyncio.TimeoutError:
            # The 30s recall budget (asyncio.wait_for in _50) fired. Memory
            # recall is best-effort -- it must not crash the agent loop.
            STATE["timeouts_caught"] += 1
            STATE["last_status"] = "timeout_caught"
            STATE["last_error"] = ""
            log.warning(
                "memory recall timed out (30s budget) -- guard caught "
                "TimeoutError, agent loop preserved"
            )
            try:
                self.agent.context.log.log(
                    type="warning",
                    heading="Memory recall timed out",
                    content=(
                        "Recall exceeded the 30s budget. The agent loop was "
                        "preserved by memory_hardening's recall-wait guard."
                    ),
                )
            except Exception:
                pass
            return
        except Exception as e:
            # Safety net: any other recall-path exception (FAISS error,
            # config glitch, etc.) is logged but not allowed to kill the
            # loop. The downstream telemetry observer still records the
            # task outcome and feeds the circuit breaker.
            STATE["errors_caught"] += 1
            STATE["last_error"] = repr(e)[:512]
            STATE["last_status"] = "error_caught"
            log.warning("memory recall raised (guard caught): %s", e)
            try:
                self.agent.context.log.log(
                    type="warning",
                    heading="Memory recall error (guarded)",
                    content=repr(e)[:300],
                )
            except Exception:
                pass
            return

    safe_execute.__wrapped__ = original
    safe_execute.__doc__ = getattr(original, "__doc__", "")
    safe_execute.__name__ = getattr(original, "__name__", "execute")

    RecallWait.execute = safe_execute
    setattr(RecallWait, _ORIGINAL_ATTR, original)
    setattr(RecallWait, _GUARDED_FLAG, True)

    STATE["applied"] += 1
    STATE["last_status"] = "applied"
    STATE["last_error"] = ""
    return _snapshot()


def get_state() -> dict:
    """Return current telemetry state for the /stats API."""
    return _snapshot()


def reset_state() -> dict:
    """Reset telemetry counters (for testing and the reset_breaker endpoint)."""
    for key in STATE:
        if isinstance(STATE[key], int):
            STATE[key] = 0
        elif key == "last_status":
            STATE[key] = "never_run"
        else:
            STATE[key] = ""
    return _snapshot()