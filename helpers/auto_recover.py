# Auto-recovery from FAISS load failure (Phase 2). See README.
from __future__ import annotations

import logging
import os
import time
from typing import Dict, Optional

from helpers import files

log = logging.getLogger("memory_hardening.auto_recover")

_recovery_lock_path: Dict[str, bool] = {}
_recovery_history: Dict[str, dict] = {}


def _quarantine_dir(configured: Optional[str] = None):
    """Resolve (and create) the quarantine dir.

    v0.6.0: honours the ``auto_recover_quarantine_dir`` config key
    (previously documented in default_config.yaml but ignored -- the
    path was hardcoded here). Falls back to tmp/memory/quarantine.
    """
    rel = configured or "tmp/memory/quarantine"
    try:
        p = files.get_abs_path(rel)
        os.makedirs(p, exist_ok=True)
        return p
    except Exception:
        return "/tmp/memory_quarantine"


def _move_to_quarantine(path, configured_dir: Optional[str] = None):
    if not os.path.exists(path):
        return None
    qdir = _quarantine_dir(configured_dir)
    base = os.path.basename(path)
    parent = os.path.basename(os.path.dirname(path))
    ts = int(time.time())
    new_name = f"{parent}_{base}.{ts}.corrupt"
    target = os.path.join(qdir, new_name)
    try:
        os.rename(path, target)
        sh = path + ".sha256"
        if os.path.exists(sh):
            try:
                os.rename(sh, target + ".sha256")
            except Exception:
                pass
        return target
    except Exception as e:
        log.warning("quarantine failed for %s: %s", path, e)
        return None


def attempt_recovery(subdir, index_path, quarantine_dir: Optional[str] = None):
    if _recovery_lock_path.get(index_path):
        return {"attempted": False, "reason": "already_in_progress"}
    _recovery_lock_path[index_path] = True
    try:
        quarantined_to = _move_to_quarantine(index_path, quarantine_dir)
        rec = {
            "attempted": True,
            "subdir": subdir,
            "index_path": index_path,
            "quarantined_to": quarantined_to,
            "at": time.time(),
        }
        _recovery_history[subdir] = rec
        if quarantined_to:
            log.warning(
                "auto_recover: quarantined corrupt FAISS index for subdir=%s -> %s",
                subdir, quarantined_to,
            )
        return rec
    finally:
        _recovery_lock_path[index_path] = False


def history():
    return dict(_recovery_history)


def record_outcome(subdir, *, success, error=None):
    if subdir in _recovery_history:
        _recovery_history[subdir]["rebuild_success"] = success
        if error:
            _recovery_history[subdir]["rebuild_error"] = error
        _recovery_history[subdir]["rebuild_at"] = time.time()
