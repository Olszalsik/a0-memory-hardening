# Auto-recovery from FAISS corruption (Phase 2). See README.
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from helpers.extension import Extension
from helpers import files, plugins

from usr.plugins.memory_hardening.helpers import auto_recover as ar

log = logging.getLogger("memory_hardening.auto_recover")


def _read_cfg(agent):
    try:
        return plugins.get_plugin_config("memory_hardening", agent) or {}
    except Exception:
        return None


def _index_path_for_subdir(subdir):
    try:
        if subdir.startswith("projects/"):
            from helpers.projects import get_project_meta
            base = files.get_abs_path(get_project_meta(subdir[9:]), "memory")
        else:
            base = files.get_abs_path("usr/memory", subdir)
        return os.path.join(base, "index.faiss")
    except Exception as e:
        log.debug("index path resolve failed for %s: %s", subdir, e)
        return ""


def _all_subdirs_from_index():
    out = []
    try:
        from plugins._memory.helpers.memory import Memory
        out = list(Memory.index.keys())
    except Exception as e:
        log.debug("Memory.index probe failed: %s", e)
    return out


class AutoRecover(Extension):
    async def execute(self, **kwargs):
        cfg = _read_cfg(self.agent)
        if not cfg or not cfg.get("hardening_enabled", True):
            return
        if not cfg.get("auto_recover_enabled", True):
            return

        exc = kwargs.get("exception")
        subdir = kwargs.get("subdir") or kwargs.get("memory_subdir")
        if exc is None and not subdir:
            return

        if not subdir:
            subs = _all_subdirs_from_index()
            if not subs:
                return
            subdir = subs[0]

        index_path = _index_path_for_subdir(subdir)
        if not index_path or not os.path.exists(index_path):
            log.debug("auto_recover: no index file at %s", index_path)
            return

        try:
            rec = ar.attempt_recovery(subdir, index_path)
            if rec.get("attempted") and rec.get("quarantined_to"):
                log.warning(
                    "auto_recover: subdir=%s quarantined corrupt index -> %s. Next load will rebuild.",
                    subdir, rec["quarantined_to"],
                )
        except Exception as e:
            log.warning("auto_recover failed: %s", e)
