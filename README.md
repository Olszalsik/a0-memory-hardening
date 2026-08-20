# memory_hardening

**Version:** 0.5.1 · **Plugin ID:** `memory_hardening` · **Author:** Agent Zero Plugin (memory_hardening) · **License:** MIT

## What it does

Wraps Agent Zero's built-in `_memory` plugin with resilience, observability, rate-limiting, and circuit-breaker protection against:

- **FAISS recall timeouts** that freeze the message loop
- **Leaked asyncio tasks** that pin memory across agents and projects
- **Stuck `memorize` background threads** that never report back
- **Unbounded `Memory.index` global cache** that leaks across many agents
- **Thundering-retry patterns** after a slow FAISS query
- **Coroutine leaks** from `asyncio.wait_for` cancellations during multi-hour provider outages
- **Missing `search_memories` method** after the upstream `_memory` v1.2.0 regression (v0.4.0)
- **ContextOverflow on memorize / solve** from the official `MAX_MSGS_CHARS = 80000` budget being too large for a 32k utility model (v0.5.0, merged from `_memory_resilience`)

Zero source modifications to `/a0/plugins/_memory/` -- purely additive extension hooks.

## Quick start

The plugin is auto-discovered. To enable it:

```
touch /a0/usr/plugins/memory_hardening/.toggle-1
```

That's it. Open **Settings -> Agent -> Memory Hardening** to see the live dashboard with all features enabled at recommended defaults.

## Features by version

| Phase | Version | Date | Feature | Default |
|---|---|---|---|---|
| 1 | 0.1.0 | 2026-07 | Watchdog (recall task tracker) | ON |
| 1 | 0.1.0 | 2026-07 | Circuit breaker (3 failures in 5 min) | ON |
| 1 | 0.1.0 | 2026-07 | Telemetry (latency, success/fail) | ON |
| 1 | 0.1.0 | 2026-07 | Health probe (FAISS + stuck tasks) | ON |
| 1 | 0.1.0 | 2026-07 | Agent notice (one-liner when breaker open) | OFF |
| 2 | 0.2.0 | 2026-07 | Memorize watchdog | ON |
| 2 | 0.2.0 | 2026-07 | Index cache GC (LRU, idle eviction) | ON |
| 2 | 0.2.0 | 2026-07 | Auto-recovery (quarantine + rebuild) | ON |
| 2 | 0.2.0 | 2026-07 | WebUI dashboard | ON |
| 3 | 0.3.0 | 2026-07 | Rate limiter (per-subdir token bucket) | ON |
| 3 | 0.3.0 | 2026-07 | Per-subdir circuit breaker | ON |
| 3 | 0.3.0 | 2026-07 | Adaptive recall interval (3-10) | ON |
| 3 | 0.3.0 | 2026-07 | Memorize hard cancel | ON |
| 3 | 0.3.0 | 2026-07 | Quarantine (archive old indexes) | OFF |
| 3 | 0.3.0 | 2026-07 | Embedding hot-swap (shadow validation) | OFF |
| 4 | 0.4.0 | 2026-07-19 | Coroutine guard + UI-loop pulse | ON |
| 4 | 0.4.0 | 2026-07-26 | **Recall method patch** (`_memory` v1.2.0 regression) | **ON** |
| 5 | 0.5.0 | 2026-08-10 | **Memory history clamp** (merged from `_memory_resilience`) | **ON** |

## What the v0.4.0 recall_patch does

The `_memory` plugin v1.2.0 added a `_safe_recall` wrapper but accidentally deleted the `search_memories` method it calls. Without this patch, every memory recall silently fails with:

```
AttributeError: 'RecallMemories' object has no attribute 'search_memories'
```

The recall method patch in v0.4.0:

1. Detects the missing method at runtime
2. Injects the original `search_memories` implementation (copied verbatim from the pre-v1.2.0 backup) via `setattr`
3. Marks the class with `_memory_hardening_patched = True` to prevent re-patching
4. Is fully forward-compatible: if a future `_memory` update restores the method, the patch becomes a no-op (`last_status: already_present`)
5. Can be toggled off from **Settings -> Agent -> Memory Hardening** if you want to use the upstream version directly

Check the dashboard card **Recall Method Patch** or query `GET /api/plugins/memory_hardening/stats.recall_patch` for live status.

## What the v0.5.0 history clamp does

The official `_memory` memorize / solve extensions truncate the chat history to `MAX_MSGS_CHARS = 80000` before calling the utility model. That budget is too large for a 32k-context utility model and causes `ContextOverflow` errors the cascade can't recover from in a single cycle (every candidate returns the same overflow).

The history clamp (merged in v0.5.0 from the former `_memory_resilience` plugin) hooks `util_model_call_before` and caps `call_data["message"]` when the system prompt matches a memory-recall / memorize / solve pattern. The budget is read in this order:

1. `memory_hardening.history_clamp_max_chars_override` (if a positive int)
2. `_model_fallback.memory_memorize_max_chars` (default 50000) — so a single setting governs both the cascade timeout budget and the memory budget
3. `50000` (fallback default)

The clamp is a safe no-op when the history is already within budget, and it never crashes the utility call (any error leaves `call_data` unchanged). Check the dashboard card **Memory History Clamp** or query `GET /api/plugins/memory_hardening/stats.history_clamp` for live counters.

## Telemetry

`GET /api/plugins/memory_hardening/stats` returns:

```json
{
  "telemetry": { "counters": {...}, "latency": {p50, p95, p99} },
  "breaker": { "state": "closed|open|half_open" },
  "watchdogs": { ... },
  "memorize_watchdogs": { ... },
  "index_gc": { ... },
  "faiss_health": { ... },
  "auto_recover": { ... },
  "rate_limiter": { ... },
  "per_subdir_breaker": { ... },
  "adaptive_interval": { ... },
  "memorize_canceller": { ... },
  "embedding_swap": { ... },
  "coroutine_guard": { "ticks": {...} },
  "recall_patch": { "applied": 1, "last_status": "applied", ... },
  "history_clamp": { "clamped": 0, "last_budget": 50000, "last_status": "never_run", ... },
  "config": { ...all keys... }
}
```

## Configuration

All 52 config keys live in `default_config.yaml`. Recommended values are loaded on first run. To customize, either edit `default_config.yaml` or use the WebUI settings panel.

Key toggles (all Phase 1+2+3+4 features default ON unless noted):

- `hardening_enabled` — master kill switch
- `watchdog_enabled` — recall task watchdog
- `breaker_enabled` — global circuit breaker
- `telemetry_enabled` — collects metrics (tiny CPU cost)
- `health_probe_enabled` — periodic FAISS scan
- `memorize_watchdog_enabled` — background-memorize watchdog
- `index_gc_enabled` — `Memory.index` LRU eviction
- `auto_recover_enabled` — quarantine + rebuild on FAISS corruption
- `rate_limiter_enabled` — per-subdir token bucket
- `per_subdir_breaker_enabled` — one breaker per memory subdir
- `adaptive_interval_enabled` — dynamic `memory_recall_interval`
- `memorize_hard_cancel_enabled` — cooperative cancel + stuck-thread scan
- `quarantine_enabled` — auto-archive old indexes (OFF by default)
- `embedding_swap_enabled` — shadow validation (OFF by default)
- `coroutine_guard_enabled` — leaked-coro cleanup
- `recall_patch_enabled` — restores missing `search_memories` (v0.4.0)
- `history_clamp_enabled` — clamp memorize/solve util-model history (v0.5.0)
- `history_clamp_max_chars_override` — positive-int override; null reads from `_model_fallback.memory_memorize_max_chars`
- `history_clamp_inject_truncation_notice` — append a trim notice to the clamped history

For full key listing with safe defaults, see `default_config.yaml`.

## Installation

The plugin ships in `/a0/usr/plugins/memory_hardening/`. To enable:

```
# enable
touch /a0/usr/plugins/memory_hardening/.toggle-1

# disable
touch /a0/usr/plugins/memory_hardening/.toggle-0
```

When enabled, the framework auto-loads:
- `hooks.py` -> `install()` on first load, `uninstall()` on disable / pre-update
- All extension hooks under `extensions/python/`
- All API handlers under `api/` (e.g. `GET /api/plugins/memory_hardening/stats`)
- The WebUI settings panel from `webui/config.html`

## WebUI dashboard

**Settings -> Agent -> Memory Hardening** shows one card per feature:

- Watchdog (active count, reaped, cancelled)
- Circuit Breaker (state, window, threshold, cooldown)
- Per-Subdir Breaker (states per memory subdir)
- Telemetry (success/fail counters, latency p50/p95/p99)
- Health Probe (FAISS index health, stuck task reports)
- Rate Limiter (allowed/throttled per subdir)
- Adaptive Interval (current interval, target latency)
- Quarantine (archived indexes)
- Embedding Hot-Swap (active swap, history)
- Coroutine Guard (tick count, last tick age)
- Memorize Watchdog + Memorize Canceller
- Index Cache GC (current size, evicted total)
- Auto-Recovery (rebuild history)
- **Recall Method Patch** (v0.4.0)

Each card has a checkbox to toggle its feature on/off and an "Advanced" expander to tune thresholds. Click **Refresh** for instant update; otherwise the dashboard auto-refreshes every 5 seconds.

## API endpoints

| Route | Method | Purpose |
|---|---|---|
| `/api/plugins/memory_hardening/stats` | GET | Full telemetry snapshot (52 keys) |
| `/api/plugins/memory_hardening/reset_breaker` | POST | Force-reset the global circuit breaker |

Example:

```
curl http://localhost:50001/api/plugins/memory_hardening/stats | jq .recall_patch
```

## Tests

Run from the plugin directory:

```
cd /a0/usr/plugins/memory_hardening
/opt/venv/bin/python tests/test_helpers.py       # 18/18 tests
/opt/venv/bin/python tests/test_extensions.py    # 5/5 tests
/opt/venv/bin/python tests/test_history_clamp.py # 29/29 tests
```

Total: **52/52 tests pass**.

## Files

```
memory_hardening/
├── AGENTS.md           # developer-facing architecture docs
├── README.md           # this file (user-facing overview)
├── USER_MANUAL.md      # step-by-step usage guide
├── CHANGELOG.md        # version history
├── plugin.yaml         # manifest
├── default_config.yaml # all 52 config keys with recommended values
├── hooks.py            # install / uninstall lifecycle
├── helpers/            # 15 helpers (circuit_breaker, recall_patch, history_clamp, ...)
├── extensions/         # 16 extension hooks across the lifecycle
├── api/                # stats.py + reset_breaker.py
├── webui/              # config.html dashboard
└── tests/              # test_helpers.py + test_extensions.py + test_history_clamp.py
```

## Compatibility

- **Agent Zero framework:** v2.5+
- **`_memory` plugin:** v0.x, v1.0, v1.1, v1.2 (with v0.4.0 recall_patch), and future v1.3+ (patch becomes no-op)
- **Embedding models:** any (shadow validation in `embedding_swap` adds optional zero-downtime model changes)
- **OS:** Linux (primary), macOS (best-effort)

## License

MIT -- see `LICENSE`.

## See also

- `USER_MANUAL.md` — practical recipes and step-by-step instructions
- `CHANGELOG.md` — version-by-version changes
- `AGENTS.md` — developer-facing architecture and contracts
- `/a0/plugins/_memory/extensions/python/_50_recall_memories.py` — the file patched at runtime in v0.4.0
- `/a0/usr/workdir/memory_fix_backups/_50_recall_memories.py.bak` — source of the embedded `search_memories` method
