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

# (mtime_ns, size) -> digest. Bounded like the breaker registries so a long
# run over many transient index paths cannot grow it without limit.
_HASH_CACHE: Dict[str, tuple] = {}
_HASH_CACHE_MAX = 512
_HASH_CHUNK = 65536


def _verify_hash(path: str, stored: str) -> bool:
    """True when the file's sha256 equals `stored`, memoised on (mtime_ns, size).

    probe_one() ran this on every pass, i.e. every
    `faiss_health_probe_interval_sec` (120s by default) for up to
    MAX_INDEXES indexes. A FAISS index is routinely hundreds of megabytes, so
    on a bind-mounted filesystem that is potentially tens of GB re-read every
    two minutes — the module's own docstring warns about "stat-storm on the
    9p bindmount" while doing exactly that.

    The cache is not a heuristic: when mtime_ns and size are unchanged the file
    content is unchanged, so re-hashing cannot yield a different answer. Any
    change to either invalidates the entry.
    """
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    cached = _HASH_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1] == stored
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                h.update(chunk)
    except OSError:
        return False
    actual = h.hexdigest()
    if len(_HASH_CACHE) >= _HASH_CACHE_MAX:
        # Drop the oldest insertion; dicts preserve insertion order.
        try:
            _HASH_CACHE.pop(next(iter(_HASH_CACHE)))
        except (StopIteration, RuntimeError):
            _HASH_CACHE.clear()
    _HASH_CACHE[path] = (key, actual)
    return actual == stored


def reset() -> None:
    """Clear process-global state. Called from ``hooks.uninstall``."""
    _HASH_CACHE.clear()


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
                # `open(...).read()` leaked the handle (no context manager) and
                # is replaced below by a `with` block.
                with open(hash_path) as fh:
                    stored = fh.read().strip()
                info["hash_ok"] = _verify_hash(path, stored)
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
