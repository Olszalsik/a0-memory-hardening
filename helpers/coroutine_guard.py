"""Coroutine lifecycle hygiene (memory_hardening — Phase 4, 2026-07-19).

WHY THIS EXISTS
---------------
Across an extended utility-model outage in continuous-fallback mode,
``usr.plugins._model_fallback.fallback._patched_call_utility_model`` and
its chat-model twin each do ``await asyncio.wait_for(coro, timeout=...)``.
When the timeout fires, ``asyncio.wait_for`` cancels the outer task, but
the litellm transport (helpers/litellm_transport.py:239) constructs a
chain of inner coroutines that, in the cancellation path, are NEVER
awaited. Python then logs:

    RuntimeWarning: coroutine 'OpenAIChatCompletion.acompletion'
        was never awaited

This is a real leak, not just noise:

* Each unawaited coroutine holds a frame object with a reference to the
  local variables of the request. Those variables include the request
  payload, the response iterator, the httpx transport, and the openai
  client. None of them can be garbage-collected until the coroutine is
  either awaited or closed.
* The HTTP connection stays open until the OS-level keepalive timer
  expires. In a 4-minute outage with 5 timeouts, that's 5 half-open
  HTTP connections pinning the openai client's connection pool.
* Eventually the warnings and the accumulated frames pollute the
  ``tracemalloc`` reports and any subsequent ``asyncio.all_tasks()``
  call, making future diagnostics harder.

WHAT THIS DOES
--------------
Two complementary pieces:

1. A ``close_inner_coro(coro)`` helper for the user plugins (currently
   ``_model_fallback``) to call inside their ``except
   (asyncio.TimeoutError, asyncio.CancelledError)`` blocks. Calling
   ``.close()`` on a never-awaited coroutine releases its frame
   immediately, eliminating the warning and the leak.

2. An ``on_long_sleep_tick(remaining_seconds)`` callback that
   ``_model_fallback.fallback._yielding_sleep`` invokes every 2s
   during the long cycle sleep. We record the tick so observability
   tools (the WebUI dashboard, the
   ``/api/plugins/memory_hardening/stats`` endpoint) can show that
   the loop is alive even when a model cascade is sleeping for
   5 minutes. This is the diagnostic half of the WebSocket-disconnect
   investigation — when the user sees the UI freeze, they can now
   ask "is the loop still ticking?" and get a yes/no.

A periodic ``scan_unawaited_coroutines()`` helper is provided so a
``job_loop`` extension can call it (e.g. once a minute) and clean up
any coroutines that escaped from paths OTHER than ``_model_fallback``
(e.g. cancelled user extensions, an interrupted
``_extension_import_guard`` import). This is best-effort: closing
coroutines the framework is still using is dangerous, so we only
close coroutines that match a name prefix the framework never holds
across the event loop boundary (``OpenAIChatCompletion``,
``HTTPClient``, etc.).

DESIGN
------
* No monkey-patching. ``_model_fallback`` imports ``close_inner_coro``
  and ``on_long_sleep_tick`` lazily, so the plugin works even when
  ``memory_hardening`` is disabled (the import path raises, the
  ``_model_fallback`` call site catches it as a no-op).
* All process-global state lives in module-level dicts; ``reset()``
  clears them on plugin uninstall, matching the convention in
  every other helper in this plugin.
* No asyncio tasks are created here. The helpers are pure
  functions called from the user plugin's existing code path.
"""
from __future__ import annotations

import time
from typing import Optional

# Process-global tick telemetry. Keyed by the source plugin (e.g.
# "_model_fallback" for the cascade ticks) so future plugins can
# register their own tick streams without colliding.
#
# Value layout::
#     {
#         "last_tick_at": float,   # time.monotonic() of the most recent tick
#         "tick_count": int,       # how many ticks we've recorded
#         "longest_remaining_s": float,  # largest remaining_s we observed
#     }
_tick_state: dict = {}

# A coroutine that we believe is safe to .close() has a name whose
# prefix matches one of these. ``OpenAIChatCompletion.acompletion``
# is the canonical example; if we see it on the floor (cancelled by
# wait_for and never awaited), closing it releases the response
# stream, the httpx transport, and the openai client.
#
# IMPORTANT: this list is intentionally short and conservative.
# Closing the wrong coroutine can corrupt the framework's task
# tree. If you're tempted to add a prefix, ASK FIRST: would closing
# a coroutine with this prefix while the framework is still
# awaiting it cause a use-after-close?
_CLOSEABLE_CORO_NAME_PREFIXES: tuple = (
    "OpenAIChatCompletion",
    "OpenAIResponses",
    "ChatCompletion",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def close_inner_coro(coro) -> bool:
    """Close a coroutine that was cancelled by ``asyncio.wait_for`` and
    therefore never awaited.

    Returns True if the coroutine was successfully closed, False if
    we left it alone (already awaited, not closeable by name, or
    still has a live frame being awaited by something else).

    Safe to call from inside an ``except
    (asyncio.TimeoutError, asyncio.CancelledError)`` block. Idempotent
    (closing a closed coroutine is a no-op).
    """
    try:
        if coro is None:
            return False
        # A coroutine has a ``cr_frame`` attribute. If the coroutine
        # is mid-execution (cr_running is True), don't close — the
        # event loop is still driving it. If the frame is None
        # (already finished or already closed), nothing to do.
        cr_frame = getattr(coro, "cr_frame", None)
        cr_running = getattr(coro, "cr_running", False)
        if cr_running:
            return False
        if cr_frame is None:
            return False
        # Only close coroutines whose qualname matches our allowlist.
        # This is a defensive measure: in principle ``close()`` is
        # safe on any never-awaited coroutine, but if a bug ever
        # routed a live framework coroutine through this path, we'd
        # rather no-op than corrupt the task tree.
        qualname = (
            getattr(coro, "__qualname__", "")
            or getattr(coro, "__name__", "")
            or ""
        )
        if not any(qualname.startswith(p) for p in _CLOSEABLE_CORO_NAME_PREFIXES):
            return False
        coro.close()
        return True
    except Exception:
        # If anything goes wrong, the coroutine will be GC'd
        # eventually. Don't propagate — this is a hygiene helper,
        # not a correctness helper.
        return False


def on_long_sleep_tick(remaining_seconds: float) -> None:
    """Called every 2s by ``_model_fallback`` during a long cycle sleep.

    Records that the loop is alive. Pure observability — no
    side effects, no asyncio tasks, no I/O.
    """
    try:
        now = time.monotonic()
        state = _tick_state.setdefault(
            "_model_fallback",
            {"last_tick_at": 0.0, "tick_count": 0, "longest_remaining_s": 0.0},
        )
        state["last_tick_at"] = now
        state["tick_count"] += 1
        if remaining_seconds > state["longest_remaining_s"]:
            state["longest_remaining_s"] = float(remaining_seconds)
    except Exception:
        pass


def get_tick_snapshot() -> dict:
    """Return the current tick state for ``/stats`` and the WebUI."""
    snapshot: dict = {}
    for source, state in _tick_state.items():
        snapshot[source] = {
            "last_tick_age_sec": (
                time.monotonic() - state["last_tick_at"]
                if state["last_tick_at"] > 0
                else None
            ),
            "tick_count": state["tick_count"],
            "longest_remaining_s": state["longest_remaining_s"],
        }
    return snapshot


def scan_unawaited_coroutines() -> int:
    """Best-effort: scan ``asyncio.all_tasks()`` for cancelled
    coroutines with closeable names that we can .close() safely.

    Returns the number of coroutines we successfully closed.

    This is a safety net for coroutines that escape
    ``_model_fallback``'s close path — e.g. a user extension that
    did its own ``asyncio.wait_for`` without our hygiene, or a
    cancelled ``OpenAIResponses`` call from the Responses transport
    path.

    Call from a ``job_loop`` extension once a minute, NOT from
    inside a hot path. Iterating all tasks is O(n) on the running
    coroutine count.
    """
    try:
        import asyncio

        closed = 0
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (sync context); nothing to do.
            return 0
        for task in asyncio.all_tasks(loop=running_loop):
            coro = task.get_coro() if hasattr(task, "get_coro") else None
            if coro is None:
                continue
            if task.done():
                # Done tasks already cleaned up their coroutines.
                continue
            if task.cancelling() == 0 and not task.cancelled():
                # Active task, not being cancelled. Leave alone.
                continue
            if close_inner_coro(coro):
                closed += 1
        return closed
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Plugin lifecycle — match the convention in every other helper here
# ---------------------------------------------------------------------------

def reset() -> None:
    """Clear process-global state. Called from ``hooks.uninstall``."""
    _tick_state.clear()
