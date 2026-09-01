"""Recall-wait TimeoutError guard extension.

Runs at priority 6 (after ``_05_recall_method_patch`` at priority 5, before
``_50_recall_memories`` at priority 50 and the built-in
``_91_recall_wait`` at priority 91). Applies an idempotent wrapper around
``RecallWait.execute`` so that an ``asyncio.TimeoutError`` from the 30s
recall budget (``asyncio.wait_for`` in ``_50_recall_memories``) cannot
propagate out of ``_91_recall_wait`` and kill the agent monologue loop.

Zero-cost after the first successful wrap (subsequent calls short-circuit
on the ``already_guarded`` telemetry path).

Config:
  recall_wait_guard_enabled (bool, default true): toggle the guard on/off.
  When set to false, the original ``RecallWait.execute`` is restored.
"""
from helpers.extension import Extension
from helpers import plugins

from usr.plugins.memory_hardening.helpers import recall_wait_guard


class RecallWaitGuard(Extension):
    async def execute(self, loop_data=None, **kwargs):
        if not self.agent:
            return

        try:
            config = plugins.get_plugin_config("memory_hardening", self.agent) or {}
        except Exception:
            config = {}

        enabled = config.get("recall_wait_guard_enabled", True)

        result = recall_wait_guard.apply_recall_wait_guard(
            enabled=enabled, agent=self.agent
        )

        status = result.get("last_status")
        if status == "applied" and result.get("applied") == 1:
            try:
                self.agent.context.log.log(
                    type="info",
                    heading="Memory recall-wait guard applied",
                    content=(
                        "RecallWait.execute wrapped -- the 30s recall "
                        "timeout can no longer crash the agent loop. "
                        "See /api/plugins/memory_hardening/stats for details."
                    ),
                )
            except Exception:
                pass
        elif status == "disabled_restored":
            try:
                self.agent.context.log.log(
                    type="info",
                    heading="Memory recall-wait guard disabled",
                    content="Original RecallWait.execute restored.",
                )
            except Exception:
                pass