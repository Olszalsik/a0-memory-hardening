"""Functional tests for the recall-wait TimeoutError guard (v0.5.2).

Injects a fake ``RecallWait`` + ``LoopData`` into ``sys.modules`` so the
helper can be exercised without the full Agent Zero stack. Verifies:

* apply() wraps execute and sets the guarded flag
* asyncio.TimeoutError is caught (loop survives)
* asyncio.CancelledError is re-raised (shutdown not swallowed)
* generic Exception is caught and logged (safety net)
* apply() is idempotent (second call -> already_guarded)
* enabled=False restores the original execute
* get_state() / reset_state() behave
"""
import sys
import types
import asyncio
import importlib

sys.path.insert(0, "/a0")
results = []


def check(name, fn):
    try:
        fn()
        results.append((name, "PASS"))
    except Exception as e:
        results.append((name, f"FAIL: {e}"))


def _install_fake_recalls(execute_fn):
    """Install a fresh fake _91_recall_wait module + agent.LoopData."""
    # parent packages
    for pkg in [
        "plugins",
        "plugins._memory",
        "plugins._memory.extensions",
        "plugins._memory.extensions.python",
        "plugins._memory.extensions.python.message_loop_prompts_after",
    ]:
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []  # mark as package
            sys.modules[pkg] = m

    mod = types.ModuleType(
        "plugins._memory.extensions.python.message_loop_prompts_after._91_recall_wait"
    )

    class RecallWait:
        pass

    RecallWait.execute = execute_fn
    mod.RecallWait = RecallWait
    sys.modules[
        "plugins._memory.extensions.python.message_loop_prompts_after._91_recall_wait"
    ] = mod

    # agent.LoopData
    if "agent" not in sys.modules:
        am = types.ModuleType("agent")

        class LoopData:
            pass

        am.LoopData = LoopData
        sys.modules["agent"] = am

    return RecallWait


class _FakeLog:
    """Models agent.context.log -- a Log object with a .log() method."""

    def __init__(self):
        self.logged = []

    def log(self, **kw):
        self.logged.append(kw)


class _FakeContext:
    def __init__(self):
        self.log = _FakeLog()


class _FakeAgent:
    def __init__(self):
        self.context = _FakeContext()


def _reload_helper():
    # Drop any cached helper + extension imports so STATE is fresh.
    for k in list(sys.modules):
        if k.startswith("usr.plugins.memory_hardening.helpers.recall_wait_guard"):
            del sys.modules[k]
    import importlib

    return importlib.import_module(
        "usr.plugins.memory_hardening.helpers.recall_wait_guard"
    )


def t_apply_wraps():
    async def boom(self, loop_data=None, **kw):
        raise asyncio.TimeoutError("30s")

    RW = _install_fake_recalls(boom)
    rwg = _reload_helper()
    rwg.reset_state()
    r = rwg.apply_recall_wait_guard(enabled=True)
    assert r["last_status"] == "applied", r
    assert getattr(RW, "_mh_wait_guarded", False) is True
    assert RW.execute.__wrapped__ is not None or hasattr(RW.execute, "__wrapped__")


def t_timeout_caught():
    async def boom(self, loop_data=None, **kw):
        raise asyncio.TimeoutError("30s budget")

    RW = _install_fake_recalls(boom)
    rwg = _reload_helper()
    rwg.reset_state()
    rwg.apply_recall_wait_guard(enabled=True)

    inst = RW()
    inst.agent = _FakeAgent()
    # Must not raise.
    asyncio.run(inst.execute())
    assert rwg.STATE["timeouts_caught"] == 1, rwg.STATE
    assert any("timed out" in str(l.get("heading", "")) for l in inst.agent.context.log.logged), \
        inst.agent.context.log.logged


def t_cancelled_reraised():
    async def boom(self, loop_data=None, **kw):
        raise asyncio.CancelledError()

    RW = _install_fake_recalls(boom)
    rwg = _reload_helper()
    rwg.reset_state()
    rwg.apply_recall_wait_guard(enabled=True)

    inst = RW()
    inst.agent = _FakeAgent()
    raised = False
    try:
        asyncio.run(inst.execute())
    except asyncio.CancelledError:
        raised = True
    assert raised, "CancelledError must propagate, not be swallowed"
    assert rwg.STATE["timeouts_caught"] == 0
    assert rwg.STATE["errors_caught"] == 0


def t_generic_exception_caught():
    async def boom(self, loop_data=None, **kw):
        raise RuntimeError("faiss exploded")

    RW = _install_fake_recalls(boom)
    rwg = _reload_helper()
    rwg.reset_state()
    rwg.apply_recall_wait_guard(enabled=True)

    inst = RW()
    inst.agent = _FakeAgent()
    # Must not raise.
    asyncio.run(inst.execute())
    assert rwg.STATE["errors_caught"] == 1, rwg.STATE
    assert "faiss exploded" in rwg.STATE["last_error"], rwg.STATE


def t_idempotent():
    async def ok(self, loop_data=None, **kw):
        return "ok"

    RW = _install_fake_recalls(ok)
    rwg = _reload_helper()
    rwg.reset_state()
    r1 = rwg.apply_recall_wait_guard(enabled=True)
    assert r1["last_status"] == "applied"
    r2 = rwg.apply_recall_wait_guard(enabled=True)
    assert r2["last_status"] == "already_guarded", r2
    assert rwg.STATE["applied"] == 1, rwg.STATE


def t_disable_restores():
    async def original(self, loop_data=None, **kw):
        return "orig"

    RW = _install_fake_recalls(original)
    rwg = _reload_helper()
    rwg.reset_state()
    rwg.apply_recall_wait_guard(enabled=True)
    original_execute = RW.execute.__wrapped__
    r = rwg.apply_recall_wait_guard(enabled=False)
    assert r["last_status"] == "disabled_restored", r
    assert RW.execute is original_execute
    assert getattr(RW, "_mh_wait_guarded", False) is False


def t_state_shape():
    rwg = _reload_helper()
    rwg.reset_state()
    s = rwg.get_state()
    for k in [
        "applied",
        "already_guarded",
        "restored",
        "timeouts_caught",
        "errors_caught",
        "import_errors",
        "patch_attempts",
        "last_status",
        "last_error",
    ]:
        assert k in s, f"missing key {k}"
    assert s["last_status"] == "never_run"
    # bump a counter then reset
    rwg.STATE["timeouts_caught"] = 5
    rwg.reset_state()
    assert rwg.STATE["timeouts_caught"] == 0


for name, fn in [
    ("apply_wraps", t_apply_wraps),
    ("timeout_caught", t_timeout_caught),
    ("cancelled_reraised", t_cancelled_reraised),
    ("generic_exception_caught", t_generic_exception_caught),
    ("idempotent", t_idempotent),
    ("disable_restores", t_disable_restores),
    ("state_shape", t_state_shape),
]:
    check(name, fn)

print("=" * 70)
total = passed = 0
for n, r in results:
    total += 1
    if r.startswith("PASS"):
        passed += 1
    print(f"  {n:28s} {r}")
print("=" * 70)
print(f"{passed}/{total} tests passed")
sys.exit(0 if passed == total else 1)