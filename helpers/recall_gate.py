# Recall gate — makes the Phase-2/3 breakers actually prevent a FAISS call.
#
# WHY THIS EXISTS
# ---------------
# `_20_circuit_breaker.py`, `_30_rate_limiter.py` and
# `_40_per_subdir_breaker.py` run at `message_loop_start` and, when their
# breaker says "skip", only set a flag in `loop_data.params_temporary`:
#
#     _memory_breaker_open / _memory_rate_limited / _memory_subdir_breaker_open
#
# Nothing ever read those keys. `_memory`'s own `_50_recall_memories`
# therefore built and awaited the recall task on every single iteration, so
# the breakers never prevented a FAISS call. The rate limiter was worse than
# inert: it CONSUMED a token and returned False, and the recall proceeded
# anyway -- so it throttled toward permanently refusing while never reducing
# load.
#
# HOW IT WORKS
# ------------
# The seam is `RecallMemories.search_memories`, the coroutine the upstream
# wraps in `asyncio.wait_for(..., timeout=30)` inside `search_and_cache`.
# Returning `{}` from it makes the whole recall a cheap no-op: no embedding
# call, no `search_similarity_threshold`, no FAISS handle.
#
# The class is resolved through `extension_class.resolve_extension_class`,
# i.e. the exact cached class list the dispatcher iterates. That is not
# optional: the framework loads extension files as SYNTHETIC modules named
# after the file basename, so patching the class obtained from a canonical
# `plugins._memory...` dotted import patches a phantom the dispatcher never
# touches. This is the v0.5.3 bug class; see `helpers/extension_class.py`.
#
# Patching is done ONCE per class. The wrapper reads a live module-level
# gate state that the per-iteration extension updates, so opening and
# closing the gate costs one dict assignment rather than a re-patch, and a
# closed gate restores normal recall with no unwrapping step to get wrong.

from __future__ import annotations

import threading
from typing import Any, Optional

_GUARD_FLAG = "_mh_recall_gate_installed"
_state_lock = threading.Lock()

# Live gate state, keyed by the memory subdir the gate applies to ("*" = any).
# Rebuilt from scratch on every `set_state()` call by the extension, so a
# failed/short-circuited iteration can never leave a stale "skip" behind.
_gate: dict = {
    "skip": False,
    "reason": "",
    "subdir": "*",
    "generation": 0,
}

# Counters, surfaced on /stats.
counters: dict = {
    "skipped": 0,
    "installed": 0,
    "last_reason": "",
    "last_subdir": "",
}


def set_state(skip: bool, reason: str = "", subdir: str = "*") -> int:
    """Set the gate for this iteration. Returns the new generation.

    Called on EVERY `message_loop_prompts_after` iteration, before
    `_50_recall_memories`. Passing skip=False is the common case and must be
    free: it only bumps a generation counter.
    """
    with _state_lock:
        _gate["skip"] = bool(skip)
        _gate["reason"] = reason or ""
        _gate["subdir"] = subdir or "*"
        _gate["generation"] += 1
        if skip:
            counters["skipped"] += 1
            counters["last_reason"] = reason or ""
            counters["last_subdir"] = subdir or "*"
        return _gate["generation"]


def current_state() -> dict:
    with _state_lock:
        return dict(_gate)


def reset() -> None:
    """Clear the gate and all counters (called from hooks.uninstall)."""
    with _state_lock:
        _gate["skip"] = False
        _gate["reason"] = ""
        _gate["subdir"] = "*"
        _gate["generation"] += 1
        for k in counters:
            counters[k] = 0
        counters["last_reason"] = ""
        counters["last_subdir"] = ""


def _should_skip_now() -> tuple[bool, str]:
    with _state_lock:
        if not _gate["skip"]:
            return False, ""
        return True, str(_gate["reason"] or "")


def install_guard(agent: Any) -> dict:
    """Idempotently wrap `RecallMemories.search_memories`. Returns a status dict.

    Synchronous on purpose: there is nothing to await, and the per-iteration
    extension calls this on every loop. As a coroutine it would allocate one
    per iteration and require an await that buys nothing.

    `agent` is required: without it the class cannot be resolved through the
    dispatcher's per-agent cache, and falling back to a canonical import would
    reintroduce the phantom-patch bug.
    """
    if agent is None:
        return {"status": "no_agent"}

    try:
        from usr.plugins.memory_hardening.helpers import extension_class
    except Exception as exc:  # noqa: BLE001
        return {"status": "import_error", "error": repr(exc)}

    # resolve_extension_class(agent, extension_point, class_name,
    #                         module_suffix, canonical_path)
    # All five are required. Guarded because a signature change here must
    # degrade to "no gate" rather than propagate: this runs inside an
    # extension that sits in the agent's monologue path.
    try:
        cls = extension_class.resolve_extension_class(
            agent,
            "message_loop_prompts_after",
            "RecallMemories",
            "_50_recall_memories",
            "plugins._memory.extensions.python.message_loop_prompts_after"
            "._50_recall_memories",
        )
    except TypeError as exc:
        return {"status": "resolver_signature_error", "error": repr(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "resolver_error", "error": repr(exc)}

    if cls is None:
        return {"status": "class_not_found"}

    if getattr(cls, _GUARD_FLAG, False):
        return {"status": "already_installed", "module": cls.__module__}

    original = getattr(cls, "search_memories", None)
    if original is None:
        # The method is genuinely absent upstream; recall_patch's
        # `_05_recall_method_patch` owns adding it. Do not fabricate one here.
        return {"status": "no_search_memories"}

    async def guarded_search_memories(self, *args: Any, **kwargs: Any):
        skip, reason = _should_skip_now()
        if skip:
            # Empty result: the upstream treats this as "nothing found" and
            # skips the embedding + both similarity searches entirely.
            return {}
        return await original(self, *args, **kwargs)

    guarded_search_memories.__name__ = getattr(original, "__name__", "search_memories")
    guarded_search_memories.__doc__ = getattr(original, "__doc__", None)
    guarded_search_memories.__mh_original__ = original

    setattr(cls, "search_memories", guarded_search_memories)
    setattr(cls, _GUARD_FLAG, True)
    counters["installed"] += 1

    return {
        "status": "installed",
        "module": getattr(cls, "__module__", "?"),
        "class": getattr(cls, "__name__", "?"),
    }


def install_guard_safe(agent: Any) -> dict:
    """install_guard() that can never raise.

    This is the entry point extensions call. A resilience plugin that breaks
    the agent it is protecting is worse than no plugin: an exception out of
    `execute` propagates through `call_extensions_async` into
    `agent.monologue` and ends the turn. So every failure mode — including a
    future signature change in the resolver — must land here as a status dict.
    """
    try:
        return install_guard(agent)
    except Exception as exc:  # noqa: BLE001
        counters["install_errors"] = counters.get("install_errors", 0) + 1
        counters["last_error"] = repr(exc)
        return {"status": "error", "error": repr(exc)}
