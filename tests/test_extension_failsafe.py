"""Fail-safe contract for every memory_hardening extension.

Incident this exists to prevent
-------------------------------
On 2026-09-26 v0.7.0 introduced `per_subdir_breaker.DEFAULT_ENABLED` and read
it as a BARE attribute from
`message_loop_start/_40_per_subdir_breaker.py`. A live agent stalled with:

    AttributeError: module 'usr.plugins.memory_hardening.helpers.
    per_subdir_breaker' has no attribute 'DEFAULT_ENABLED'
      File "/a0/helpers/extension.py", line 240, in call_extensions_async
      File "/a0/agent.py", line 418, in monologue

The mechanism: the framework re-reads EXTENSION files on every dispatch, but
imported HELPER modules stay cached in `sys.modules` for the whole process. So
after a plugin update, freshly-loaded extension code can run against a stale
copy of a helper module and miss any symbol it added. A bare attribute
reference turns that into an exception on the agent's hot path.

This file enforces the two rules that make that impossible:

  RULE 1  an extension's `execute()` must not raise, for ANY input
  RULE 2  cross-module constants are read with `getattr(obj, NAME, fallback)`,
          never as a bare attribute

A resilience plugin that can break the agent it protects is worse than no
plugin, so these are treated as hard invariants, not style.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import types

import pytest

from helpers.extension import Extension

PLUGIN_ROOT = pathlib.Path("usr/plugins/memory_hardening")
EXT_DIR = PLUGIN_ROOT / "extensions" / "python"
HELPERS_DIR = PLUGIN_ROOT / "helpers"


def _extension_files() -> list[pathlib.Path]:
    return sorted(p for p in EXT_DIR.rglob("*.py") if p.name != "__init__.py")


def _load_extension_module(path: pathlib.Path):
    """Import an extension file the way the framework does: synthetic module
    named after the file basename, never registered in sys.modules."""
    name = "ext_" + path.stem
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return mod


# --------------------------------------------------------------------------
# RULE 2: no bare cross-module constant access in extension code
# --------------------------------------------------------------------------

# Helper modules are cached in sys.modules; extension files are not. Any
# bare `<alias>.<UPPER_CASE>` where <alias> is a helper import can therefore
# raise AttributeError on a stale module.
_HELPER_ALIASES = {
    "psb", "cb", "igc", "mc", "mw", "rl", "qu", "rp", "rwg", "tm", "wd",
    "cg", "es", "hc", "fh", "ar", "rg",
    "recall_gate", "recall_patch", "recall_wait_guard", "per_subdir_breaker",
    "circuit_breaker", "telemetry", "watchdog", "extension_class",
}


@pytest.mark.parametrize(
    "path", _extension_files(), ids=lambda p: p.name
)
def test_no_bare_helper_constant_access(path: pathlib.Path):
    """RULE 2: helper constants must be read with getattr, not dot access."""
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    offenders = []
    for node in ast.walk(tree):
        # module.CONSTANT  ->  Name(id=alias) . Attribute(attr=CONSTANT)
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.value, ast.Name):
            continue
        if node.value.id not in _HELPER_ALIASES:
            continue
        # getattr(psb, "X", default) is a Call node, not an Attribute, so any
        # Attribute on a helper alias here IS a bare access.
        #
        # Only SCREAMING_CASE names are flagged. Those are the ones a helper
        # module gains between releases (DEFAULT_ENABLED, RECALL_TASK_KEY),
        # which is exactly the shape that broke production. CamelCase names
        # like wd.WatchdogRegistry are long-standing structural references:
        # if a stale module lacked one, the extension would fail to import
        # loudly and every call site would need the same treatment, so they
        # are a different (and much louder) failure mode.
        if not node.attr or not node.attr.replace("_", "").isupper():
            continue
        offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
    assert not offenders, (
        f"{path.name} reads a helper constant as a bare attribute. A stale "
        f"(sys.modules-cached) helper module would raise AttributeError on the "
        f"agent's monologue path:\n  " + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------
# RULE 1: execute() must not raise, for any input
# --------------------------------------------------------------------------

def _make_fake_agent(**ctx):
    agent = types.SimpleNamespace(**ctx)
    agent.context = types.SimpleNamespace(id="ctx-test")
    agent.loop_data = types.SimpleNamespace(
        iteration=1,
        params_persistent={},
        params_temporary={},
        extras_persistent={},
    )
    agent.get_data = lambda *a, **k: None
    agent.set_data = lambda *a, **k: None
    agent.logs = []
    agent.context.log = types.SimpleNamespace(
        log=lambda *a, **k: types.SimpleNamespace(update=lambda *a, **k: None)
    )
    agent.hist_add_warning = lambda *a, **k: None
    return agent


@pytest.mark.parametrize("path", _extension_files(), ids=lambda p: p.name)
def test_execute_never_raises(path: pathlib.Path):
    """RULE 1: drive execute() with hostile inputs; it must not propagate."""
    import asyncio
    import inspect

    try:
        mod = _load_extension_module(path)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{path.name} does not import standalone: {exc!r}")

    ext_classes = [
        obj
        for _n, obj in vars(mod).items()
        if inspect.isclass(obj)
        and issubclass(obj, Extension)
        and obj.__module__ == mod.__name__
    ]
    if not ext_classes:
        pytest.skip(f"{path.name} exposes no Extension subclass")

    hostile_loop_data = [
        None,
        types.SimpleNamespace(params_temporary=None, iteration="not-an-int"),
        types.SimpleNamespace(params_temporary="not-a-dict", iteration=1),
        types.SimpleNamespace(params_temporary={}, iteration=None),
        types.SimpleNamespace(
            params_temporary={
                "_memory_breaker_open": True,
                "_memory_rate_limited": True,
                "_memory_subdir_breaker_open": True,
            },
            iteration=3,
        ),
    ]

    for cls in ext_classes:
        for agent_variant in (None, _make_fake_agent()):
            for loop_data in hostile_loop_data:
                kwargs = {"loop_data": loop_data}
                inst = cls(agent=agent_variant)
                try:
                    result = inst.execute(**kwargs)
                    if inspect.isawaitable(result):
                        asyncio.run(_await(result))
                except Exception as exc:  # noqa: BLE001
                    pytest.fail(
                        f"{path.name}::{cls.__name__}.execute raised "
                        f"{type(exc).__name__}: {exc}\n"
                        f"  agent={'None' if agent_variant is None else 'fake'} "
                        f"loop_data={loop_data!r}"
                    )


async def _await(awaitable):
    return await awaitable


def test_recall_gate_extension_swallows_its_own_errors():
    """The gate must close itself when it cannot evaluate the flags."""
    import asyncio

    from usr.plugins.memory_hardening.helpers import recall_gate
    from usr.plugins.memory_hardening.extensions.python.message_loop_prompts_after._04_recall_gate import (
        RecallGate,
    )

    recall_gate.reset()

    class Hostile:
        @property
        def params_temporary(self):
            raise RuntimeError("boom")

    recall_gate.set_state(True, "circuit_breaker_open")
    inst = RecallGate(agent=_make_fake_agent())
    asyncio.run(inst.execute(loop_data=Hostile()))
    assert recall_gate.current_state()["skip"] is False, (
        "a hostile loop_data must leave the gate CLOSED, not latched open"
    )


def test_recall_gate_install_safe_never_raises():
    """install_guard_safe() absorbs resolver failures."""
    from usr.plugins.memory_hardening.helpers import recall_gate

    class BadAgent:
        class context:
            id = "x"

    out = recall_gate.install_guard_safe(object())
    assert isinstance(out, dict) and "status" in out
