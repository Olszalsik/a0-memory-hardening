# Test suite for the memory_hardening history_clamp (v0.5.0)
#
# Ported from the former _memory_resilience/tests/test_clamp_v1.py, then
# retargeted to memory_hardening.helpers.history_clamp. Covers:
# - _looks_like_memory_prompt: substring match for the official memory
#   system-prompt file names; no false positives on non-memory prompts.
# - _resolve_budget: own override wins; falls through to
#   _model_fallback.memory_memorize_max_chars; default 50000; bool
#   override rejected; string coercion; zero/negative ignored.
# - clamp(): no-op on non-memory calls; no-op within budget; truncates
#   and appends notice when over budget; bug-safe (exception in resolve
#   does not crash; exception in clamp leaves call_data unchanged);
#   own override takes precedence.
# - STATE telemetry: accumulates counters; reset() clears them.
# - Extension wiring: respects hardening_enabled and history_clamp_enabled.
#
# Run from the plugin directory:
#   /opt/venv/bin/python tests/test_history_clamp.py
import sys, asyncio, ast
from pathlib import Path

# Repo/install root: derived from this file's location
# (<root>/usr/plugins/memory_hardening/tests/ -> 4 levels up), which works
# both inside the container (/a0) and on the host.
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, str(Path(_REPO_ROOT) / 'usr' / 'plugins'))

from unittest.mock import patch, MagicMock

# Import via the SAME module path the extension uses (usr.plugins.*) so
# `hc.STATE` and the extension's `history_clamp.STATE` are the one and
# the same module object. (Agent Zero supports two import roots --
# `/a0/usr/plugins/<name>` and `/a0/usr/plugins/<name>` via `usr.plugins`
# -- which load as distinct module objects; mixing them splits STATE.)
from usr.plugins.memory_hardening.helpers import history_clamp as hc
from usr.plugins.memory_hardening.extensions.python.util_model_call_before import (  # noqa: E402
    _10_clamp_memory_history as ext_mod,
)

results = []

def _run_case(name, fn):
    try:
        fn()
        results.append((name, 'PASS'))
    except Exception as e:
        results.append((name, f'FAIL: {e}'))


# ---------------------------------------------------------------------------
# Prompt detection
# ---------------------------------------------------------------------------
# Markers must match the RENDERED prompt content the framework hands to
# util_model_call_before -- not prompt file names (the v0.5.0 bug: the
# clamp silently never fired live).
_MEMORIES_SUM_SENTENCE = (
    "- The response format is a JSON array of text notes containing "
    "durable facts to memorize")
_SOLUTIONS_SUM_SENTENCE = (
    '- The response format is a JSON array of successful solutions '
    'containing "problem" and "solution" properties')

def t_prompt_memories_sum():
    assert hc._looks_like_memory_prompt(_MEMORIES_SUM_SENTENCE) is True

def t_prompt_solutions_sum():
    assert hc._looks_like_memory_prompt(_SOLUTIONS_SUM_SENTENCE) is True

def t_prompt_live_upstream_files():
    """Regression: load the REAL upstream prompt files and assert the
    markers still match their rendered content. Skipped when the host
    install doesn't ship plugins/_memory (bare CI envs)."""
    prompt_dir = Path(_REPO_ROOT) / 'plugins' / '_memory' / 'prompts'
    fragments = prompt_dir / 'memory.memories_sum.sys.md'
    solutions = prompt_dir / 'memory.solutions_sum.sys.md'
    if not fragments.exists() or not solutions.exists():
        import pytest
        pytest.skip('upstream _memory prompts not present')
    assert hc._looks_like_memory_prompt(fragments.read_text(encoding='utf-8')) is True
    assert hc._looks_like_memory_prompt(solutions.read_text(encoding='utf-8')) is True

def t_prompt_non_memory():
    assert hc._looks_like_memory_prompt("You are a code reviewer. Be terse.") is False

def t_prompt_empty():
    assert hc._looks_like_memory_prompt("") is False

def t_prompt_non_string():
    assert hc._looks_like_memory_prompt(None) is False
    assert hc._looks_like_memory_prompt(123) is False
    assert hc._looks_like_memory_prompt(["list"]) is False


# ---------------------------------------------------------------------------
# Budget resolution
# ---------------------------------------------------------------------------
def t_budget_own_override_wins():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {"_model_fallback": 99999}.get(name, {})
        assert hc._resolve_budget(agent, own_override=30000) == 30000

def t_budget_falls_through_to_fallback():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 42000},
        }.get(name, {})
        assert hc._resolve_budget(agent, own_override=None) == 42000

def t_budget_default_when_neither_set():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {}
        assert hc._resolve_budget(agent, own_override=None) == hc._DEFAULT_BUDGET

def t_budget_default_when_helpers_raises():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config", side_effect=Exception("down")):
        assert hc._resolve_budget(agent, own_override=None) == hc._DEFAULT_BUDGET

def t_budget_string_override_coerced():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {}
        assert hc._resolve_budget(agent, own_override="15000") == 15000

def t_budget_string_garbage_ignored():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 12345},
        }.get(name, {})
        assert hc._resolve_budget(agent, own_override="not a number") == 12345

def t_budget_zero_negative_ignored():
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 12345},
        }.get(name, {})
        assert hc._resolve_budget(agent, own_override=0) == 12345
        assert hc._resolve_budget(agent, own_override=-5) == 12345

def t_budget_bool_override_rejected():
    # isinstance(True, int) is True in Python; a config of `true` must NOT
    # be read as budget 1. It should fall through to the fallback.
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 12345},
        }.get(name, {})
        assert hc._resolve_budget(agent, own_override=True) == 12345


# ---------------------------------------------------------------------------
# clamp() behaviour
# ---------------------------------------------------------------------------
def _clamp(cd, agent, *, own_override=None, inject_notice=True):
    return hc.clamp(cd, agent, own_override=own_override, inject_notice=inject_notice)

def t_clamp_non_memory_untouched():
    hc.reset()
    cd = {"system": "You are a code reviewer.", "message": "x" * 100_000}
    assert _clamp(cd, None) == "skipped_non_memory"
    assert cd["message"] == "x" * 100_000

def t_clamp_within_budget_untouched():
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 200_000},
        }.get(name, {})
        cd = {"system": _MEMORIES_SUM_SENTENCE,
              "message": "x" * 100_000}
        assert _clamp(cd, agent) == "skipped_within_budget"
        assert cd["message"] == "x" * 100_000

def t_clamp_over_budget_truncated_no_notice():
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 50_000},
        }.get(name, {})
        cd = {"system": _MEMORIES_SUM_SENTENCE,
              "message": "x" * 100_000}
        assert _clamp(cd, agent, inject_notice=False) == "clamped"
        assert len(cd["message"]) == 50_000
        assert cd["message"] == ("x" * 100_000)[-50_000:]

def t_clamp_truncation_appends_notice():
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 50_000},
        }.get(name, {})
        cd = {"system": _SOLUTIONS_SUM_SENTENCE, "message": "x" * 100_000}
        assert _clamp(cd, agent, inject_notice=True) == "clamped"
        assert cd["message"].endswith(
            "[NOTE: Earlier turns were trimmed to fit the "
            "utility-model context window.]")
        assert cd["message"].startswith("x" * 50_000)

def t_clamp_empty_message_untouched():
    hc.reset()
    cd = {"system": _MEMORIES_SUM_SENTENCE, "message": ""}
    assert _clamp(cd, None) == "skipped_within_budget"
    assert cd["message"] == ""

def t_clamp_bug_safe_when_resolve_raises():
    # _resolve_budget catches its own exception and returns the default
    # (50000), so the clamp still fires with that budget.
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config", side_effect=Exception("cfg down")):
        cd = {"system": _MEMORIES_SUM_SENTENCE, "message": "x" * 100_000}
        status = _clamp(cd, agent)
    assert status == "clamped"
    assert len(cd["message"]) < 100_000
    assert cd["message"].startswith("x" * 50_000)

def t_clamp_inner_try_except_no_crash():
    # If _resolve_budget itself raises (bypassing its own guard), the
    # outer try/except in clamp() catches it and call_data is unchanged.
    hc.reset()
    agent = MagicMock()
    with patch.object(
        hc, "_resolve_budget",
        side_effect=Exception("simulated helper bug"),
    ):
        cd = {"system": _MEMORIES_SUM_SENTENCE, "message": "x" * 100_000}
        status = _clamp(cd, agent)
    assert status == "error"
    assert cd["message"] == "x" * 100_000  # unchanged
    assert hc.get_state()["errors"] == 1

def t_clamp_own_override_takes_precedence():
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 50000},
        }.get(name, {})
        cd = {"system": _MEMORIES_SUM_SENTENCE, "message": "y" * 50_000}
        assert _clamp(cd, agent, own_override=1000, inject_notice=False) == "clamped"
        assert len(cd["message"]) == 1000


# ---------------------------------------------------------------------------
# STATE telemetry
# ---------------------------------------------------------------------------
def t_state_accumulates():
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: {
            "_model_fallback": {"memory_memorize_max_chars": 50000},
        }.get(name, {})
        _clamp({"system": "code review", "message": "x" * 10}, agent)        # non-memory
        _clamp({"system": _MEMORIES_SUM_SENTENCE, "message": "x" * 100}, agent)  # within
        _clamp({"system": _MEMORIES_SUM_SENTENCE, "message": "x" * 100_000}, agent)  # clamped
    s = hc.get_state()
    assert s["calls_seen"] == 3
    assert s["skipped_non_memory"] == 1
    assert s["skipped_within_budget"] == 1
    assert s["clamped"] == 1
    assert s["memory_calls"] == 2
    assert s["last_status"] == "clamped"
    assert s["last_budget"] == 50000
    assert s["last_orig_chars"] == 100_000

def t_reset_clears():
    hc.reset()
    s = hc.get_state()
    for k in ("calls_seen", "memory_calls", "clamped", "skipped_non_memory",
              "skipped_within_budget", "errors"):
        assert s[k] == 0, f"{k} not reset"
    assert s["last_status"] == "never_run"


# ---------------------------------------------------------------------------
# Extension wiring (config -> clamp)
# ---------------------------------------------------------------------------
def _run_ext(plugin_cfg, system_prompt, message):
    hc.reset()
    agent = MagicMock()
    with patch("helpers.plugins.get_plugin_config") as gpc:
        gpc.side_effect = lambda name, _a: (
            plugin_cfg if name == "memory_hardening" else
            {"_model_fallback": {"memory_memorize_max_chars": 50000}}.get(name, {})
        )
        class _H(ext_mod.ClampMemoryUtilCall):
            def __init__(self):
                self.agent = agent
        hook = _H()
        cd = {"system": system_prompt, "message": message}
        asyncio.get_event_loop().run_until_complete(hook.execute(call_data=cd))
        return cd

def t_ext_respects_hardening_disabled():
    cd = _run_ext({"hardening_enabled": False, "history_clamp_enabled": True},
                  _MEMORIES_SUM_SENTENCE, "x" * 100_000)
    # Master switch off -> clamp did not fire.
    assert cd["message"] == "x" * 100_000
    assert hc.get_state()["clamped"] == 0

def t_ext_respects_clamp_disabled():
    cd = _run_ext({"hardening_enabled": True, "history_clamp_enabled": False},
                  _MEMORIES_SUM_SENTENCE, "x" * 100_000)
    assert cd["message"] == "x" * 100_000
    assert hc.get_state()["clamped"] == 0

def t_ext_full_path_clamps():
    cd = _run_ext({"hardening_enabled": True, "history_clamp_enabled": True,
                   "history_clamp_max_chars_override": None,
                   "history_clamp_inject_truncation_notice": False},
                  _MEMORIES_SUM_SENTENCE, "x" * 100_000)
    assert hc.get_state()["clamped"] == 1
    assert len(cd["message"]) == 50_000

def t_ext_ast_shape():
    # The extension is a class subclassing Extension with an async execute.
    p = str(Path(_REPO_ROOT) / 'usr' / 'plugins' / 'memory_hardening' / 'extensions' / 'python' / 'util_model_call_before' / '_10_clamp_memory_history.py')
    tree = ast.parse(open(p).read())
    found = False
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == 'ClampMemoryUtilCall':
            for b in n.bases:
                if isinstance(b, ast.Name) and b.id == 'Extension':
                    assert any(isinstance(m, ast.AsyncFunctionDef) and m.name == 'execute'
                               for m in n.body)
                    found = True
    assert found, 'ClampMemoryUtilCall(Extension) with async execute not found'


_CASES = [
    # prompt detection (5)
    ('prompt_memories_sum', t_prompt_memories_sum),
    ('prompt_solutions_sum', t_prompt_solutions_sum),
    ('prompt_live_upstream_files', t_prompt_live_upstream_files),
    ('prompt_non_memory', t_prompt_non_memory),
    ('prompt_empty', t_prompt_empty),
    ('prompt_non_string', t_prompt_non_string),
    # budget resolution (8)
    ('budget_own_override_wins', t_budget_own_override_wins),
    ('budget_falls_through_to_fallback', t_budget_falls_through_to_fallback),
    ('budget_default_when_neither_set', t_budget_default_when_neither_set),
    ('budget_default_when_helpers_raises', t_budget_default_when_helpers_raises),
    ('budget_string_override_coerced', t_budget_string_override_coerced),
    ('budget_string_garbage_ignored', t_budget_string_garbage_ignored),
    ('budget_zero_negative_ignored', t_budget_zero_negative_ignored),
    ('budget_bool_override_rejected', t_budget_bool_override_rejected),
    # clamp behaviour (8)
    ('clamp_non_memory_untouched', t_clamp_non_memory_untouched),
    ('clamp_within_budget_untouched', t_clamp_within_budget_untouched),
    ('clamp_over_budget_truncated_no_notice', t_clamp_over_budget_truncated_no_notice),
    ('clamp_truncation_appends_notice', t_clamp_truncation_appends_notice),
    ('clamp_empty_message_untouched', t_clamp_empty_message_untouched),
    ('clamp_bug_safe_when_resolve_raises', t_clamp_bug_safe_when_resolve_raises),
    ('clamp_inner_try_except_no_crash', t_clamp_inner_try_except_no_crash),
    ('clamp_own_override_takes_precedence', t_clamp_own_override_takes_precedence),
    # state telemetry (2)
    ('state_accumulates', t_state_accumulates),
    ('reset_clears', t_reset_clears),
    # extension wiring (4)
    ('ext_respects_hardening_disabled', t_ext_respects_hardening_disabled),
    ('ext_respects_clamp_disabled', t_ext_respects_clamp_disabled),
    ('ext_full_path_clamps', t_ext_full_path_clamps),
    ('ext_ast_shape', t_ext_ast_shape),
]

if __name__ == '__main__':
    # Standalone runner: python tests/test_history_clamp.py
    for name, fn in _CASES:
        _run_case(name, fn)

    print('=' * 70)
    total = 0; passed = 0
    for n, r in results:
        total += 1
        if r.startswith('PASS'): passed += 1
        print(f'  {n:38s} {r}')
    print('=' * 70)
    print(f'{passed}/{total} tests passed')
    sys.exit(0 if passed == total else 1)
else:
    # pytest: expose each case as a real test (exceptions propagate).
    import pytest

    @pytest.mark.parametrize("name,fn", _CASES, ids=[n for n, _ in _CASES])
    def test_case(name, fn):
        fn()