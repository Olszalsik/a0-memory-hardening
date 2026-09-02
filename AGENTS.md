# memory_hardening

> Wraps the built-in `_memory` plugin with resilience, observability, and rate-limiting. Adds circuit breakers per memory subdir, embedding-swap on failure, per-process watchdog tasks, and a memory-history clamp.

**Version:** 0.6.0 · **Plugin ID:** `memory_hardening`

## Purpose

Wraps the built-in `_memory` plugin with resilience, observability, and rate-limiting. Adds circuit breakers per memory subdir, embedding-swap on failure, per-process watchdog tasks, and a memory-history clamp (merged from the former `_memory_resilience` plugin in v0.5.0).

## Ownership / Layout

- `helpers/` — circuit breakers, rate limiter, embedding swap, watchdog registry, memorize-cancellation, recall method patch, recall-wait TimeoutError guard, history clamp
- `hooks.py` — install() / pre_update() / uninstall() lifecycle; uninstall cancels every tracked watchdog task, un-guards `RecallWait.execute`, and resets process-global registries (including `history_clamp.STATE`)

## Local Contracts

- `hooks.py` defines `install`, `pre_update`, `uninstall` (the v2.5 framework hook names). Earlier versions used `initialize` / `shutdown` — the framework silently no-ops missing hooks, so the old names caused a real state leak: watchdog tasks were never cancelled, embedding swaps never reset, etc.
- Each helper module exposes a `reset()` function so uninstall can clear its process-global state.

## v0.5.0 history_clamp contract (merged from `_memory_resilience`)

- `helpers/history_clamp.py` owns the clamp logic + a process-global `STATE` telemetry dict (`get_state()`, `reset()`).
- `extensions/python/util_model_call_before/_10_clamp_memory_history.py` is a thin `Extension` that reads `memory_hardening` config and calls `history_clamp.clamp(call_data, agent, own_override=..., inject_notice=...)`.
- The hook point `util_model_call_before` is also used by the `_model_fallback` cascade at priorities `_00` / `_01`. Our hook runs LAST (`_10`), so the cascade's own `call_data` mutations (force chat-completions, strip a0-only kwargs) are already applied when we clamp — the desired order (clamp the final message after the cascade has chosen the API).
- Budget resolution: `history_clamp_max_chars_override` → `_model_fallback.memory_memorize_max_chars` → `50000`. The shared `_model_fallback` key is intentional: one setting governs both the cascade timeout budget and the memory budget, because both protect the same utility-model context window.
- Prompt detection is a substring match on the official memory system-prompt file names (`memory.memories_sum`, `memory.solutions_sum`, `memory.fragments_sum`, `memory.solutions.sys`). If upstream renames them, the clamp silently becomes a no-op — safe degradation.
- Failure semantics: `clamp()` never raises. Any exception is caught, recorded in `STATE["errors"]`, and `call_data` is left unchanged so the utility call still runs.
- Master switch: the extension respects both `hardening_enabled` (plugin master) and `history_clamp_enabled` (feature toggle).

## v2.5 Status

- Hooks renamed `initialize`→`install` and `shutdown`→`uninstall` in v0.3.0 to match the v2.5 contract. Sync hooks are sufficient (all work is short and I/O-bound).

## Verification

Disable the plugin, confirm in the framework log that `uninstall()` ran and cancelled watchdog tasks. Re-enable, confirm `install()` ran and the registry is empty. Query `GET /api/plugins/memory_hardening/stats.history_clamp` to confirm the clamp counters reset to zero across the enable/disable cycle.

## DOX changelog

### v0.5.1 (2026-08-20) — settings did not save

Symptom: ticking boxes in the plugin settings screen and hitting Save did
nothing — on reopen every toggle reverted.

Root cause: `webui/config.html` ran its own Alpine component (`alpineState()`)
whose custom Save button POSTed `action: "config_set"` to `/api/plugins`. The
v2.9 framework (`api/plugins.py`) only handles `get_config` / `save_config` —
there is no `config_set` action, so Save got `400 Unknown action` and wrote
nothing. The official footer Save (which does work, via `save_config`) bound
to `pluginSettingsPrototype.settings`, an object the checkboxes never touched
(they wrote to the plugin's separate `cfg`), so it couldn't save the user's
ticks either. Same toggle-save regression class as `_model_fallback` v2.6.5.

Fix: migrated `config.html` to the official `pluginSettingsPrototype` store
(same pattern as `_chat_naining` / `_model_fallback`):

- Checkboxes bind `x-model="config.<key>"`, where `config` is the parent modal
  scope getter for `pluginSettingsPrototype.settings` — the same object the
  footer Save serializes via `save_config` → `config.json`. Toggles persist.
- `config` is referenced directly (not captured) so **Reset to default**
  (which replaces the settings object) is reflected live.
- `mhBackfill(config)` seeds recommended defaults for keys ABSENT in the
  loaded config (core features ON, "Advanced Features" set OFF); a present
  `null` (e.g. `history_clamp_max_chars_override`) is left untouched.
- Removed the broken custom Save; kept the read-only `/stats` live panel +
  Refresh status + Reset Breaker; folded the orphaned Recall-Method-Patch card
  into the Alpine bindings (`x-model="config.recall_patch_enabled"`).
- Note on the x-component loader (`webui/js/components.js`): non-module inline
  `<script>` runs synchronously before body nodes are appended, so
  `window.mhBackfill/mhActiveCount/mhStats` are defined before Alpine
  evaluates `x-data="mhStats()"`.

Also aligned `api/stats.py` `/stats` `config` payload Phase-3 defaults
(`per_subdir_breaker_enabled`, `adaptive_interval_enabled`,
`memorize_hard_cancel_enabled`) `False`→`True` to match `default_config.yaml`
(latent inconsistency; cosmetic after the UI migration since the toggles no
longer read that payload).

Defaults: `default_config.yaml` already encodes "core recommended ON, advanced
extras OFF" (Phase 1–4 ON; quarantine / embedding-swap / agent-notice OFF). The
bug was that a *change* could never be kept. After this fix, **Reset to
default** loads that baseline and **Save** holds it. A live `config.json`
carrying the old stale state (Phase 3 off) is preserved as-is; to get the full
recommended baseline, click **Reset to default** → **Save** once.

Version 0.5.0 → 0.5.1 across `plugin.yaml`, `config.html` title, this file,
`README.md`, and the `t_manifest` version assertion in
`tests/test_extensions.py`. `t_webui` (marker-presence check) still passes.

Suite: host run is partial — the test files hardcode `/a0/...` container
paths and only run inside the container (host is Windows at `E:\...`).
`test_history_clamp` is 28/29 on host (the 1 fail is a `/a0` path-resolution
issue, not a regression). Static checks pass: markers preserved, broken
`config_set`/`alpineState` code gone, JS parses (`node --check`), version
consistent at 0.5.1.

Commit `eee9492` → Olszalsik/a0-memory-hardening main (7 files:
`webui/config.html`, `api/stats.py`, `plugin.yaml`, `CHANGELOG.md`,
`README.md`, `AGENTS.md`, `tests/test_extensions.py`). `config.json` is
gitignored (sacred, not committed).

### v0.5.1 follow-up (2026-08-20) — Refresh status / Reset Breaker 403 CSRF

Symptom (reported live after enabling every toggle for testing): clicking
**Refresh status** or **Reset Breaker** in the settings screen popped an error.
Both buttons posted to the plugin's own `/api/plugins/memory_hardening/stats`
and `/reset_breaker` endpoints with a raw `fetch()` — no `X-CSRF-Token` header.
Those handlers inherit `ApiHandler.requires_csrf() = cls.requires_auth() = True`
(`helpers/api.py`), and the dispatch wraps them in `csrf_protect`, so a headerless
POST is rejected with `403 "CSRF token missing or invalid"` — before the handler
body ever runs. This was a latent bug inherited from the pre-v0.5.1 `config.html`
(which also used raw `fetch`); it only surfaced once someone actually clicked the
buttons.

Fix: converted the bottom `<script>` to `<script type="module">` and switched
both calls to the framework's `fetchApi` (from `/js/api.js`), which attaches the
`X-CSRF-Token` (fetched from `/api/csrf_token`) and auto-retries once on token
expiry. Module-import ordering in the x-component loader
(`webui/js/components.js`) is safe: it `await`s all module imports before
appending the deferred body nodes, so `window.mhStats / mhBackfill / mhActiveCount`
are still defined before Alpine evaluates `x-data="mhStats()"`. Verified
statically: 0 raw `fetch(` calls remain, `fetchApi` used for both endpoints,
markers preserved, `node --check` parses the module. Version stays 0.5.1 (this is
a correction to the just-shipped settings release, not a new feature).

## v0.5.2 recall_wait_guard contract

- `helpers/recall_wait_guard.py` owns the guard + a process-global `STATE` telemetry dict (`get_state()`, `reset_state()`).
- `extensions/python/message_loop_prompts_after/_06_recall_wait_guard.py` is a thin `Extension` (priority 6, after `_05_recall_method_patch` at 5, before `_50_recall_memories` at 50 and the built-in `_91_recall_wait` at 91) that reads `recall_wait_guard_enabled` from config and calls `apply_recall_wait_guard(enabled=...)` every loop iteration — idempotent, zero-cost after the first wrap.
- **Wraps, does not replace:** `apply_recall_wait_guard` stores the original `RecallWait.execute` on the class (`_mh_original_execute`) and substitutes a `safe_execute` wrapper that calls the original inside `try/except`. This keeps the guard resilient to upstream changes to the `RecallWait.execute` body — unlike `recall_patch` (which only acts when the method is *missing*), this always wraps because the method is present but unguarded.
- **Exception policy:** `asyncio.CancelledError` is re-raised (never swallow loop shutdown). `asyncio.TimeoutError` (the 30s `asyncio.wait_for` budget from `_50_recall_memories`) is caught + logged — memory recall is best-effort and must not crash the monologue loop. A generic `Exception` safety net catches any other recall-path failure (FAISS error, config glitch) with a loud warning log so real bugs stay visible.
- **Interaction with telemetry/breaker:** the downstream `_95_recall_telemetry` observer (priority 95) still inspects `task.exception()` and sees the `TimeoutError`, classifying the outcome as `timeout` and feeding the circuit breaker. This guard only keeps the loop alive long enough for that to happen; it does not suppress the breaker learning.
- **Toggle:** `recall_wait_guard_enabled: false` restores the original `execute` (status `disabled_restored`); the extension re-applies on the next iteration if set back to true.
- Master switch: the extension also respects `hardening_enabled` indirectly (it returns early if `self.agent` is None); the feature toggle is `recall_wait_guard_enabled`.

## DOX changelog

### v0.5.2 (2026-08-26) — recall-wait TimeoutError guard

Symptom: the built-in `_memory` plugin's `_91_recall_wait.py` does `await task`
with no `try/except`, where `task = asyncio.wait_for(search_memories(...),
timeout=SEARCH_TIMEOUT=30)`. When the 30s budget fires, `asyncio.TimeoutError`
propagates out of `RecallWait.execute` → `prepare_prompt` → `monologue` and
kills the whole agent loop. Historical logs in this install show ~1268 such
crashes. `memory_hardening` already runtime-patches `search_memories` (the
v1.2.0 regression, `recall_patch`) but did not guard the outer `await task` —
that gap remained.

Fix: added `helpers/recall_wait_guard.py` + the
`_06_recall_wait_guard.py` extension (priority 6) that wraps
`RecallWait.execute` so the `TimeoutError` is caught and logged instead of
crashing the loop. Wraps (not replaces) the method → resilient to upstream
body changes. `asyncio.CancelledError` is re-raised; `asyncio.TimeoutError`
and a generic `Exception` safety net are caught with a loud warning. The
downstream `_95_recall_telemetry` still records the timeout outcome and
feeds the breaker, so backoff still happens.

Wiring: `api/stats.py` exposes `recall_wait_guard` state + a
`recall_wait_guard_enabled` config key; `default_config.yaml` adds the toggle
(recommended: ON); `webui/config.html` adds a toggle card with live stats;
`hooks.py` uninstall restores the original `execute` + resets state;
`tests/test_extensions.py` bumps the extension count 16→17 and the version
assertion 0.5.1→0.5.2. New dedicated `tests/test_recall_wait_guard.py` (7/7)
covers apply, timeout-caught, cancelled-reraised, generic-exception-caught,
idempotent, disable-restores, state-shape. Integration smoke-tested against
the real `_memory.RecallWait` class (apply/idempotent/restore all green).

Version 0.5.1 → 0.5.2 across `plugin.yaml`, `AGENTS.md`, `CHANGELOG.md`,
`README.md`, `default_config.yaml`, `webui/config.html`, `api/stats.py`,
`hooks.py`, `tests/test_extensions.py`. `config.json` is gitignored (sacred).


- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults (referenced by `install()` and the WebUI settings UI)
- `README.md` — user-facing docs (what the plugin does from a user's perspective)
- `helpers/history_clamp.py` — the merged clamp logic (was `_memory_resilience/extensions/.../​_10_clamp_memory_history.py`)
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch), `helpers/ui_server.py` (asset serving)

## v0.5.3 — recall guard must bind the FRAMEWORK class (2026-09-01)

**Live failure found 2026-09-01:** the v0.5.2 recall-wait guard never fired
in production. 2026-08-27 crash tracebacks (`_91_recall_wait.py line 30,
await task`) show NO `safe_execute` frame: the dispatcher kept calling the
unwrapped class. Telemetry said "applied"; tests passed; the wrap landed on
a ghost.

Root cause: the framework loads extension files via
`helpers.modules.import_module` — a SYNTHETIC module named after the file
basename (e.g. `"_91_recall_wait"`), never registered in `sys.modules`.
`apply_recall_wait_guard` imported `RecallWait` via the canonical dotted
path, creating a SECOND module + class object that
`call_extensions_async` (`cls(agent=agent).execute(...)`) never touches.
Canonical-import patches on extension-point classes are structurally
silent no-ops in this framework.

Fix (v0.5.3):
- NEW `helpers/extension_class.py` — `resolve_extension_class()` resolves
  the class through `helpers.extension._get_extension_classes` (the exact
  cached list the dispatcher iterates), matched by class name +
  `__module__.endswith(basename)`; canonical import only as a bare-test
  fallback. Never patch an extension-point class via a dotted-path import.
- `recall_wait_guard.apply_recall_wait_guard(enabled, agent=None)` +
  `recall_patch.apply_recall_patch(enabled, agent=None)` use it; both
  extensions pass `self.agent`. New telemetry key `last_class_module`
  (verify via `/stats`: must be the synthetic `"_91_recall_wait"`, not the
  dotted path).
- `tests/test_recall_wait_guard.py` (8/8): fakes now set
  `__module__ = "_91_recall_wait"` and the dispatcher class list is
  monkeypatched; new `dispatcher_class_wins` regression test asserts the
  framework class is wrapped, NOT the canonical phantom.

Related: the Jul-2026 disk-edit hardening of core
`plugins/_memory/.../_91_recall_wait.py` was reverted by the v2.9/v2.10/
v2.11 upstream merges — this plugin-level guard is the merge-proof
replacement, which is why binding it correctly matters. Upstream v2.11
additionally handles the 30s `TimeoutError` inside the recall task itself
(`search_and_cache` catches it), so the guard is now defense-in-depth for
`CancelledError` paths and future upstream regressions.

The same defect in `_model_fallback`'s memory recall patches was fixed in
parallel (v2.8.2).

## v0.6.0 — dormant-code cleanup (2026-09-02)

Third-pass audit cleanup; every change removes something that never
worked or was silently ignored:

- **REMOVED the Adaptive Recall Interval feature** (`helpers/adaptive_interval.py`,
  `job_loop/_60_adaptive_interval.py`, all `adaptive_interval_*` config
  keys, the WebUI card, the `/stats` section, the `hooks.py` uninstall
  reset). It was structurally dead since birth: `record_latency_ms()` had
  NO production caller (only the test fed it, so p99 was always None and
  `adjust()` always returned the current interval), and its persist path
  called `helpers.plugins.set_plugin_config(...)` which does not exist in
  the framework (only `save_plugin_config` does) — the AttributeError was
  swallowed into a debug log. If dynamic recall intervals are ever wanted,
  they need a real latency source + `save_plugin_config` first.
- **`coroutine_guard_enabled` is now saveable from the WebUI** — the
  toggle existed in `default_config.yaml` and was read by the job_loop
  sweep, but the settings UI never rendered it and it was missing from
  the save-field list, so users could not turn it off. Added the card +
  added it to `mhActiveCount`'s feature list (14 features, same count —
  replaces the removed adaptive-interval entry).
- **`faiss_health` scan is now recursive + bounded** — knowledge subdirs
  nest under `usr/memory/knowledge/<name>/`, and the old single-level
  `os.listdir` silently missed every nested index. Bounded by
  `MAX_INDEXES=200` / `MAX_DIRS=500` (stat-storm caution on the 9p
  bindmount) with a CUMULATIVE directory counter (the first cut compared
  per-level counts and never fired on a normal knowledge tree).
  Staleness flagging now uses the configured
  `faiss_health_max_age_days` (probe_one had a hardcoded 365 that
  contradicted probe_all's 90-day default).
- **`quarantine.scan()` walks nested subdirs too** (same one-level miss;
  `candidates[].subdir` is now the path relative to `usr/memory`), and
  `snapshot()` returns the last scan (it previously read
  `scan.__dict__.get("_last")`, a field that was never set, so `/stats`
  always showed `last_scan: null`). The snapshot is now also exposed on
  the `/stats` endpoint as the `quarantine` field.
- **`auto_recover_quarantine_dir` is now honoured** — the key was
  documented in `default_config.yaml` but `auto_recover._quarantine_dir()`
  hardcoded `tmp/memory/quarantine`. The config value flows through
  `attempt_recovery(..., quarantine_dir=...)` → `_move_to_quarantine`.
- **Removed the unused `dashboard_refresh_sec` / `dashboard_max_points`
  keys** — no code ever read them.

### Portability note (import style)

All plugin modules import each other via the absolute
`usr.plugins.memory_hardening.*` namespace (and
`memory_hardening.helpers.*` in tests). This requires the plugin to be
installed under a standard plugins root as `usr/plugins/memory_hardening/`
— which is how Agent Zero loads every plugin, both in the container and
in Hub installs. The plugin will NOT import if its folder is placed
somewhere else and put on `sys.path` directly; do not "flatten" the
package when installing.
