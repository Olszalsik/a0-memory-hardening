# Changelog

All notable changes to `memory_hardening` are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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