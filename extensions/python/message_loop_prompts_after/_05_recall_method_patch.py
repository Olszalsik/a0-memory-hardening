"""Memory recall method patcher extension.

Runs at priority 5 (before _50_recall_memories at priority 50) on every
message loop iteration. On first run, applies the recall_patch to restore
the missing `search_memories` method on `RecallMemories`. Idempotent and
zero-cost after the first successful patch.

Config:
  recall_patch_enabled (bool, default true): toggle the patch on/off
"""
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import recall_patch


class RecallMethodPatch(Extension):
    async def execute(self, loop_data=None, **kwargs):
        if not self.agent:
            return

        try:
            config = plugins.get_plugin_config("memory_hardening", self.agent) or {}
        except Exception:
            config = {}

        enabled = config.get("recall_patch_enabled", True)

        result = recall_patch.apply_recall_patch(enabled=enabled)

        if result.get("last_status") == "applied" and result.get("applied") == 1:
            try:
                self.agent.context.log.log(
                    type="info",
                    heading="Memory recall method patch applied",
                    content=(
                        "Restored missing search_memories() on RecallMemories. "
                        "See /api/plugins/memory_hardening/stats for details."
                    ),
                )
            except Exception:
                pass
