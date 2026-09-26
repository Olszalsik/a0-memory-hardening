# Cross-chat quarantine (Phase 3).
# Auto-archives memory entries older than max_age_days to a side dir.
from __future__ import annotations
import json
import logging
import os
import time
from typing import Dict, List, Optional

from helpers import files

log = logging.getLogger("memory_hardening.quarantine")


def _archive_dir(path: str) -> str:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


_LAST_SCAN: Optional[Dict] = None

# Bounds for the tree walk, mirroring faiss_health.MAX_INDEXES / MAX_DIRS.
MAX_DIRS = 500          # directories visited per scan
MAX_CANDIDATES = 200    # stale indexes reported per scan


def reset() -> None:
    """Clear process-global state. Called from ``hooks.uninstall``.

    Without this, the last scan summary (and its candidate list) survived a
    disable/enable cycle, so /stats reported pre-uninstall state for a plugin
    that had not run yet.
    """
    global _LAST_SCAN
    _LAST_SCAN = None


def scan(*, max_age_days: int = 90, archive_dir: str = "tmp/memory/archive") -> Dict:
    """Scan all known FAISS index metadata for stale entries and archive them.
    Returns a summary dict. We do not delete from the live index here -- we
    just identify candidates and write a manifest to the archive dir.
    """
    global _LAST_SCAN
    arch = _archive_dir(files.get_abs_path(archive_dir))
    now = time.time()
    cutoff = now - (max_age_days * 86400.0)
    summary = {
        "scanned_at": now,
        "max_age_days": max_age_days,
        "archive_dir": arch,
        "candidates": [],
    }
    base = files.get_abs_path("usr/memory")
    if not os.path.isdir(base):
        _LAST_SCAN = summary
        return summary
    # v0.6.0: recursive walk (nested knowledge subdirs were invisible to
    # the old single-level listing); subdir = path relative to usr/memory.
    #
    # v0.7.0: the walk is BOUNDED. `faiss_health` already capped its own scan
    # (MAX_INDEXES / MAX_DIRS) precisely because `usr/memory` is large,
    # user-controlled in depth, and may sit on a bind mount; this module
    # walked it unbounded on the same job_loop schedule, so the two halves of
    # the same health feature had opposite cost profiles.
    walked = 0
    for root, dirs, _files in os.walk(base):
        walked += 1
        if walked > MAX_DIRS:
            summary["truncated"] = True
            break
        # Prune descent at this level; we only ever match the index file
        # inside the current directory.
        dirs[:] = []
        p = os.path.join(root, "index.faiss")
        if not os.path.exists(p):
            continue
        if len(summary["candidates"]) >= MAX_CANDIDATES:
            summary["truncated"] = True
            break
        try:
            mtime = os.path.getmtime(p)
            if mtime < cutoff:
                rel = os.path.relpath(os.path.dirname(p), base).replace("\\", "/")
                summary["candidates"].append({"subdir": rel, "age_days": round((now - mtime) / 86400.0, 1)})
        except OSError:
            continue
    summary["dirs_walked"] = walked
    if summary["candidates"]:
        try:
            manifest = os.path.join(arch, f"quarantine_{int(now)}.json")
            with open(manifest, "w") as f:
                json.dump(summary, f, indent=2)
            summary["manifest"] = manifest
            log.info("quarantine: found %d stale indexes, manifest=%s", len(summary["candidates"]), manifest)
        except Exception as e:
            log.warning("quarantine manifest write failed: %s", e)
    # v0.6.0: record the last scan for snapshot() (the old
    # scan.__dict__.get("_last") read a field that was never set, so
    # /stats always showed last_scan: null).
    _LAST_SCAN = summary
    return summary


def snapshot() -> Dict:
    return {"last_scan": _LAST_SCAN}
