# FAISS index health probe (Phase 2). See README for full docstring.
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Dict, List

from helpers import files

log = logging.getLogger("memory_hardening.faiss_health")


def _index_paths():
    """Find FAISS index files under usr/memory.

    v0.6.0: recursive (os.walk) instead of a single-level listing --
    knowledge subdirs nest (e.g. ``usr/memory/knowledge/<name>/``), and
    the old one-level scan silently missed every nested index. Bounded by
    MAX_INDEXES + MAX_DIRS so a pathological tree cannot make the
    job_loop health probe stat-storm (the 9p bindmount on Windows is
    stat-sensitive; see the Time Travel incident).
    """
    out = []
    walked = 0
    try:
        base = files.get_abs_path("usr/memory")
        if os.path.isdir(base):
            for root, dirs, _files in os.walk(base):
                walked += len(dirs)
                p = os.path.join(root, "index.faiss")
                if os.path.exists(p):
                    out.append(p)
                    if len(out) >= MAX_INDEXES:
                        break
                # v0.6.0 fix: cumulative bound -- the original per-level
                # check (len(dirs) + len(out) > MAX_DIRS) never fired on a
                # normal knowledge tree, so the walk was still unbounded.
                if walked >= MAX_DIRS:
                    dirs[:] = []  # stop descending
    except Exception as e:
        log.debug("scan failed: %s", e)
    return out


MAX_INDEXES = 200   # cap on index files probed per pass
MAX_DIRS = 500      # cap on directories walked per pass


def probe_one(path, min_size_bytes=1024, max_age_days=90):
    info = {
        "path": path,
        "exists": False,
        "size_bytes": 0,
        "mtime": None,
        "age_days": None,
        "hash_ok": None,
        "warning": None,
    }
    try:
        st = os.stat(path)
        info["exists"] = True
        info["size_bytes"] = st.st_size
        info["mtime"] = st.st_mtime
        info["age_days"] = round((time.time() - st.st_mtime) / 86400.0, 2)
        if st.st_size < min_size_bytes:
            info["warning"] = "too_small"
        # v0.6.0: staleness uses the configured max_age_days (was a
        # hardcoded 365 that contradicted probe_all's 90-day default).
        if info["age_days"] is not None and info["age_days"] > max_age_days:
            info["warning"] = (info["warning"] or "") + "|stale"
        hash_path = path + ".sha256"
        if os.path.exists(hash_path):
            try:
                stored = open(hash_path).read().strip()
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                info["hash_ok"] = (h.hexdigest() == stored)
                if not info["hash_ok"]:
                    info["warning"] = (info["warning"] or "") + "|hash_mismatch"
            except Exception as e:
                info["warning"] = (info["warning"] or "") + f"|hash_error:{e}"
    except FileNotFoundError:
        info["warning"] = "missing"
    except Exception as e:
        info["warning"] = f"error:{e}"
    return info


def probe_all(*, min_size_bytes=1024, max_age_days=90):
    paths = _index_paths()
    results = [
        probe_one(p, min_size_bytes=min_size_bytes, max_age_days=max_age_days)
        for p in paths
    ]
    warnings = [r for r in results if r["warning"]]
    stale = [r for r in results if r["age_days"] is not None and r["age_days"] > max_age_days]
    return {
        "checked_at": time.time(),
        "count": len(results),
        "warning_count": len(warnings),
        "stale_count": len(stale),
        "results": results,
    }
