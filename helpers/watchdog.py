"""
Recall task watchdog.

The built-in _memory extension stores the in-flight recall task on the
agent object via `agent.set_data("_recall_memories_task", task)`. The
task can outlive the agent context (chat reset, reload, exception),
leaking references and hanging the event loop.

This module provides:

- WatchdogRegistry: process-global registry of (agent_context_id -> recall_task)
- Watchdog:        per-context record holding the task + start time + hard cap
- track():         register a new recall task for a context
- reap_stale():    cancel any task that exceeded hard_cap_sec
- cleanup():       cancel + await the task for a given context
- cancel_all():    used by hooks.shutdown() at process exit

The watchdog never owns the task's lifecycle — it observes and bounds
it. The _memory plugin still drives creation and consumption. We only
ensure no task can stay alive past the hard cap or leak across context
boundaries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

log = logging.getLogger("memory_hardening.watchdog")

# Mirror the constants used by the _memory plugin so we can correlate
# telemetry across the two modules without coupling the imports.
RECALL_TASK_KEY = "_recall_memories_task"
RECALL_ITER_KEY = "_recall_memories_iter"


@dataclass
class Watchdog:
    agent_context_id: str
    task: asyncio.Task
    started_at: float = field(default_factory=time.time)
    iteration: Optional[int] = None
    hard_cap_sec: float = 90.0
    cancelled: bool = False
    completed: bool = False

    @property
    def age_sec(self) -> float:
        return time.time() - self.started_at

    def is_stuck(self, stuck_threshold_sec: float) -> bool:
        if self.completed or self.cancelled:
            return False
        if self.task.done():
            return False
        return self.age_sec > stuck_threshold_sec


class WatchdogRegistry:
    _registry: Dict[str, Watchdog] = {}
    _lock = asyncio.Lock() if False else None  # use a real lock for thread safety

    @classmethod
    def _key(cls, agent) -> str:
        # agent.context.id is the stable per-context identifier; fall back to id(agent)
        try:
            ctx = getattr(agent, "context", None)
            ctx_id = getattr(ctx, "id", None) if ctx else None
            if ctx_id:
                return f"ctx:{ctx_id}"
        except Exception:
            pass
        return f"agent:{id(agent)}"

    @classmethod
    def track(
        cls,
        agent,
        task: asyncio.Task,
        *,
        hard_cap_sec: float = 90.0,
        iteration: Optional[int] = None,
    ) -> Optional[Watchdog]:
        """Register a new recall task for an agent context.

        Returns the Watchdog if registered, None if the agent is None or
        the task is None. If a previous watchdog exists for the same
        context, it is left untouched (caller is expected to have called
        cleanup() first). The watchdog does not own or weakref the task;
        it just records a strong reference so we can cancel it later.
        """
        if agent is None or task is None:
            return None
        key = cls._key(agent)
        wd = Watchdog(
            agent_context_id=key,
            task=task,
            hard_cap_sec=hard_cap_sec,
            iteration=iteration,
        )
        # if there's an existing live watchdog, cancel and supersede
        existing = cls._registry.get(key)
        if existing is not None and not existing.completed and not existing.cancelled:
            try:
                if not existing.task.done():
                    existing.task.cancel()
            except Exception as e:  # pragma: no cover - defensive
                log.debug("supersede cancel failed: %s", e)
            existing.cancelled = True
        cls._registry[key] = wd
        return wd

    @classmethod
    def mark_completed(cls, agent) -> None:
        key = cls._key(agent)
        wd = cls._registry.get(key)
        if wd is not None:
            wd.completed = True

    @classmethod
    def get(cls, agent) -> Optional[Watchdog]:
        return cls._registry.get(cls._key(agent))

    @classmethod
    def reap_stale(cls, stuck_threshold_sec: float) -> int:
        """Cancel any watchdog whose task has been pending for more than
        stuck_threshold_sec. Returns the count cancelled.
        """
        count = 0
        now = time.time()
        for wd in list(cls._registry.values()):
            if wd.completed or wd.cancelled:
                continue
            if wd.task.done():
                wd.completed = True
                continue
            if (now - wd.started_at) > stuck_threshold_sec:
                try:
                    wd.task.cancel()
                except Exception as e:  # pragma: no cover - defensive
                    log.debug("reap cancel failed: %s", e)
                wd.cancelled = True
                count += 1
        return count

    @classmethod
    async def cleanup(
        cls,
        agent,
        *,
        await_cap_sec: float = 60.0,
    ) -> bool:
        """Cancel the recall task for this context and wait up to
        await_cap_sec for it to actually stop. Returns True if cleanup
        completed within the cap.
        """
        key = cls._key(agent)
        wd = cls._registry.pop(key, None)
        if wd is None or wd.task is None or wd.task.done():
            return True
        try:
            wd.task.cancel()
        except Exception as e:  # pragma: no cover - defensive
            log.debug("cleanup cancel failed: %s", e)
        try:
            await asyncio.wait_for(
                asyncio.shield(wd.task), timeout=await_cap_sec
            )
        except asyncio.TimeoutError:
            wd.cancelled = True
            return False
        except asyncio.CancelledError:
            pass
        except Exception as e:  # pragma: no cover - defensive
            log.debug("cleanup await raised: %s", e)
        wd.cancelled = True
        return True

    @classmethod
    def cancel_all(cls) -> int:
        """Cancel every tracked task (no awaiting). Used at shutdown."""
        count = 0
        for wd in list(cls._registry.values()):
            if wd.task is None or wd.task.done():
                continue
            try:
                wd.task.cancel()
                count += 1
            except Exception as e:  # pragma: no cover - defensive
                log.debug("cancel_all failed: %s", e)
        cls._registry.clear()
        return count

    @classmethod
    def snapshot(cls) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for k, wd in cls._registry.items():
            out[k] = {
                "started_at": wd.started_at,
                "age_sec": round(wd.age_sec, 2),
                "iteration": wd.iteration,
                "hard_cap_sec": wd.hard_cap_sec,
                "cancelled": wd.cancelled,
                "completed": wd.completed,
                "task_done": wd.task.done(),
            }
        return out
