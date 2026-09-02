# Test suite for memory_hardening extension classes (v0.3.0)
#
# Run standalone from the plugin directory:
#   python tests/test_extensions.py
# or with pytest from the repo root:
#   pytest usr/plugins/memory_hardening/tests/
#
# NOTE: originally a script-style suite with module-level sys.exit(); that
# crashed pytest collection (SystemExit during import -> INTERNALERROR),
# so the runner now only executes under `python tests/test_extensions.py`.
import sys, os, ast
from pathlib import Path

# Repo/install root: derived from this file's location
# (<root>/usr/plugins/memory_hardening/tests/ -> 4 levels up), which works
# both inside the container (/a0) and on the host.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN = _REPO_ROOT / 'usr' / 'plugins' / 'memory_hardening'

results = []

def check(name, fn):
    try:
        fn()
        results.append((name, 'PASS'))
    except Exception as e:
        results.append((name, f'FAIL: {e}'))

# All 16 extension files (Phase 1+2+3+4+5; v0.6.0 removed the dead
# adaptive_interval extension)
all_ext = [
    ('message_loop_start/_10_watchdog_init.py', 'WatchdogInit'),
    ('message_loop_start/_20_circuit_breaker.py', 'CircuitBreakerGate'),
    ('message_loop_start/_30_rate_limiter.py', 'RateLimiter'),
    ('message_loop_start/_40_per_subdir_breaker.py', 'PerSubdirBreaker'),
    ('monologue_end/_10_task_cleanup.py', 'TaskCleanup'),
    ('monologue_end/_20_memorize_watchdog.py', 'MemorizeWatchdog'),
    ('monologue_end/_30_memorize_canceller.py', 'MemorizeCanceller'),
    ('message_loop_prompts_after/_95_recall_telemetry.py', 'RecallTelemetry'),
    # v0.3.2
    ('message_loop_prompts_after/_05_recall_method_patch.py', 'RecallMethodPatch'),
    # v0.5.2
    ('message_loop_prompts_after/_06_recall_wait_guard.py', 'RecallWaitGuard'),
    ('job_loop/_30_memory_health.py', 'MemoryHealth'),
    ('job_loop/_40_index_gc.py', 'IndexGC'),
    ('job_loop/_50_quarantine.py', 'QuarantineScan'),
    # v0.6.0: job_loop/_60_adaptive_interval.py REMOVED (dead feature:
    # record_latency_ms had no production caller and the persist path
    # called a nonexistent helpers.plugins.set_plugin_config).
    ('embedding_model_changed/_20_auto_recover.py', 'AutoRecover'),
    ('system_prompt/_05_hardening_notice.py', 'HardeningNotice'),
    # v0.5.0
    ('util_model_call_before/_10_clamp_memory_history.py', 'ClampMemoryUtilCall'),
]

def t_all_extensions():
    base = _PLUGIN / 'extensions' / 'python'
    found = []
    for rel, expected in all_ext:
        p = base / rel
        tree = ast.parse(p.read_text(encoding='utf-8'))
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and n.name == expected:
                for b in n.bases:
                    if isinstance(b, ast.Name) and b.id == 'Extension':
                        found.append(expected)
    assert len(found) == 16, f'expected 16, got {len(found)}: {found}'

def t_api_handlers():
    for f, expected in [('stats.py', 'Stats'), ('reset_breaker.py', 'ResetBreaker')]:
        p = _PLUGIN / 'api' / f
        tree = ast.parse(p.read_text(encoding='utf-8'))
        ok = False
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and n.name == expected:
                bases = [b.id if isinstance(b, ast.Name) else '?' for b in n.bases]
                has_process = any(isinstance(m, ast.AsyncFunctionDef) and m.name == 'process' for m in n.body)
                has_gm = any(isinstance(m, ast.FunctionDef) and m.name == 'get_methods' for m in n.body)
                if 'ApiHandler' in bases and has_process and has_gm:
                    ok = True
        assert ok, f'{expected} in {f} not valid'

def t_manifest():
    import yaml
    m = yaml.safe_load((_PLUGIN / 'plugin.yaml').read_text(encoding='utf-8'))
    assert m['version'] == '0.6.0'
    cfg = yaml.safe_load((_PLUGIN / 'default_config.yaml').read_text(encoding='utf-8'))
    # Check Phase 3 keys
    for k in ['rate_limiter_enabled', 'per_subdir_breaker_enabled',
              'memorize_hard_cancel_enabled',
              'quarantine_enabled', 'embedding_swap_enabled']:
        assert k in cfg, f'missing Phase 3 key: {k}'
    # v0.6.0: the dead adaptive_interval keys must be gone
    for k in ['adaptive_interval_enabled', 'adaptive_interval_min',
              'adaptive_interval_max', 'adaptive_interval_target_p99_ms',
              'dashboard_refresh_sec', 'dashboard_max_points']:
        assert k not in cfg, f'stale key still present: {k}'
    # Check v0.5.0 history_clamp keys
    for k in ['history_clamp_enabled', 'history_clamp_max_chars_override',
              'history_clamp_inject_truncation_notice']:
        assert k in cfg, f'missing history_clamp key: {k}'

def t_webui():
    html = (_PLUGIN / 'webui' / 'config.html').read_text(encoding='utf-8')
    assert html.startswith('<html>')
    # Phase 3 markers (v0.6.0: 'Adaptive Interval' card removed with the
    # dead feature; 'Coroutine Guard' toggle added)
    for marker in ['Rate Limiter', 'Per-Subdir Breaker',
                   'Quarantine',
                   'Embedding Hot-Swap', 'features active', 'Advanced',
                   'Memory History Clamp', 'Coroutine Guard']:
        assert marker in html, f'missing: {marker}'
    assert 'Adaptive Interval' not in html, 'stale Adaptive Interval card still present'

def t_hook_points():
    # Verify extension files exist in correct hook-point directories
    ext_base = _PLUGIN / 'extensions' / 'python'
    hook_points = ['message_loop_start', 'monologue_end', 'message_loop_prompts_after',
                   'job_loop', 'system_prompt', 'embedding_model_changed',
                   'util_model_call_before']
    for hp in hook_points:
        p = ext_base / hp
        assert p.is_dir(), f'missing hook point dir: {p}'
        files = [f for f in os.listdir(p) if f.endswith('.py')]
        assert len(files) >= 1, f'no extensions in {hp}'


_CASES = [
    ('all_16_extensions', t_all_extensions),
    ('api_handlers', t_api_handlers),
    ('manifest_v0.6.0', t_manifest),
    ('webui_phase3', t_webui),
    ('hook_points', t_hook_points),
]

if __name__ == '__main__':
    # Standalone runner: python tests/test_extensions.py
    for name, fn in _CASES:
        check(name, fn)

    print('=' * 70)
    total = 0; passed = 0
    for n, r in results:
        total += 1
        if r.startswith('PASS'): passed += 1
        print(f'  {n:25s} {r}')
    print('=' * 70)
    print(f'{passed}/{total} tests passed')
    sys.exit(0 if passed == total else 1)
else:
    # pytest: expose each case as a real test (exceptions propagate).
    import pytest

    @pytest.mark.parametrize("name,fn", _CASES, ids=[n for n, _ in _CASES])
    def test_case(name, fn):
        fn()