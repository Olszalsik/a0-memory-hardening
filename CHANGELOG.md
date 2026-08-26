# Changelog

All notable changes to `memory_hardening` are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.2] - 2026-08-26

### Added — recall-wait `TimeoutError` guard (stops the 30s recall timeout from killing the agent loop)

The built-in `_memory` plugin's `_91_recall_wait.py` does `await task` with no `try/except`, where `task = asyncio.wait_for(search_memories(...), timeout=SEARCH_TIMEOUT=30)`. When the 30s budget fires, `asyncio.TimeoutError` propagates out of `RecallWait.execute` → `prepare_prompt` → `monologue` and tears down the whole agent loop. Historical logs in this install show ~1268 such crashes. `memory_hardening` already runtime-patches `search_memories` (the v1.2.0 regression, `recall_patch`) but did **not** guard the outer `await task` — that gap remained.

- **`helpers/recall_wait_guard.py`** — `apply_recall_wait_guard(enabled=True)` **wraps** (does not replace) `RecallWait.execute` with a `safe_execute` wrapper that calls the original inside `try/except`. This is resilient to upstream changes to the method body (unlike `recall_patch`, which only acts when the method is *missing* — here the method is present but unguarded). `asyncio.CancelledError` is re-raised (never swallow loop shutdown); `asyncio.TimeoutError` is caught + logged (memory recall is best-effort); a generic `Exception` safety net catches any other recall-path failure with a loud warning so real bugs stay visible. Idempotent (`_mh_wait_guarded` flag); reversible (`enabled=False` restores the original). Telemetry via `get_state()` / `reset_state()`.
- **`extensions/python/message_loop_prompts_after/_06_recall_wait_guard.py`** — `RecallWaitGuard` extension at priority 6 (after `_05_recall_method_patch` at 5, before `_50_recall_memories` at 50 and `_91_recall_wait` at 91). Reads `recall_wait_guard_enabled` from config and applies the guard each loop iteration — idempotent, zero-cost after the first wrap.
- **Interaction with telemetry/breaker:** the downstream `_95_recall_telemetry` observer (priority 95) still inspects `task.exception()`, sees the `TimeoutError`, classifies the outcome as `timeout`, and feeds the circuit breaker — so backoff still happens. This guard only keeps the loop alive long enough for that to happen.
- **`api/stats.py`** — exposes `recall_wait_guard` state + a `recall_wait_guard_enabled` config key.
- **`default_config.yaml`** — adds `recall_wait_guard_enabled: true` (recommended: ON) with documentation.
- **`webui/config.html`** — adds a "Recall-Wait TimeoutError Guard" toggle card with live stats (timeouts caught / errors caught / last status).
- **`hooks.py`** — `uninstall()` now restores the original `RecallWait.execute` (un-guard) and resets the guard's telemetry state.
- **`tests/test_extensions.py`** — extension count 16 → 17; version assertion 0.5.1 → 0.5.2.
- **`tests/test_recall_wait_guard.py`** — new dedicated suite (7/7): apply-wraps, timeout-caught, cancelled-reraised, generic-exception-caught, idempotent, disable-restores, state-shape. Integration smoke-tested against the real `_memory.RecallWait` class (apply / idempotent / restore all green).
- Version 0.5.1 → 0.5.2 across `plugin.yaml`, `AGENTS.md`, `CHANGELOG.md`, `README.md`, `default_config.yaml`, `webui/config.html`, `api/stats.py`, `hooks.py`, `tests/test_extensions.py`. `config.json` is gitignored (sacred, not committed).

## [0.5.1] - 2026-08-20

### Fixed — settings did not save (regression after the v2.9 framework merge)

The settings screen ticked boxes but **nothing persisted**: on reopen every toggle reverted. Root cause: `webui/config.html` ran its own Alpine component (`alpineState()`) whose custom Save button POSTed `action: "config_set"` to `/api/plugins`. The v2.9 framework's `api/plugins.py` only handles `get_config` / `save_config` — there is **no `config_set` action**, so the Save button got a `400 Unknown action` and wrote nothing. The official footer Save button (which does work, via `save_config`) bound to a *different* object (`pluginSettingsPrototype.settings`) that the checkboxes never touched, so it couldn't save your ticks either.

- **`webui/config.html`** — migrated to the official plugin-settings store (the same pattern `_chat_naming` / `_model_fallback` use):
  - Checkboxes now bind `x-model="config.<key>"` where `config` is the parent modal scope's getter for `pluginSettingsPrototype.settings` — the **same object** the footer Save serializes via `save_config` → `config.json`. Toggles now persist.
  - `config` is referenced directly (not captured) so **Reset to default** — which replaces the settings object — is reflected live.
  - `mhBackfill(config)` seeds recommended defaults for keys ABSENT in the loaded config (core features ON, the "Advanced Features" set OFF). A present `null` (e.g. `history_clamp_max_chars_override`) is a real value and is left untouched.
  - Removed the broken custom Save button; the footer **Save** + **Reset to default** buttons handle persistence. Kept the read-only live-status panel (polls `/stats`), the **Refresh status** and **Reset Breaker** buttons.
  - Folded the orphaned vanilla-DOM "Recall Method Patch" card into the Alpine tree (`x-model="config.recall_patch_enabled"`) so it saves like every other toggle, with live status from `stats.recall_patch`.
- **`plugin.yaml`** — version 0.5.0 → 0.5.1; `webui/config.html` title, `AGENTS.md`, `README.md` version lines updated; `tests/test_extensions.py` version assertion updated to 0.5.1.
- **`api/stats.py`** — aligned the `/stats` `config` payload's Phase-3 defaults (`per_subdir_breaker_enabled`, `adaptive_interval_enabled`, `memorize_hard_cancel_enabled`) with `default_config.yaml` (`False` → `True`); they previously contradicted the recommended defaults. Cosmetic after the UI migration (the toggles no longer read this payload) but removes the latent inconsistency.
- **`webui/config.html` (follow-up)** — the **Refresh status** and **Reset Breaker** buttons returned `403 "CSRF token missing or invalid"`. They used raw `fetch()` to the plugin's `/stats` and `/reset_breaker` endpoints, but those handlers inherit `ApiHandler.requires_csrf() = True`, so a POST without the `X-CSRF-Token` header is rejected. Switched both calls to the framework's `fetchApi` (from `/js/api.js`), which attaches the CSRF token and auto-retries once on token expiry. The script became an ES module so it can `import { fetchApi }`; the x-component loader awaits all module imports before appending the body, so `window.mhStats / mhBackfill / mhActiveCount` are still defined before Alpine evaluates `x-data`. (This raw-fetch CSRF bug was inherited from the pre-v0.5.1 config.html — it was latent until someone actually clicked those buttons.)

### Default state (unchanged, now actually reachable)

`default_config.yaml` already encodes "core recommended ON, advanced extras OFF" (Phase 1-4 features ON; quarantine / embedding-swap / agent-notice OFF). The bug was that you could never save a *change* to it. After this fix, **Reset to default** loads that baseline and **Save** keeps it.

### Tests

- `test_extensions.py::t_webui` still passes — it only checks `config.html` starts with `<html>` and contains the phase-3 marker strings, all preserved.
- `test_extensions.py::t_manifest` updated to assert version `0.5.1`.
- No Python helper/extension changed; `test_helpers.py` / `test_history_clamp.py` unaffected.
- NOTE: the test files hardcode `/a0/...` container paths, so they only run inside the container (host is Windows at `E:\...`). 28/29 `test_history_clamp` cases pass on host; the 1 failure is a `/a0` path-resolution issue, not a regression.

## [0.5.0] - 2026-08-10

### Added — Memory History Clamp (merged from `_memory_resilience`)

The standalone `_memory_resilience` plugin (v1.0.0) is folded into `memory_hardening`. Both were memory patches for agent-zero; consolidating them means a single plugin owns all memory protection and a single install/enable toggle covers every layer. The standalone plugin is removed once its single extension moves here.

- **`helpers/history_clamp.py`** — the clamp logic + a process-global `STATE` telemetry dict. Exposes:
  - `clamp(call_data, agent, *, own_override=None, inject_notice=True)` — mutates `call_data["message"]` when the system prompt is a memory-recall / memorize / solve prompt and the message exceeds the budget; returns a status string; never raises.
  - `_looks_like_memory_prompt(system)` — substring match on the official memory system-prompt file names.
  - `_resolve_budget(agent, own_override)` — budget resolution: override → `_model_fallback.memory_memorize_max_chars` → `50000`. Booleans are rejected so a `true` config is not read as budget `1`.
  - `get_state()` / `reset()` — for `/stats` and `uninstall()`.
- **`extensions/python/util_model_call_before/_10_clamp_memory_history.py`** — thin `ClampMemoryUtilCall` extension. Reads `memory_hardening` config, respects `hardening_enabled` + `history_clamp_enabled`, then calls `history_clamp.clamp(...)`. Runs at priority `_10`, after the `_model_fallback` cascade hooks at `_00` / `_01`.
- **`default_config.yaml`** — new `history_clamp_enabled` (default ON), `history_clamp_max_chars_override` (null), `history_clamp_inject_truncation_notice` (true) section.
- **`api/stats.py`** — adds `history_clamp` field to `/stats` payload + `history_clamp_enabled` in `config`.
- **`webui/config.html`** — new **Memory History Clamp** card (toggle + truncation-notice toggle + max-chars override) and a live status line; feature count 13 → 14; title version v0.5.0.
- **`hooks.py`** — `uninstall()` now resets `history_clamp.STATE` (no telemetry leak across enable/disable cycles).
- **`tests/test_history_clamp.py`** — 29 tests ported from `_memory_resilience/tests/test_clamp_v1.py`, retargeted to `memory_hardening.helpers.history_clamp` (prompt detection, budget resolution incl. bool rejection, clamp behaviour, bug-safety, extension config wiring).
- **`tests/test_helpers.py`** — adds `t_history_clamp` (smoke test of clamp + reset); 17 → 18 tests.
- **`tests/test_extensions.py`** — registers `_10_clamp_memory_history.py` → `ClampMemoryUtilCall`; expected extension count 15 → 16; version check 0.4.0 → 0.5.0; adds `util_model_call_before` hook point.
- **`plugin.yaml`** — version 0.4.0 → 0.5.0; description updated to mention the merged clamp.
- **`README.md` / `AGENTS.md`** — document the merged clamp, budget resolution, and the `util_model_call_before` ordering contract.

### Fixed

- **`ContextOverflow` from the official `MAX_MSGS_CHARS = 80000` budget** during memorize / solve utility calls (the cascade can't recover in a single cycle because every candidate returns the same overflow). The clamp caps the history to the budget before the call.

### Removed

- The standalone `_memory_resilience` plugin — its single extension now lives here as `history_clamp`.

### Tests

- `test_helpers.py`: **18/18 PASS** (was 17/17)
- `test_extensions.py`: **5/5 PASS**
- `test_history_clamp.py`: **29/29 PASS**
- **Total: 52/52 PASS**

## [0.4.0] - 2026-07-26

### Added — Recall Method Patch (bug-fix layer)

- **`helpers/recall_patch.py`** — embeds the original `search_memories()` method body (copied verbatim from `memory_fix_backups/_50_recall_memories.py.bak`, pre-v1.2.0) and exposes:
  - `apply_recall_patch(enabled=True)` — idempotent, safe to call on every loop iteration
  - `get_state()` — for `/stats` API
  - `reset_state()` — for tests + manual reset
  - Module-level `STATE` dict tracks `applied`, `already_present`, `import_errors`, `class_not_found`, `patch_attempts`, `last_status`, `last_error`, `method_source`, `method_size_bytes`
- **`extensions/python/message_loop_prompts_after/_05_recall_method_patch.py`** — Extension subclass at priority 5 (before `_50_recall_memories` at priority 50). On every message loop:
  1. Reads `recall_patch_enabled` from `memory_hardening` config (default `true`)
  2. Calls `apply_recall_patch(enabled=...)`
  3. On first successful patch, logs a one-time info message
- **`default_config.yaml`** — new `recall_patch_enabled: true` section with documentation
- **`api/stats.py`** — adds `recall_patch` field to `/stats` payload + `recall_patch_enabled` in `config` section
- **`webui/config.html`** — new **Recall Method Patch** card with toggle + 7 live telemetry fields
- **`tests/test_helpers.py`** — 4 new tests:
  - `p32_recall_patch_initial` — first call sets state correctly
  - `p32_recall_patch_idempotent` — second call increments `patch_attempts` without re-applying
  - `p32_recall_patch_disabled` — `enabled=False` short-circuits
  - `p32_recall_patch_state_shape` — `get_state()` returns the expected keys
- **`tests/test_extensions.py`** — registers `_05_recall_method_patch.py` -> `RecallMethodPatch`, bumps expected extension count 14->15 and version check 0.3.1->0.4.0
- **`AGENTS.md`** — adds the `v0.4.0 recall_patch contract` section
- **`README.md`** — adds the *What the v0.4.0 recall_patch does* section + Recall Method Patch row in the feature table

### Fixed

- **`AttributeError: 'RecallMemories' object has no attribute 'search_memories'`** raised by `_50_recall_memories._safe_recall` after the upstream `_memory` plugin v1.2.0 refactor (which added the wrapper but accidentally removed the method it calls). The patch restores the method at runtime without modifying source code.

### Tests

- `test_helpers.py`: **17/17 PASS** (was 13/13)
- `test_extensions.py`: **5/5 PASS** (was 5/5)
- **Total: 22/22 PASS**

## [0.4.0] - 2026-07-19

### Added — Coroutine Guard + UI-loop pulse (Phase 4)

- **`helpers/coroutine_guard.py`** — leaks-aware close helper:
  - `close_inner_coro(coro)` — closes leaked inner coroutines on `asyncio.wait_for` cancellation
  - `on_long_sleep_tick(name)` — periodic heartbeat for `_model_fallback` long sleeps to keep the WebSocket dispatcher alive
  - `get_tick_snapshot()` — for `/stats` API
  - Conservative allowlist `_CLOSEABLE_CORO_NAME_PREFIXES`
- **`extensions/python/job_loop/_70_coroutine_guard_sweep.py`** — periodic best-effort cleanup of leaked `asyncio` coroutines
- **`usr/plugins/_model_fallback/AGENTS.md`** — updated to describe lazy-import hook in `fallback._yielding_sleep`
- **`default_config.yaml`** — new `coroutine_guard_enabled` config key (default ON)
- **`api/stats.py`** — adds `coroutine_guard` field with tick snapshot

### Fixed

- WebSocket dispatcher freeze during multi-hour provider outages (openai/httpx coroutines leaked when `wait_for` cancelled outer tasks)

## [0.3.0] - 2026-07-13

### Added — Rate Limiting, Per-Subdir Breakers, Adaptive Interval (Phase 3)

- **`helpers/rate_limiter.py`** — per-subdir token bucket. Caps FAISS recall rate per memory subdir.
- **`helpers/per_subdir_breaker.py`** — one circuit breaker per memory subdir.
- **`helpers/adaptive_interval.py`** — dynamically widens `memory_recall_interval` from 3 to 10 when breaker tripping.
- **`helpers/memorize_canceller.py`** — cooperative cancellation + stuck-thread scanner for `DeferredTask` workers.
- **`helpers/quarantine.py`** — auto-archives memory indexes older than N days.
- **`helpers/embedding_swap.py`** — shadow validation framework for embedding model changes.
- **6 new extensions** at priority 30/40/50/60 across `message_loop_start`, `monologue_end`, `job_loop`.
- **`default_config.yaml`** — 20 new config keys for Phase 3.
- **`webui/config.html`** — dashboard with Phase 3 sections.

### Tests

- Total: 18/18 PASS (added 6 new helper tests + 1 new extension count)

## [0.2.0] - 2026-07-10

### Added — Index GC, FAISS Health, Auto-Recovery, Dashboard (Phase 2)

- **`helpers/memorize_watchdog.py`** — `memorize` background-thread watchdog (warning only, Phase 1 had no cancel).
- **`helpers/index_gc.py`** — LRU eviction for `Memory.index[memory_subdir]` (process-global dict that never freed).
- **`helpers/faiss_health.py`** — reports index size, age, hash verification.
- **`helpers/auto_recover.py`** — transparent rebuild on FAISS corruption.
- **4 new extensions** at `monologue_end/_20`, `job_loop/_30/_40`, `embedding_model_changed/_20`.
- **`webui/config.html`** — first dashboard version (14807 bytes).
- **`api/stats.py`** — adds `faiss_health`, `index_gc`, `memorize_watchdogs`, `auto_recover` sections.

### Tests

- Total: 13/13 PASS

## [0.1.0] - 2026-07-05

### Added — Watchdog, Circuit Breaker, Telemetry (Phase 1, first public release)

- **`helpers/telemetry.py`** — counters + latency snapshots for recall outcomes.
- **`helpers/watchdog.py`** — agent-scoped recall-task tracker.
- **`helpers/circuit_breaker.py`** — global circuit breaker (3 failures in 5 min opens, 60s cooldown).
- **5 extensions** at `message_loop_start/_10/_20`, `message_loop_prompts_after/_95`, `monologue_end/_10`, `system_prompt/_05`.
- **`api/stats.py`** — initial `/stats` endpoint.
- **`api/reset_breaker.py`** — `POST /api/plugins/memory_hardening/reset_breaker`.
- **`hooks.py`** — `install()` / `uninstall()` lifecycle hooks.
- **`plugin.yaml`** — manifest.