# Test suite for memory_hardening extension classes (v0.3.0)
import sys, os, ast
sys.path.insert(0, '/a0')
results = []

def check(name, fn):
    try:
        fn()
        results.append((name, 'PASS'))
    except Exception as e:
        results.append((name, f'FAIL: {e}'))

# All 17 extension files (Phase 1+2+3+4+5)
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
    ('job_loop/_60_adaptive_interval.py', 'AdaptiveInterval'),
    ('embedding_model_changed/_20_auto_recover.py', 'AutoRecover'),
    ('system_prompt/_05_hardening_notice.py', 'HardeningNotice'),
    # v0.5.0
    ('util_model_call_before/_10_clamp_memory_history.py', 'ClampMemoryUtilCall'),
]

def t_all_extensions():
    base = '/a0/usr/plugins/memory_hardening/extensions/python/'
    found = []
    for rel, expected in all_ext:
        p = base + rel
        tree = ast.parse(open(p).read())
        for n in tree.body:
            if isinstance(n, ast.ClassDef) and n.name == expected:
                for b in n.bases:
                    if isinstance(b, ast.Name) and b.id == 'Extension':
                        found.append(expected)
    assert len(found) == 17, f'expected 17, got {len(found)}: {found}'

def t_api_handlers():
    for f, expected in [('stats.py', 'Stats'), ('reset_breaker.py', 'ResetBreaker')]:
        p = '/a0/usr/plugins/memory_hardening/api/' + f
        tree = ast.parse(open(p).read())
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
    m = yaml.safe_load(open('/a0/usr/plugins/memory_hardening/plugin.yaml'))
    assert m['version'] == '0.5.3'
    cfg = yaml.safe_load(open('/a0/usr/plugins/memory_hardening/default_config.yaml'))
    # Check Phase 3 keys
    for k in ['rate_limiter_enabled', 'per_subdir_breaker_enabled',
              'adaptive_interval_enabled', 'memorize_hard_cancel_enabled',
              'quarantine_enabled', 'embedding_swap_enabled']:
        assert k in cfg, f'missing Phase 3 key: {k}'
    # Check v0.5.0 history_clamp keys
    for k in ['history_clamp_enabled', 'history_clamp_max_chars_override',
              'history_clamp_inject_truncation_notice']:
        assert k in cfg, f'missing history_clamp key: {k}'

def t_webui():
    html = open('/a0/usr/plugins/memory_hardening/webui/config.html').read()
    assert html.startswith('<html>')
    # Phase 3 markers
    for marker in ['Rate Limiter', 'Per-Subdir Breaker',
                   'Adaptive Interval', 'Quarantine',
                   'Embedding Hot-Swap', 'features active', 'Advanced',
                   'Memory History Clamp']:
        assert marker in html, f'missing: {marker}'

def t_hook_points():
    # Verify extension files exist in correct hook-point directories
    import os
    ext_base = '/a0/usr/plugins/memory_hardening/extensions/python/'
    hook_points = ['message_loop_start', 'monologue_end', 'message_loop_prompts_after',
                   'job_loop', 'system_prompt', 'embedding_model_changed',
                   'util_model_call_before']
    for hp in hook_points:
        p = ext_base + hp
        assert os.path.isdir(p), f'missing hook point dir: {p}'
        files = [f for f in os.listdir(p) if f.endswith('.py')]
        assert len(files) >= 1, f'no extensions in {hp}'

for name, fn in [
    ('all_14_extensions', t_all_extensions),
    ('api_handlers', t_api_handlers),
    ('manifest_v0.3.0', t_manifest),
    ('webui_phase3', t_webui),
    ('hook_points', t_hook_points),
]:
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
