"""Memory recall method patcher.

Restores the missing `search_memories` method on `RecallMemories` in the
v1.2.0 update of the `_memory` plugin. The v1.2.0 patch added a
`_safe_recall` wrapper but accidentally deleted the method it calls.

This module embeds the exact method body from the last known-good version
(memory_fix_backups/_50_recall_memories.py.bak, pre-v1.2.0) and injects it
into the class at runtime via setattr. Fully reversible: just disable
the extension or set `recall_patch_enabled: false` in config.

Telemetry is exposed via `get_state()` for the /stats API endpoint.

Framework imports (agent.LoopData, helpers.*, Memory) are deferred to
function-call time so that this module can be imported in bare test
environments without the full Agent Zero stack.
"""

STATE = {
    "applied": 0,
    "already_present": 0,
    "import_errors": 0,
    "class_not_found": 0,
    "patch_attempts": 0,
    "last_status": "never_run",
    "last_error": "",
    "method_source": "embedded_last_known_good_pre_v1.2.0",
    "method_size_bytes": 0,
}


def _snapshot():
    return dict(STATE)


async def _do_search_memories(self, log_item, loop_data, **kwargs):
    """Restored from memory_fix_backups/_50_recall_memories.py.bak (pre-v1.2.0).
    Lazy-imports the framework deps so this module is importable in tests."""
    from helpers import dirty_json, errors, log, plugins
    from plugins._memory.helpers.memory import Memory

    if not self.agent:
        return

    extras = loop_data.extras_persistent
    if "memories" in extras:
        del extras["memories"]
    if "solutions" in extras:
        del extras["solutions"]

    set = plugins.get_plugin_config("_memory", self.agent)
    if not set:
        return None

    system = self.agent.read_prompt("memory.memories_query.sys.md")

    user_instruction = (
        loop_data.user_message.output_text() if loop_data.user_message else "None"
    )
    history = self.agent.history.output_text()[-set["memory_recall_history_len"]:]
    message = self.agent.read_prompt(
        "memory.memories_query.msg.md", history=history, message=user_instruction
    )

    if set["memory_recall_query_prep"]:
        try:
            query = await self.agent.call_utility_model(
                system=system,
                message=message,
            )
            query = query.strip()
            log_item.update(query=query)
        except Exception as e:
            err = errors.format_error(e)
            self.agent.context.log.log(
                type="warning", heading="Recall memories extension error:", content=err
            )
            query = ""

        if not query:
            log_item.update(heading="Failed to generate memory query")
            return
    else:
        query = user_instruction + "\n\n" + history

    if not query or len(query) <= 3:
        log_item.update(query="No relevant memory query generated, skipping search")
        return

    db = await Memory.get(self.agent)

    memories = await db.search_similarity_threshold(
        query=query,
        limit=set["memory_recall_memories_max_search"],
        threshold=set["memory_recall_similarity_threshold"],
        filter=f"area == '{Memory.Area.MAIN.value}' or area == '{Memory.Area.FRAGMENTS.value}'",
    )

    solutions = await db.search_similarity_threshold(
        query=query,
        limit=set["memory_recall_solutions_max_search"],
        threshold=set["memory_recall_similarity_threshold"],
        filter=f"area == '{Memory.Area.SOLUTIONS.value}'",
    )

    if not memories and not solutions:
        log_item.update(heading="No memories or solutions found")
        return

    if set["memory_recall_post_filter"]:
        mems_list = {i: memory.page_content for i, memory in enumerate(memories + solutions)}
        try:
            filter_resp = await self.agent.call_utility_model(
                system=self.agent.read_prompt("memory.memories_filter.sys.md"),
                message=self.agent.read_prompt(
                    "memory.memories_filter.msg.md",
                    memories=mems_list,
                    history=history,
                    message=user_instruction,
                ),
            )
            filter_inds = dirty_json.try_parse(filter_resp)
            filtered_memories = []
            filtered_solutions = []
            mem_len = len(memories)
            if isinstance(filter_inds, list):
                for idx in filter_inds:
                    if isinstance(idx, int):
                        if idx < mem_len:
                            filtered_memories.append(memories[idx])
                        else:
                            sol_idx = idx - mem_len
                            if sol_idx < len(solutions):
                                filtered_solutions.append(solutions[sol_idx])
            memories = filtered_memories
            solutions = filtered_solutions
        except Exception as e:
            err = errors.format_error(e)
            self.agent.context.log.log(
                type="warning", heading="Failed to filter relevant memories", content=err
            )

    memories = memories[: set["memory_recall_memories_max_result"]]
    solutions = solutions[: set["memory_recall_solutions_max_result"]]

    log_item.update(
        heading=f"{len(memories)} memories and {len(solutions)} relevant solutions found",
    )

    memories_txt = "\n\n".join([mem.page_content for mem in memories]) if memories else ""
    solutions_txt = "\n\n".join([sol.page_content for sol in solutions]) if solutions else ""

    if memories_txt:
        log_item.update(memories=memories_txt)
    if solutions_txt:
        log_item.update(solutions=solutions_txt)

    if memories_txt:
        extras["memories"] = self.agent.parse_prompt(
            "agent.system.memories.md", memories=memories_txt
        )
    if solutions_txt:
        extras["solutions"] = self.agent.parse_prompt(
            "agent.system.solutions.md", solutions=solutions_txt
        )


async def search_memories(self, log_item, loop_data, **kwargs):
    """Public method attached to RecallMemories via setattr."""
    return await _do_search_memories(self, log_item, loop_data, **kwargs)


def apply_recall_patch(enabled=True, agent=None):
    """Apply the search_memories method patch to RecallMemories if needed.

    Resolves the class through the dispatcher's own list
    (``helpers.extension._get_extension_classes``) -- the framework loads
    extension files as synthetic modules, so a canonical import yields a
    phantom class that is never instantiated (patching it is a silent
    no-op; see helpers/extension_class.py). Upstream currently defines
    ``search_memories`` itself, so this is dormant ("already_present"),
    but it must target the real class to work if upstream ever drops it.

    Returns a status dict with counts and last_status. Safe to call
    multiple times; idempotent. If the method already exists on the
    class, it is NOT overwritten (preserves any later fix from upstream).
    """
    if not enabled:
        STATE["last_status"] = "disabled"
        return _snapshot()

    STATE["patch_attempts"] += 1

    try:
        from usr.plugins.memory_hardening.helpers.extension_class import (
            resolve_extension_class,
        )

        RecallMemories = resolve_extension_class(
            agent,
            "message_loop_prompts_after",
            "RecallMemories",
            "_50_recall_memories",
            "plugins._memory.extensions.python.message_loop_prompts_after._50_recall_memories",
        )
        if RecallMemories is None:
            raise ImportError("RecallMemories not resolvable")
    except ImportError as e:
        STATE["import_errors"] += 1
        STATE["last_error"] = str(e)
        STATE["last_status"] = "import_error"
        return _snapshot()
    except Exception as e:
        STATE["import_errors"] += 1
        STATE["last_error"] = str(e)
        STATE["last_status"] = "import_error"
        return _snapshot()

    if RecallMemories is None:
        STATE["class_not_found"] += 1
        STATE["last_status"] = "class_not_found"
        return _snapshot()

    if hasattr(RecallMemories, "search_memories"):
        STATE["already_present"] += 1
        STATE["last_status"] = "already_present"
        return _snapshot()

    STATE["applied"] += 1
    try:
        STATE["method_size_bytes"] = len(search_memories.__code__.co_code)
    except Exception:
        STATE["method_size_bytes"] = 0
    setattr(RecallMemories, "search_memories", search_memories)
    STATE["last_status"] = "applied"
    STATE["last_error"] = ""
    return _snapshot()


def get_state():
    """Return current telemetry state for the /stats API."""
    return _snapshot()


def reset_state():
    """Reset telemetry counters (for testing and reset_breaker endpoint)."""
    for key in STATE:
        if isinstance(STATE[key], int):
            STATE[key] = 0
        elif key == "last_status":
            STATE[key] = "never_run"
        elif key == "method_source":
            STATE[key] = "embedded_last_known_good_pre_v1.2.0"
        else:
            STATE[key] = ""
    return _snapshot()
