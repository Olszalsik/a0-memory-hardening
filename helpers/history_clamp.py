"""Memory history clamp for utility-model memorize / solve calls.

Merged from the former ``_memory_resilience`` plugin (v1.0.0) into
``memory_hardening`` in v0.5.0. Both were memory patches for agent-zero;
the clamp is the resilience layer and now lives here so a single plugin
owns all memory protection. The standalone ``_memory_resilience`` plugin
was removed once its single extension folded in here.

Why this exists
---------------
The official ``plugins/_memory/.../monologue_end/_50_memorize_*.py``
extensions read ``MAX_MSGS_CHARS = 80000`` and truncate the local
``msgs_text`` variable before calling ``self.agent.call_utility_model``.
The 80000 budget is too generous for a 32k-context utility model and
causes ``ContextOverflow`` errors that the cascade can't recover from
in a single cycle (every utility candidate in the rotation returns the
same overflow).

The official code lives in ``plugins/_memory/`` and is part of
agent-zero's tracked files, so direct edits are overwritten on update.
This module hooks the same ``util_model_call_before`` extension point
that the cascade and the timeout-guard use, and clamps
``call_data["message"]`` when the system prompt looks like a
memory-recall / memorize / solve prompt.

Budget resolution
-----------------
The clamp budget is read in this order:

1. ``memory_hardening.history_clamp_max_chars_override`` from this
   plugin's config (per-agent / per-project / global). When present and
   a positive integer, the caller has explicitly chosen a different
   budget.
2. ``_model_fallback.memory_memorize_max_chars`` (default 50000) from
   the cascade plugin's config. This means a single setting governs
   both the utility-call timeout budget (used by the cascade) and the
   memory-recall budget (used here) -- both protect the same
   downstream constraint (the utility model's context window).
3. ``_DEFAULT_BUDGET`` (50000) when neither is available.

Prompt detection
----------------
The hook does not parse the system prompt. It matches on substring
presence of the well-known memory system-prompt file names. If a future
agent-zero version renames them, the substring match silently fails and
the clamp becomes a no-op for those prompts -- safe degradation, not a
crash.

Failure semantics
-----------------
A bug here (e.g. the budget is non-numeric, the config fetch raises)
must NOT crash the utility-model call. ``clamp()`` is wrapped in a
try/except that logs a debug line and leaves ``call_data`` unchanged.
The LLM call still runs with the slightly-too-long history; the
cascade still retries with the next model; the user sees a soft
degradation rather than a crash.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

_log = logging.getLogger("memory_hardening.history_clamp")

# Stable identifiers of the official memory system-prompt files. The
# official extensions call ``self.agent.read_prompt(<name>)`` for each.
# If a future agent-zero version renames them, the substring match in
# ``_looks_like_memory_prompt`` returns False and the clamp becomes a
# silent no-op for that prompt -- the safe failure mode.
_MEMORY_PROMPT_MARKERS = (
    "memory.memories_sum",
    "memory.solutions_sum",
    "memory.fragments_sum",
    "memory.solutions.sys",
)

# The `_model_fallback` plugin's own memory budget key. When this
# plugin is installed alongside `_model_fallback` (the recommended
# layout), the same value governs both layers.
_FALLBACK_BUDGET_KEY = "memory_memorize_max_chars"

# Default budget if neither this plugin's override nor
# `_model_fallback`'s config is available. Matches the
# `_model_fallback` default at 50000 chars (~12.5k tokens, safely under
# a 32k context window).
_DEFAULT_BUDGET = 50000

# The truncation notice appended (when enabled) to a clamped history.
# Kept as a constant so tests can assert against it exactly.
_TRUNCATION_NOTICE = (
    "\n\n[NOTE: Earlier turns were trimmed to fit the "
    "utility-model context window.]"
)

# Process-global telemetry state. Exposed via ``get_state()`` for the
# /stats API. Mirrors the STATE-dict pattern used by ``recall_patch``.
STATE: Dict[str, Any] = {
    "calls_seen": 0,
    "memory_calls": 0,
    "clamped": 0,
    "skipped_non_memory": 0,
    "skipped_within_budget": 0,
    "errors": 0,
    "last_budget": 0,
    "last_orig_chars": 0,
    "last_new_chars": 0,
    "last_status": "never_run",
}


def _snapshot() -> Dict[str, Any]:
    return dict(STATE)


def get_state() -> Dict[str, Any]:
    """Return current telemetry state for the /stats API."""
    return _snapshot()


def reset() -> None:
    """Reset telemetry counters. Used by tests and ``uninstall()``."""
    for key in STATE:
        STATE[key] = 0
    STATE["last_status"] = "never_run"


def _looks_like_memory_prompt(system: str) -> bool:
    """Return True if the system prompt looks like a memory-recall or
    memorize / solve prompt from the official ``_memory`` plugin.

    Substring matching (not a full parse): the system prompts are large
    Markdown blobs and parsing them is fragile. The markers are stable
    file names that the official extensions reference via
    ``read_prompt(...)``; if upstream renames them, this returns False
    and the clamp becomes a no-op -- the safe failure mode.
    """
    if not isinstance(system, str):
        return False
    for marker in _MEMORY_PROMPT_MARKERS:
        if marker in system:
            return True
    return False


def _coerce_positive_int(value: Any) -> Optional[int]:
    """Coerce a config value to a positive int, or return None.

    Booleans are explicitly rejected: in Python ``isinstance(True,
    int)`` is True, but a config of ``true`` is not a budget of 1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            v = int(value)
            if v > 0:
                return v
        except ValueError:
            return None
    return None


def _resolve_budget(agent: Any, own_override: Any = None) -> int:
    """Pick the clamp budget for this agent.

    Resolution order:
    1. ``own_override`` (``history_clamp_max_chars_override``).
    2. ``_model_fallback.memory_memorize_max_chars``.
    3. ``_DEFAULT_BUDGET`` (50000).

    Never raises: a config-fetch failure falls through to the default.
    """
    override = _coerce_positive_int(own_override)
    if override is not None:
        return override
    try:
        from helpers import plugins as plugin_helpers  # type: ignore
        fb = plugin_helpers.get_plugin_config("_model_fallback", agent) or {}
    except Exception:  # noqa: BLE001
        fb = {}
    candidate = _coerce_positive_int(fb.get(_FALLBACK_BUDGET_KEY))
    if candidate is not None:
        return candidate
    return _DEFAULT_BUDGET


def clamp(
    call_data: Dict[str, Any],
    agent: Any,
    *,
    own_override: Any = None,
    inject_notice: bool = True,
) -> str:
    """Clamp ``call_data["message"]`` if it is a memory prompt that
    exceeds the budget.

    Returns a status string:
      - ``"skipped_non_memory"``    -- not a memory prompt / non-string msg
      - ``"skipped_within_budget"`` -- memory prompt, within budget
      - ``"clamped"``               -- truncated to the budget (+ notice)
      - ``"error"``                 -- an exception was caught; data unchanged

    Mutates ``call_data["message"]`` in place when clamping. Never
    raises.
    """
    STATE["calls_seen"] += 1
    try:
        system = call_data.get("system") or ""
        if not _looks_like_memory_prompt(system):
            STATE["skipped_non_memory"] += 1
            STATE["last_status"] = "skipped_non_memory"
            return "skipped_non_memory"

        message = call_data.get("message")
        if not isinstance(message, str):
            # A memory prompt with a non-string message: nothing to
            # clamp. Count as non-memory so we don't inflate the
            # memory_calls / within-budget counters.
            STATE["skipped_non_memory"] += 1
            STATE["last_status"] = "skipped_non_memory"
            return "skipped_non_memory"

        STATE["memory_calls"] += 1
        budget = _resolve_budget(agent, own_override)
        if not message or len(message) <= budget:
            STATE["skipped_within_budget"] += 1
            STATE["last_budget"] = budget
            STATE["last_orig_chars"] = len(message)
            STATE["last_new_chars"] = len(message)
            STATE["last_status"] = "skipped_within_budget"
            return "skipped_within_budget"

        # Truncate from the START (drop the oldest portion) so the LLM
        # sees the most recent turns. The official code uses
        # ``msgs_text[-MAX_MSGS_CHARS:]`` for the same reason; we mirror
        # that semantic.
        new_message = message[-budget:]
        if inject_notice:
            new_message = new_message + _TRUNCATION_NOTICE
        call_data["message"] = new_message
        STATE["clamped"] += 1
        STATE["last_budget"] = budget
        STATE["last_orig_chars"] = len(message)
        STATE["last_new_chars"] = len(new_message)
        STATE["last_status"] = "clamped"
        _log.debug(
            "history_clamp clamped util-model message from %d to %d chars "
            "(budget=%d)",
            len(message), len(new_message), budget,
        )
        return "clamped"
    except Exception as exc:  # noqa: BLE001
        # Never crash the utility call from our clamp.
        STATE["errors"] += 1
        STATE["last_status"] = "error"
        _log.debug("history_clamp failed: %s", exc)
        return "error"