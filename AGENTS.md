# memory_hardening

> Wraps the built-in `_memory` plugin with resilience, observability, and rate limiting. Adds circuit breakers per memory subdir, a recall gate that actually suppresses FAISS calls, embedding-swap on failure, memorize watchdog, and a memory-history clamp.

**Version:** 0.7.0 · **Plugin ID:** `memory_hardening` · Requires framework v2.5 extension hooks

## Purpose

Add resilience and observability around `_memory` without editing it. Zero source modifications to `plugins/_memory/` — everything here is extension hooks plus plugin-local helpers.

## THE FAIL-SAFE CONTRACT (read this first)

**An extension's `execute()` must never raise.** An exception propagates through `call_extensions_async` into `agent.monologue` and ends the agent's turn. A resilience plugin that can break the agent it protects is worse than no plugin.

This is not theoretical. On **2026-09-26** v0.7.0 added `per_subdir_breaker.DEFAULT_ENABLED` and read it as a bare attribute from `message_loop_start/_40_per_subdir_breaker.py`. A live agent stalled:

```
AttributeError: module 'usr.plugins.memory_hardening.helpers.per_subdir_breaker'
                has no attribute 'DEFAULT_ENABLED'
  File "/a0/helpers/extension.py", line 240, in call_extensions_async
  File "/a0/agent.py", line 418, in monologue
```

**Why:** the framework re-reads **extension** files on every dispatch, but imported **helper** modules stay cached in `sys.modules` for the process lifetime. After a plugin update, freshly-loaded extension code can run against a stale copy of a helper that predates any symbol it just started using.

Two rules follow, enforced by `tests/test_extension_failsafe.py` (38 cases, all extensions):

1. **Read cross-module constants with `getattr(obj, "NAME", <fallback>)`** — never `obj.NAME`. This includes module-level constants: a bare access at module scope fails to *load* the extension, which is just as fatal.
2. **Wrap the whole `execute()` body in `try/except`**, and on failure close any state you opened so a latched value cannot persist.

Helpers that are called from a hook must be non-raising at their boundary too: `recall_gate.install_guard_safe()` absorbs resolver failures, and `_04_recall_gate` closes the gate when it cannot evaluate the flags.

## Ownership / Layout

- `helpers/` — `extension_class` (dispatcher-class resolver, stateless), `recall_gate` (the gate that makes breakers bite), `circuit_breaker`, `rate_limiter`, `per_subdir_breaker`, `watchdog` (recall tasks), `memorize_watchdog` + `memorize_canceller`, `coroutine_guard`, `quarantine`, `index_gc`, `faiss_health`, `auto_recover`, `embedding_swap`, `recall_patch`, `recall_wait_guard`, `history_clamp`, `telemetry`.
- `hooks.py` — `install` / `pre_update` / `uninstall`. `uninstall` resets every helper it can (see below).
- `extensions/python/` — 17 files. Named points plus `message_loop_prompts_after/_04_recall_gate.py` (v0.7.0).
- `api/stats.py`, `api/reset_breaker.py` — both declare `get_methods()`.
- `webui/config.html` — uses the official `pluginSettingsPrototype` store and the framework's `fetchApi` (raw `fetch()` 403s on CSRF).

## Local Contracts

- **Extension ordering is file-basename sort.** Priorities are load-bearing: `_04_recall_gate` must run after the `message_loop_start` gates that set the flags (20/30/40) and before `_50_recall_memories` (50), which builds the recall task.
- **Never patch an extension-point class via a dotted-path import.** The framework loads extension files as synthetic modules named after the file basename, so a canonical `plugins._memory...` import yields a *second* class the dispatcher never touches. Always resolve through `extension_class.resolve_extension_class(agent, point, class_name, module_suffix, canonical_path)` — all five arguments. This was the v0.5.3 bug and it was structurally silent.
- **The recall gate is the only thing that suppresses a FAISS call.** `_20_circuit_breaker`, `_30_rate_limiter` and `_40_per_subdir_breaker` only set `params_temporary` flags; `_04_recall_gate` reads them and drives `recall_gate.set_state()`. Adding a flag without a consumer recreates the v0.6.0 defect, where all three recorded telemetry and changed nothing. The rate limiter was the worst case: it consumed a token, reported "limited", and the recall ran anyway.
- **The gate is patched once per class, state per iteration.** `install_guard` wraps `RecallMemories.search_memories` and the wrapper reads a live module-level state, so opening/closing the gate is a dict assignment rather than a re-patch. `set_state(False)` must be called every iteration — a flag absent this iteration must not leave the previous skip latched.
- **Breakers must be able to recover.** `per_subdir_breaker.should_skip()` runs the `open → half_open` transition itself; it did not, and because `should_skip()` is what gates the recall, no outcome could ever be recorded to move the breaker on — so a subdir that opened once stayed open for the process lifetime. `half_open` also needs an in-flight guard (one trial, not unlimited) exactly as `circuit_breaker.CircuitBreaker` has.
- **`per_subdir_breaker.DEFAULT_ENABLED` is the single source of truth** for the gate/recorder default, imported by both. They were `True` and `False` respectively, latent only because a live `config.json` carries the key.
- **`recall_wait_guard` wraps, `recall_patch` only fills a gap.** The guard substitutes a `safe_execute` wrapper (the method exists but is unguarded); the patch adds a method only when upstream removed it. The guard re-raises `CancelledError` and catches `TimeoutError` plus a generic net, so the loop survives while `_95_recall_telemetry` still feeds the breaker.
- **`params_temporary` is wiped every inner iteration; `params_persistent` survives to the end of a monologue.** Streaks, budgets and task handles belong in the latter.
- **Every helper exposes `reset()`** (or `reset_state`/`reset_instance`). The contract was documented and not kept: `hooks.uninstall` reset 7 of 16 modules, so a disable/enable cycle left an open circuit breaker, stale per-subdir events, a stale quarantine scan and growing auto-recover dicts behind. `uninstall` now walks an explicit list, resetting each independently so one import failure cannot skip the rest. `tests/test_v070_fixes.py` asserts both the per-module entry point and the `hooks.py` coverage list. `extension_class` is the only exemption — it is stateless.
- **Background scans are bounded and memoised.** `faiss_health` and `quarantine` both cap their walk (`MAX_DIRS`/`MAX_INDEXES`/`MAX_CANDIDATES`) because `usr/memory` is large, user-controlled in depth, and may sit on a bind mount. `faiss_health` memoises its integrity hash on `(mtime_ns, size)` — it used to stream every index in full on every 120 s probe, which is not a heuristic but a guarantee: unchanged mtime and size cannot hash differently.
- **Never close a coroutine the loop might own.** `coroutine_guard.scan_unawaited_coroutines()` uses `inspect.getcoroutinestate` and only touches `CORO_CREATED` / `CORO_CLOSED`. Do not hand-roll this: on CPython 3.12 a fresh coroutine has a **non-None `cr_frame` and `f_lasti == 0`**, and a *finished* one has `cr_frame is None` — so the intuitive `cr_frame is None` test silently matches only finished coroutines and misses every leak, while `f_lasti == -1` is right on 3.10 and wrong on 3.12.
- **Measure thread age by observation, not a magic attribute.** `memorize_canceller.scan_stuck_threads` used `getattr(t, "_mh_started_at", now)`, an attribute nothing ever set, so `alive_for` was always 0 and the function always returned `[]`. It now keeps its own first-seen map keyed by ident and drops entries not seen in the current pass. `is_cancelled()` is **advisory**: nothing inside the memorize task polls it, so it must not be presented as working cooperative cancellation.
- **`index_gc` eviction races are upstream-inherent.** `Memory.index` is a shared plain dict and `Memory.get` subscripts it directly; the framework's own `Memory.reload()` deletes the same key the same way. Do not add a lock the read path would not honour.

## Configuration

`default_config.yaml` + `api/stats.py` must agree. `helpers/plugins.get_plugin_config` does **not** merge `default_config.yaml` when a `config.json` exists, so the `cfg.get(key, <default>)` at each call site is the real fallback — keep them in sync with the YAML.

## Verification

- `pytest usr/plugins/memory_hardening/tests` — 97 tests.
- `pytest usr/plugins/memory_hardening/tests/test_extension_failsafe.py` — the fail-safe contract. Run this after ANY edit to an extension or a helper constant.
- Confirm the breakers bite: trip one, then check `GET /api/plugins/memory_hardening/stats` shows `recall_gate.counters.skipped` increasing. Before v0.7.0 `breaker.state` could read "open" while nothing was suppressed; the two are now separately visible.
- `history_clamp` markers must still match `plugins/_memory/prompts/memory.{memories_sum,solutions_sum}.sys.md`. Verified present; if upstream renames those files the clamp becomes a silent no-op.
- Disable/re-enable the plugin and confirm `/stats` counters are back at zero (`recall_gate`, `history_clamp`, `circuit_breaker`, `quarantine`, `index_gc`, `auto_recover`, `telemetry`).
- `plugin.yaml` is parsed by `PluginMetadata`, which ignores unknown keys. `author`/`license`/`homepage` were removed in v0.7.0 for that reason; they live in `README.md`.

## Store Submission

Contents go at the **repository root** of a standalone repo so the installer finds `plugin.yaml`. The hub index is a separate `index.yaml` in a fork of `agent0ai/a0-plugins` — there is no local `plugin-hub/` directory. The index CI validator requires folder name `^[a-z0-9_]+$` with no leading `_`, the remote `plugin.yaml` `name` to match the index folder name exactly, and only `title`/`description`/`github`/`tags`/`screenshots` within their limits. A root `LICENSE` is required for listing. `settings_sections` must list only `agent`/`external`. `always_enabled` stays `false`.

Plugin-local imports use the absolute `usr.plugins.memory_hardening.*` namespace, which is the prescribed convention for a plugin installed under `usr/plugins/<name>/` (Python 3 namespace packages — no `__init__.py` needed anywhere in the chain). Do not flatten the package or add `sys.path` hacks; do not "fix" these to `plugins.*`, which is only valid for bundled plugins shipped in this tree.

## See also

- `plugin.yaml` — manifest (parsed keys documented inline)
- `default_config.yaml` — defaults
- `README.md` — user-facing docs
- `tests/test_extension_failsafe.py` — the two fail-safe rules, enforced
- `tests/test_v070_fixes.py` — regressions for the gate, the breaker latch, the sweep and the scanners
- Framework: `helpers/plugins.py`, `helpers/extension.py` (dispatch + synthetic modules), `helpers/api.py` (CSRF), `helpers/settings.py`, `plugins/_memory/AGENTS.md` (the plugin being wrapped)
