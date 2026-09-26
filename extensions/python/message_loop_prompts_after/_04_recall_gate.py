"""Recall gate: turn the Phase-2/3 breaker verdicts into a skipped FAISS call.

Priority 04 — it must run AFTER the `message_loop_start` gates that set the
flags (priorities 20/30/40) and BEFORE `_50_recall_memories` (50), which is
where the upstream builds the recall task. `_05_recall_method_patch` (5) and
`_06_recall_wait_guard` (6) follow; ordering against those two does not matter
because they patch different methods.

Before v0.7.0 nothing consumed the three `params_temporary` flags, so the
circuit breaker, the rate limiter and the per-subdir breaker all recorded
telemetry and changed nothing: `_50_recall_memories` built and awaited the
recall task every iteration regardless. The rate limiter was the worst case —
it consumed a token, reported "limited", and the recall ran anyway, so it
walked itself toward permanently refusing without ever shedding load.

FAIL-SAFE CONTRACT (learned the hard way on 2026-09-26)
------------------------------------------------------
`execute()` below is wrapped end to end. An exception raised here propagates
through `call_extensions_async` into `agent.monologue` and KILLS the agent's
turn — a resilience plugin must never be able to do that. A first cut of this
extension referenced `psb.DEFAULT_ENABLED` as a bare attribute and stalled a
live agent: extension files are re-read on every dispatch, but imported helper
modules stay cached in `sys.modules` for the process lifetime, so new
extension code can run against a stale helper that lacks the new symbol.

Two rules follow, and they apply to every extension in this plugin:
  1. Read cross-module values with `getattr(obj, "NAME", <fallback>)`.
  2. Wrap the whole `execute()` body in try/except.
"""

from __future__ import annotations

import logging

from helpers.extension import Extension
from usr.plugins.memory_hardening.helpers import recall_gate

log = logging.getLogger("memory_hardening.recall_gate")

GLOBAL_FLAG = "_memory_breaker_open"
RATE_FLAG = "_memory_rate_limited"
SUBDIR_FLAG = "_memory_subdir_breaker_open"

# Ordered most- to least- specific so the operator sees the binding reason.
_REASONS = (
    (GLOBAL_FLAG, "circuit_breaker_open"),
    (RATE_FLAG, "rate_limited"),
    (SUBDIR_FLAG, "per_subdir_breaker_open"),
)


class RecallGate(Extension):
    async def execute(self, **kwargs) -> None:
        try:
            await self._run(**kwargs)
        except Exception as exc:  # noqa: BLE001 - must never kill the monologue
            # Close the gate defensively. If we cannot evaluate the flags we
            # must not leave the previous iteration's "skip" latched, or
            # recall would stay off for the rest of the monologue.
            try:
                recall_gate.set_state(False)
            except Exception:
                pass
            log.warning("recall gate error (no-op): %r", exc)

    async def _run(self, **kwargs) -> None:
        agent = getattr(self, "agent", None)
        if agent is None:
            recall_gate.set_state(False)
            return

        loop_data = kwargs.get("loop_data")
        params = getattr(loop_data, "params_temporary", None)

        # Always clear first. A flag absent from this iteration's
        # params_temporary must not leave the previous iteration's "skip"
        # latched, or recall would stay off forever after one breaker trip.
        if not isinstance(params, dict):
            recall_gate.set_state(False)
            return

        skip, reason = False, ""
        for flag, label in _REASONS:
            if params.get(flag):
                skip, reason = True, label
                break

        recall_gate.set_state(skip, reason)

        # Install the wrapper unconditionally, before `_50_recall_memories`
        # builds its task. It is idempotent and short-circuits on a class
        # attribute check, so this costs one attribute read per iteration —
        # and it means the first breaker trip does not race the dispatcher.
        # install_guard_safe() cannot raise.
        recall_gate.install_guard_safe(agent)
