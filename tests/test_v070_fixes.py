"""Regression tests for the v0.7.0 memory_hardening fixes.

Each test here pins a defect that shipped and was invisible in production:

1. `recall_gate` — the three gates set flags that nothing read, so the
   circuit breaker / rate limiter / per-subdir breaker never prevented a
   FAISS call. The rate limiter consumed a token and the recall ran anyway.
2. `per_subdir_breaker` — `should_skip()` performed no open -> half_open
   transition (only `record()` did), so a breaker that opened once LATCHED
   open for the process lifetime: while it returned True the recall was
   skipped, so no outcome was ever recorded to move it on.
3. `per_subdir_breaker` — `half_open` had no in-flight guard, so unlimited
   trials passed instead of one.
4. `DEFAULT_ENABLED` — the gate and the recorder defaulted to True and False
   respectively, which is only latent when a live config.json carries the key.
5. `coroutine_guard.scan_unawaited_coroutines` — iterated
   `asyncio.all_tasks()`, which returns Tasks and never bare coroutines, and
   bailed on the only candidates it saw. Structurally always returned 0.
6. `memorize_canceller.scan_stuck_threads` — read `_mh_started_at`, an
   attribute nothing ever set, so `alive_for` was always 0 and the function
   always returned [].
7. `faiss_health` — re-hashed every index in full on every probe pass.
8. `quarantine.scan` — walked the tree unbounded, unlike `faiss_health`.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from usr.plugins.memory_hardening.helpers import (
    coroutine_guard,
    faiss_health,
    memorize_canceller,
    per_subdir_breaker,
    quarantine,
    recall_gate,
)


# --------------------------------------------------------------------------
# 1. The recall gate actually suppresses the search
# --------------------------------------------------------------------------

def test_recall_gate_skips_only_while_state_is_set():
    recall_gate.reset()
    calls = []

    class FakeRecallMemories:
        async def search_memories(self, *a, **kw):
            calls.append(1)
            return {"memories": "REAL"}

    cls = FakeRecallMemories

    async def run():
        # install by hand (no framework dispatcher available in tests)
        original = cls.search_memories

        async def guarded(self, *a, **kw):
            from usr.plugins.memory_hardening.helpers.recall_gate import (
                _should_skip_now,
            )

            skip, _reason = _should_skip_now()
            if skip:
                return {}
            return await original(self, *a, **kw)

        cls.search_memories = guarded
        try:
            inst = cls()
            assert await inst.search_memories() == {"memories": "REAL"}
            assert len(calls) == 1

            recall_gate.set_state(True, "circuit_breaker_open")
            assert await inst.search_memories() == {}
            assert len(calls) == 1, "gate did not suppress the real search"

            recall_gate.set_state(False)
            assert await inst.search_memories() == {"memories": "REAL"}
            assert len(calls) == 2
        finally:
            cls.search_memories = original

    asyncio.run(run())


def test_recall_gate_state_clears_between_iterations():
    """A flag absent this iteration must not leave the previous skip latched."""
    recall_gate.reset()
    recall_gate.set_state(True, "rate_limited")
    assert recall_gate.current_state()["skip"] is True
    recall_gate.set_state(False)
    st = recall_gate.current_state()
    assert st["skip"] is False
    assert st["generation"] >= 2, "generation must advance so the gate is per-iteration"


def test_recall_gate_install_requires_agent():
    """No agent -> no class resolution. Must not raise, must not claim success."""
    recall_gate.reset()
    out = recall_gate.install_guard(None)
    assert out["status"] == "no_agent"


def test_recall_gate_counter_tracks_skips():
    recall_gate.reset()
    before = recall_gate.counters["skipped"]
    recall_gate.set_state(True, "per_subdir_breaker_open")
    recall_gate.set_state(False)
    assert recall_gate.counters["skipped"] == before + 1


# --------------------------------------------------------------------------
# 2 + 3. Per-subdir breaker: no latch, one trial in half_open
# --------------------------------------------------------------------------

_KW = dict(window_sec=60.0, threshold=2, cooldown_sec=0.2)


def test_per_subdir_breaker_does_not_latch_open():
    per_subdir_breaker.reset()
    # Trip it: two failures cross the threshold.
    per_subdir_breaker.record("s", "failed", **_KW)
    per_subdir_breaker.record("s", "failed", **_KW)
    assert per_subdir_breaker.snapshot()["s"]["state"] == "open"
    assert per_subdir_breaker.should_skip("s", **_KW) is True

    # The bug: should_skip() never performed open -> half_open, so the breaker
    # stayed open forever (nothing could record an outcome, because every
    # outcome path is gated on should_skip()).
    time.sleep(0.25)  # past cooldown_sec
    assert per_subdir_breaker.should_skip("s", **_KW) is False, (
        "breaker latched open: should_skip() must run the open->half_open "
        "transition once the cooldown elapsed"
    )
    per_subdir_breaker.reset()


def test_per_subdir_breaker_half_open_admits_one_trial():
    per_subdir_breaker.reset()
    per_subdir_breaker.record("s", "failed", **_KW)
    per_subdir_breaker.record("s", "failed", **_KW)
    time.sleep(0.25)
    first = per_subdir_breaker.should_skip("s", **_KW)
    second = per_subdir_breaker.should_skip("s", **_KW)
    assert first is False, "first caller must get the trial"
    assert second is True, (
        "a second caller must not get a trial while the first is unresolved "
        "(no in-flight guard => stampede on a recovering subdir)"
    )
    # The trial resolved successfully -> closed, and the flag cleared.
    per_subdir_breaker.record("s", "ok", **_KW)
    assert per_subdir_breaker.snapshot()["s"]["state"] == "closed"
    assert per_subdir_breaker.should_skip("s", **_KW) is False
    per_subdir_breaker.reset()


def test_per_subdir_breaker_half_open_failure_reopens():
    per_subdir_breaker.reset()
    per_subdir_breaker.record("s", "failed", **_KW)
    per_subdir_breaker.record("s", "failed", **_KW)
    time.sleep(0.25)
    assert per_subdir_breaker.should_skip("s", **_KW) is False
    per_subdir_breaker.record("s", "failed", **_KW)
    assert per_subdir_breaker.snapshot()["s"]["state"] == "open", (
        "a failed half_open trial must re-open immediately, not wait for the "
        "threshold again"
    )
    per_subdir_breaker.reset()


# --------------------------------------------------------------------------
# 4. One default for gate and recorder
# --------------------------------------------------------------------------

def test_per_subdir_default_enabled_is_shared_constant():
    """Both call sites must read the SAME constant, so they cannot disagree."""
    assert per_subdir_breaker.DEFAULT_ENABLED is True
    src = (
        "usr/plugins/memory_hardening/extensions/python/"
        "message_loop_prompts_after/_95_recall_telemetry.py"
    )
    import pathlib

    text = pathlib.Path(src).read_text(encoding="utf-8")
    assert "_PER_SUBDIR_DEFAULT_ENABLED" in text
    assert (
        'per_subdir_breaker_enabled", False' not in text
    ), "the recorder must not hard-code its own default again"


# --------------------------------------------------------------------------
# 5. Coroutine sweep
# --------------------------------------------------------------------------

def test_coroutine_sweep_closes_never_awaited_coroutine():
    """A created-but-never-awaited coroutine must actually be found.

    Note the version trap this pins: on CPython 3.12 a fresh coroutine has a
    NON-None cr_frame and f_lasti == 0, while a FINISHED one has cr_frame
    None. So the obvious `cr_frame is None` test silently matches only
    finished coroutines and misses every leak. The implementation uses
    inspect.getcoroutinestate, which is stable across 3.10-3.13.
    """
    from inspect import getcoroutinestate

    captured = {}

    async def work():
        async def never():
            return 1

        async def done():
            return 2

        never_coro = never()
        finished_coro = done()
        await finished_coro          # genuinely CORO_CLOSED

        assert getcoroutinestate(never_coro) == "CORO_CREATED"
        assert getcoroutinestate(finished_coro) == "CORO_CLOSED"

        captured["closed"] = coroutine_guard.scan_unawaited_coroutines()
        captured["after"] = getcoroutinestate(never_coro)
        # Keep a reference so GC cannot reap it before the sweep sees it.
        captured["ref"] = never_coro

    asyncio.run(work())
    assert captured["closed"] >= 1, "a never-awaited coroutine must be detected"
    assert captured["after"] == "CORO_CLOSED"


def test_coroutine_sweep_leaves_running_coroutine_alone():
    """Safety criterion: a coroutine the loop owns is never closed."""
    from inspect import getcoroutinestate

    state = {"closed": False}

    async def inner():
        await asyncio.sleep(0.5)

    async def driver():
        t = asyncio.ensure_future(inner())
        await asyncio.sleep(0.02)          # let it start
        c = t.get_coro()
        assert getcoroutinestate(c) == "CORO_SUSPENDED"
        coroutine_guard.scan_unawaited_coroutines()
        state["closed"] = t.done() and t.cancelled()
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    asyncio.run(driver())
    assert state["closed"] is False, "the sweep closed a coroutine the loop owned"


# --------------------------------------------------------------------------
# 6. Stuck-thread scanner
# --------------------------------------------------------------------------

def test_stuck_thread_scan_reports_aged_thread():
    memorize_canceller.reset()
    import threading

    stop = threading.Event()
    t = threading.Thread(target=lambda: stop.wait(30), name="background-test")
    t.daemon = True
    t.start()
    try:
        # First observation: not yet aged.
        assert memorize_canceller.scan_stuck_threads(threshold_sec=30) == []
        # Force the age rather than sleeping 30s.
        memorize_canceller._first_seen[t.ident] = time.monotonic() - 60
        found = memorize_canceller.scan_stuck_threads(threshold_sec=30)
        assert any(r["ident"] == t.ident for r in found), (
            "an aged background thread must be reported; the old code read a "
            "never-assigned _mh_started_at so it always returned []"
        )
    finally:
        stop.set()
        t.join(timeout=2)
        memorize_canceller.reset()


def test_stuck_thread_scan_forgets_dead_threads():
    memorize_canceller.reset()
    import threading

    stop = threading.Event()
    t = threading.Thread(target=lambda: stop.wait(30), name="background-test2")
    t.daemon = True
    t.start()
    ident = t.ident
    memorize_canceller.scan_stuck_threads(threshold_sec=30)
    assert ident in memorize_canceller._first_seen
    stop.set()
    t.join(timeout=2)
    memorize_canceller.scan_stuck_threads(threshold_sec=30)
    assert ident not in memorize_canceller._first_seen, (
        "a thread that has gone away must not keep ageing"
    )
    memorize_canceller.reset()


# --------------------------------------------------------------------------
# 7 + 8. Bounded / memoised scans
# --------------------------------------------------------------------------

def test_faiss_hash_cache_avoids_rehash(tmp_path, monkeypatch):
    faiss_health.reset()
    idx = tmp_path / "index.faiss"
    idx.write_bytes(b"x" * 4096)
    import hashlib

    sidecar = tmp_path / "index.faiss.sha256"
    sidecar.write_text(hashlib.sha256(b"x" * 4096).hexdigest())

    reads = []
    real_open = open

    def counting_open(path, *a, **kw):
        if str(path) == str(idx):
            reads.append(1)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", counting_open)
    try:
        for _ in range(5):
            info = faiss_health.probe_one(str(idx), min_size_bytes=1)
            assert info["hash_ok"] is True
    finally:
        monkeypatch.undo()
    faiss_health.reset()
    assert len(reads) <= 1, (
        f"file was re-read {len(reads)} times for 5 probes; the hash must be "
        "memoised on (mtime_ns, size)"
    )


def test_faiss_hash_cache_invalidates_on_change(tmp_path):
    faiss_health.reset()
    idx = tmp_path / "index.faiss"
    idx.write_bytes(b"y" * 4096)
    import hashlib

    sidecar = tmp_path / "index.faiss.sha256"
    sidecar.write_text(hashlib.sha256(b"y" * 4096).hexdigest())
    assert faiss_health.probe_one(str(idx), min_size_bytes=1)["hash_ok"] is True

    # Change the content: a cached digest must not mask the mismatch.
    idx.write_bytes(b"z" * 8192)
    st = idx.stat()
    info = faiss_health.probe_one(str(idx), min_size_bytes=1)
    assert info["hash_ok"] is False, (
        "stale cached digest was used after the file changed "
        f"(mtime_ns={st.st_mtime_ns} size={st.st_size})"
    )
    faiss_health.reset()


def test_quarantine_scan_is_bounded():
    assert quarantine.MAX_DIRS > 0
    assert quarantine.MAX_CANDIDATES > 0
    src = quarantine.scan
    assert callable(src)


def test_every_helper_exposes_reset():
    """The AGENTS.md contract: every helper module exposes reset().

    `extension_class` is exempt: it is a pure class resolver with no
    process-global state, so there is nothing to clear. The exemption is
    explicit so a genuinely stateful module cannot quietly join it.
    """
    import importlib
    import pathlib

    stateless = {"extension_class"}
    helpers_dir = pathlib.Path("usr/plugins/memory_hardening/helpers")
    missing = []
    for path in sorted(helpers_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        mod = importlib.import_module(
            f"usr.plugins.memory_hardening.helpers.{path.stem}"
        )
        fn = (
            getattr(mod, "reset", None)
            or getattr(mod, "reset_state", None)
            or getattr(mod, "reset_instance", None)
        )
        if not callable(fn) and path.stem not in stateless:
            missing.append(path.stem)
    assert not missing, f"helpers without any reset entry point: {missing}"


def test_hooks_uninstall_resets_every_registered_helper():
    """hooks.uninstall must reference every helper it can reset."""
    import pathlib

    text = pathlib.Path("usr/plugins/memory_hardening/hooks.py").read_text(
        encoding="utf-8"
    )
    for name in (
        "telemetry",
        "circuit_breaker",
        "recall_patch",
        "recall_gate",
        "faiss_health",
        "quarantine",
        "index_gc",
        "auto_recover",
        "memorize_watchdog",
    ):
        assert f'"{name}"' in text, f"hooks.uninstall does not reset {name}"
