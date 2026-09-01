"""Framework-consistent extension class resolution.

Why this exists
---------------
Agent Zero loads extension-point files via ``helpers.modules.import_module``,
which builds a SYNTHETIC module named after the file basename (e.g.
``"_91_recall_wait"``) and never registers it in ``sys.modules``. Importing
the canonical dotted path (``plugins._memory.extensions...._91_recall_wait``)
therefore creates a SECOND module + class object that the framework never
instantiates. Class-level patches applied to that phantom class are silent
no-ops: telemetry reports "applied", unit tests pass (they exercise the
canonical class), but the dispatcher keeps calling the unwrapped synthetic
class. This is exactly how the v0.5.2 recall-wait guard failed live
(2026-08-27 crash tracebacks show no ``safe_execute`` frame).

The authoritative class list is ``helpers.extension._get_extension_classes``
-- the exact cached list ``call_extensions_async`` iterates, so a class
found there is guaranteed to be the one instantiated. This resolver goes
through it first and only falls back to a canonical import in bare
environments (unit tests that fake ``sys.modules``).
"""

from __future__ import annotations

import logging

log = logging.getLogger("memory_hardening.extension_class")


def find_extension_class(classes, class_name: str, module_suffix: str):
    """Pure matcher: pick ``cls`` whose class name and module basename match.

    Synthetic modules have ``__module__ == "<file basename>"``; canonical
    imports have the full dotted path. Matching on the suffix covers both.
    """
    for cls in classes or []:
        if cls.__name__ != class_name:
            continue
        if str(getattr(cls, "__module__", "")).endswith(module_suffix):
            return cls
    return None


def resolve_extension_class(
    agent,
    extension_point: str,
    class_name: str,
    module_suffix: str,
    canonical_path: str,
):
    """Return the extension class the framework will actually instantiate.

    Tries the dispatcher's own class list first (authoritative), then a
    canonical import for bare test environments. Returns None when neither
    works; never raises.
    """
    try:
        from helpers import extension as _ext

        classes = _ext._get_extension_classes(extension_point, agent=agent)
        cls = find_extension_class(classes, class_name, module_suffix)
        if cls is not None:
            return cls
        log.debug(
            "extension class %s not in dispatcher list for %s",
            class_name,
            extension_point,
        )
    except Exception as e:  # bare env / framework not fully loaded
        log.debug("extension-class dispatcher resolution failed: %s", e)

    try:
        import importlib

        mod = importlib.import_module(canonical_path)
        return getattr(mod, class_name, None)
    except Exception as e:
        log.debug("extension-class canonical fallback failed: %s", e)
        return None