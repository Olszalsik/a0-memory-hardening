"""Memory history clamp extension.

Hooks ``util_model_call_before`` at priority 10 -- after the
``_model_fallback`` cascade hooks at ``_00`` / ``_01`` -- and clamps
the chat-history string the official ``_memory`` plugin sends to the
utility model during memorize / solve operations. See
``helpers/history_clamp.py`` for the rationale and budget resolution.

The hook fires for EVERY utility-model call (not just memory ones).
``history_clamp.clamp()`` filters on the system-prompt substring so
non-memory calls are not affected.

Config (``memory_hardening``):
  hardening_enabled                  master kill switch (default true)
  history_clamp_enabled              toggle this feature (default true)
  history_clamp_max_chars_override    optional positive-int override
  history_clamp_inject_truncation_notice  bool (default true)
"""

from __future__ import annotations

from typing import Any, Dict

from helpers.extension import Extension

from usr.plugins.memory_hardening.helpers import history_clamp


class ClampMemoryUtilCall(Extension):
    """Clamp the util-model message for memory prompts."""

    async def execute(self, call_data: Dict[str, Any] = None, **kwargs: Any) -> None:
        if call_data is None:
            return
        try:
            from helpers import plugins
            cfg = plugins.get_plugin_config("memory_hardening", self.agent) or {}
        except Exception:  # noqa: BLE001
            cfg = {}

        # Respect the plugin master switch, then the feature toggle.
        if not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("history_clamp_enabled", True):
            return

        history_clamp.clamp(
            call_data,
            self.agent,
            own_override=cfg.get("history_clamp_max_chars_override"),
            inject_notice=cfg.get("history_clamp_inject_truncation_notice", True),
        )