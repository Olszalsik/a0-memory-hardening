# Test suite for memory_hardening helpers (v0.3.0)
# Run standalone from the plugin directory:
#   python tests/test_helpers.py
# or with pytest from the repo root:
#   pytest usr/plugins/memory_hardening/tests/
import sys, os, asyncio, time, ast
from pathlib import Path

# Repo/install root: derived from this file's location
# (<root>/usr/plugins/memory_hardening/tests/ -> 4 levels up), which works
# both inside the container (/a0) and on the host.
_REPO_ROOT = str(Path(__file__).resolve().parents[4])
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, str(Path(_REPO_ROOT) / 'usr' / 'plugins'))
from memory_hardening.helpers import (
    telemetry, watchdog, circuit_breaker,
    memorize_watchdog, index_gc, faiss_health, auto_recover,
    rate_limiter, per_subdir_breaker,
    quarantine, memorize_canceller, embedding_swap, recall_patch,
    history_clamp,
)

results = []

def _run_case(name, fn):
    try:
        fn()
        results.append((name, 'PASS'))
    except Exception as e:
        results.append((name, f'FAIL: {e}'))

# Phase 1
def t_telemetry():
    t = telemetry; t.reset()
    t.record_outcome(outcome='success', latency_ms=42.0)
    t.record_outcome(outcome='timeout', latency_ms=30000.0)
    assert t.snapshot()['counters']['recall_succeeded'] == 1
    assert t.snapshot()['counters']['recall_timeout'] == 1
def t_breaker():
    cb = circuit_breaker.CircuitBreaker(window_sec=10, failure_threshold=3, cooldown_sec=0.5)
    cb.record('timeout'); cb.record('timeout'); assert not cb.should_skip()
    cb.record('timeout'); assert cb.should_skip()
def t_watchdog():
    wd = watchdog.WatchdogRegistry; wd.cancel_all()
    class C: id = 'ctx-r1'
    class A: context = C()
    a = A()
    async def ok(): await asyncio.sleep(0.01)
    async def run():
        tk = asyncio.create_task(ok())
        wd.track(a, tk, hard_cap_sec=2.0)
        await asyncio.sleep(0.05)
        wd.mark_completed(a)
    asyncio.run(run())

# Phase 2
def t_memorize():
    mw = memorize_watchdog; mw.MemorizeWatchdogRegistry.clear()
    class C: id = 'ctx-m1'
    class A: context = C()
    a = A()
    mw.MemorizeWatchdogRegistry.begin(a, phase='memorize')
    time.sleep(0.15)
    s = mw.MemorizeWatchdogRegistry.check(a, soft_cap_sec=0.05, hard_warn_sec=0.1)
    assert s is not None and s['level'] == 'hard'
    mw.MemorizeWatchdogRegistry.end(a)

def t_memorize_refcount_and_reset():
    # v0.5.4: concurrent memorize runs (fragments + solutions) share one
    # refcounted state; the run's warned flags reset once ALL runs end.
    mw = memorize_watchdog; mw.MemorizeWatchdogRegistry.clear()
    class C: id = 'ctx-m2'
    class A: context = C()
    a = A()
    mw.MemorizeWatchdogRegistry.begin(a, phase='fragments')
    mw.MemorizeWatchdogRegistry.begin(a, phase='solutions')
    time.sleep(0.05)
    # First finisher must NOT close the run.
    assert mw.MemorizeWatchdogRegistry.end(a) is None
    # Active run is checkable while one run is still in flight.
    s = mw.MemorizeWatchdogRegistry.check(a, soft_cap_sec=0.01, hard_warn_sec=0.2)
    assert s is not None and s['level'] == 'soft'
    duration = mw.MemorizeWatchdogRegistry.end(a)
    assert duration is not None and duration >= 0.05
    # Registry must be empty after the run ends (no leak, warned flags gone).
    assert mw.MemorizeWatchdogRegistry.snapshot() == {}
    # A NEW run starts clean -- no carried-over warned flags.
    mw.MemorizeWatchdogRegistry.begin(a, phase='memorize')
    s2 = mw.MemorizeWatchdogRegistry.check(a, soft_cap_sec=100.0, hard_warn_sec=200.0)
    assert s2 is None  # fresh run, far under caps
    mw.MemorizeWatchdogRegistry.end(a)
def t_faiss():
    r = faiss_health.probe_all()
    assert 'count' in r and 'results' in r
    info = faiss_health.probe_one('/nonexistent/index.faiss')
    assert info['warning'] == 'missing'
    # v0.6.0: staleness is configurable (was hardcoded 365d in probe_one
    # while probe_all default was 90d). Probe a REAL temp file with an old
    # mtime and max_age_days=0 -- probing a missing file short-circuits at
    # the FileNotFoundError branch, before the staleness check is reached.
    import tempfile, os as _os
    fd, tmp = tempfile.mkstemp(suffix='.faiss')
    _os.close(fd)
    try:
        old = _os.stat(tmp).st_mtime - (120 * 86400)  # 120 days ago
        _os.utime(tmp, (old, old))
        stale_info = faiss_health.probe_one(tmp, max_age_days=0)
        assert '|stale' in (stale_info['warning'] or ''), f"expected '|stale', got {stale_info['warning']!r}"
        fresh_info = faiss_health.probe_one(tmp, max_age_days=365)
        assert '|stale' not in (fresh_info['warning'] or '')
    finally:
        _os.unlink(tmp)
def t_auto_recover():
    rec = auto_recover.attempt_recovery('test-sub', '/nonexistent/index.faiss')
    assert rec.get('attempted') is True
    assert 'test-sub' in auto_recover.history()
def t_index_gc():
    res = index_gc.gc_once(idle_min=30, max_entries=16)
    assert 'inspected' in res
    snap = index_gc.snapshot()
    assert 'current_size' in snap

# Phase 3
def t_rate_limiter():
    rl = rate_limiter
    rl.reset()
    # first burst should succeed
    for i in range(5):
        assert rl.try_acquire('subdir-A', max_per_min=60, burst=5) is True
    # 6th should fail (burst exhausted)
    assert rl.try_acquire('subdir-A', max_per_min=60, burst=5) is False
    snap = rl.snapshot()
    assert snap['stats']['allowed'] >= 5
    assert snap['stats']['throttled'] >= 1
def t_per_subdir_breaker():
    psb = per_subdir_breaker
    psb.reset()
    kw = {'window_sec': 10, 'threshold': 2, 'cooldown_sec': 0.5}
    psb.record('subdir-A', 'timeout', **kw)
    psb.record('subdir-A', 'timeout', **kw)
    assert psb.should_skip('subdir-A', **kw) is True
    assert psb.should_skip('subdir-B', **kw) is False
    snap = psb.snapshot()
    assert 'subdir-A' in snap
    # subdir-B may appear in snapshot because should_skip lazily creates entries
    assert snap['subdir-A']['state'] == 'open'
    assert snap['subdir-A']['failure_count'] == 2
def t_quarantine():
    q = quarantine
    # scan with very short max_age to find candidates
    result = q.scan(max_age_days=0)
    assert 'candidates' in result
    assert 'scanned_at' in result
    # v0.6.0: snapshot() returns the last scan (was always None before)
    snap = q.snapshot()
    assert snap['last_scan'] is not None
    assert snap['last_scan']['scanned_at'] == result['scanned_at']
def t_memorize_canceller():
    mc = memorize_canceller
    mc.reset()
    mc.request_cancel('agent-1')
    assert mc.is_cancelled('agent-1') is True
    mc.clear('agent-1')
    assert mc.is_cancelled('agent-1') is False
    snap = mc.snapshot()
    assert snap['stats']['cancelled_cooperative'] == 1
def t_embedding_swap():
    es = embedding_swap
    es.reset()
    es.begin_swap('model-A', 'model-B', shadow_requests=3)
    sim = es.record_comparison(old_vec=[1.0, 0.0], new_vec=[1.0, 0.0])
    assert sim == 1.0
    assert es.should_commit(consensus_min=0.9) is False  # only 1 of 3
    es.record_comparison(old_vec=[1.0, 0.0], new_vec=[0.0, 1.0])  # sim=0
    es.record_comparison(old_vec=[1.0, 0.0], new_vec=[1.0, 0.0])  # sim=1
    # avg = (1+0+1)/3 = 0.67 < 0.8
    assert es.should_commit(consensus_min=0.8) is False
    rec = es.abort('test')
    assert rec is None  # abort does not return
    snap = es.snapshot()
    assert snap['stats']['swaps_aborted'] == 1


# v0.3.2 -- recall_patch helper tests
def t_recall_patch_initial():
    rp = recall_patch
    rp.reset_state()
    state = rp.apply_recall_patch(enabled=True)
    assert state["patch_attempts"] >= 1
    assert state["last_status"] in (
        "applied", "already_present", "import_error",
        "class_not_found", "setattr_error",
    )
    for k in ("applied", "already_present", "import_errors",
              "class_not_found", "patch_attempts"):
        assert isinstance(state[k], int) and state[k] >= 0


def t_recall_patch_idempotent():
    rp = recall_patch
    rp.reset_state()
    s1 = rp.apply_recall_patch(enabled=True)
    s2 = rp.apply_recall_patch(enabled=True)
    assert s2["patch_attempts"] == s1["patch_attempts"] + 1


def t_recall_patch_disabled():
    rp = recall_patch
    rp.reset_state()
    state = rp.apply_recall_patch(enabled=False)
    assert state["last_status"] == "disabled"
    assert state["patch_attempts"] == 0
    assert state["applied"] == 0


def t_recall_patch_state_shape():
    rp = recall_patch
    state = rp.get_state()
    required_keys = {
        "applied", "already_present", "import_errors",
        "class_not_found", "patch_attempts",
        "last_status", "last_error", "method_source", "method_size_bytes",
    }
    assert required_keys.issubset(state.keys())
    assert isinstance(state["method_source"], str)
    assert state["method_source"].startswith("embedded_last_known_good")


def t_history_clamp():
    hc = history_clamp
    hc.reset()
    # Markers match the RENDERED prompt content, not the prompt file
    # name (v0.5.4 fix -- file names never occur in live system prompts).
    memories_sentence = (
        "- The response format is a JSON array of text notes containing "
        "durable facts to memorize")
    # non-memory prompt -> skipped, message untouched
    cd = {"system": "You are a code reviewer.", "message": "x" * 100}
    assert hc.clamp(cd, None) == "skipped_non_memory"
    assert cd["message"] == "x" * 100
    # memory prompt over default budget -> clamped
    cd = {"system": memories_sentence, "message": "x" * 100_000}
    assert hc.clamp(cd, None, inject_notice=False) == "clamped"
    assert len(cd["message"]) == 50_000
    state = hc.get_state()
    assert state["clamped"] == 1
    assert state["skipped_non_memory"] == 1
    assert state["last_status"] == "clamped"
    # reset clears counters
    hc.reset()
    assert hc.get_state()["clamped"] == 0
    assert hc.get_state()["last_status"] == "never_run"


_CASES = [
    ('p1_telemetry', t_telemetry),
    ('p1_breaker', t_breaker),
    ('p1_watchdog', t_watchdog),
    ('p2_memorize', t_memorize),
    ('p2_memorize_refcount', t_memorize_refcount_and_reset),
    ('p2_faiss_health', t_faiss),
    ('p2_auto_recover', t_auto_recover),
    ('p2_index_gc', t_index_gc),
    ('p3_rate_limiter', t_rate_limiter),
    ('p3_per_subdir_breaker', t_per_subdir_breaker),
    ('p3_quarantine', t_quarantine),
    ('p3_memorize_canceller', t_memorize_canceller),
    ('p3_embedding_swap', t_embedding_swap),
    # v0.3.2
    ('p32_recall_patch_initial', t_recall_patch_initial),
    ('p32_recall_patch_idempotent', t_recall_patch_idempotent),
    ('p32_recall_patch_disabled', t_recall_patch_disabled),
    ('p32_recall_patch_state_shape', t_recall_patch_state_shape),
    # v0.5.0
    ('p5_history_clamp', t_history_clamp),
]

if __name__ == '__main__':
    # Standalone runner: python tests/test_helpers.py
    for name, fn in _CASES:
        _run_case(name, fn)

    print('=' * 70)
    total = 0; passed = 0
    for n, r in results:
        total += 1
        if r.startswith('PASS'): passed += 1
        print(f'  {n:30s} {r}')
    print('=' * 70)
    print(f'{passed}/{total} tests passed')
    sys.exit(0 if passed == total else 1)
else:
    # pytest: expose each case as a real test (exceptions propagate).
    import pytest

    @pytest.mark.parametrize("name,fn", _CASES, ids=[n for n, _ in _CASES])
    def test_case(name, fn):
        fn()
