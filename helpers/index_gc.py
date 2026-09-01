# Index cache GC (Phase 2). See README for full docstring.
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

log = logging.getLogger("memory_hardening.index_gc")

_lock = threading.Lock()
_last_access = {}
_last_gc_at = 0.0
_evicted_total = 0


def _memory_index():
    try:
        from plugins._memory.helpers.memory import Memory
        return Memory.index
    except Exception as e:
        log.debug("Memory.index import failed: %s", e)
        return None


def touch(subdir):
    if not subdir:
        return
    with _lock:
        _last_access[subdir] = time.time()


def _subdir_last_used(subdir, default=None):
    with _lock:
        return _last_access.get(subdir, default)


def _evict(subdir):
    try:
        from plugins._memory.helpers.memory import Memory
        if subdir in Memory.index:
            del Memory.index[subdir]
            with _lock:
                global _evicted_total
                _evicted_total += 1
                _last_access.pop(subdir, None)
            log.info("index_gc: evicted subdir=%s", subdir)
            return True
    except Exception as e:
        log.debug("evict failed for %s: %s", subdir, e)
    return False


def gc_once(*, idle_min=30.0, max_entries=16):
    idx = _memory_index()
    if not idx:
        return {"inspected": 0, "evicted_idle": 0, "evicted_overflow": 0, "remaining": 0}
    now = time.time()
    idle_cutoff = now - (idle_min * 60.0)
    inspected = len(idx)
    evicted_idle = 0
    evicted_overflow = 0
    subdirs = list(idx.keys())
    subdirs.sort(key=lambda s: (_subdir_last_used(s) or 0.0))
    # v0.5.3 fix: an entry with no recorded access is UNKNOWN, not idle.
    # touch() is only called from the recall path, so treating None as
    # idle evicted every cached index on every pass and forced a full
    # FAISS reload on the next recall. Unknown entries are never evicted.
    for subdir in list(subdirs):
        last = _subdir_last_used(subdir)
        if last is not None and last < idle_cutoff:
            if _evict(subdir):
                evicted_idle += 1
                subdirs.remove(subdir)
    if len(idx) > max_entries:
        known = [s for s in subdirs if _subdir_last_used(s) is not None]
        known.sort(key=lambda s: _subdir_last_used(s))
        while len(idx) > max_entries and known:
            oldest = known.pop(0)
            if _evict(oldest):
                evicted_overflow += 1
    if evicted_idle or evicted_overflow:
        log.info(
            "index_gc pass: inspected=%d idle_evicted=%d overflow_evicted=%d remaining=%d",
            inspected, evicted_idle, evicted_overflow, len(idx),
        )
    return {
        "inspected": inspected,
        "evicted_idle": evicted_idle,
        "evicted_overflow": evicted_overflow,
        "remaining": len(idx),
    }


def snapshot():
    idx = _memory_index()
    with _lock:
        return {
            "current_size": len(idx) if idx else 0,
            "tracked_subdirs": list(_last_access.keys()),
            "evicted_total": _evicted_total,
            "last_gc_at": _last_gc_at,
        }


def mark_gc_run():
    global _last_gc_at
    _last_gc_at = time.time()
