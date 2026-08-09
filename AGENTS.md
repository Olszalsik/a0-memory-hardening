# memory_hardening

> Wraps the built-in `_memory` plugin with resilience, observability, and rate-limiting. Adds circuit breakers per memory subdir, embedding-swap on failure, exponential backoff with adaptive interval, and per-process watchdog tasks.

**Version:** 0.3.1 · **Plugin ID:** `memory_hardening`

## Purpose

Wraps the built-in `_memory` plugin with resilience, observability, and rate-limiting. Adds circuit breakers per memory subdir, embedding-swap on failure, exponential backoff with adaptive interval, and per-process watchdog tasks.

## Ownership / Layout

- `helpers/` — circuit breakers, rate limiter, adaptive interval, embedding swap, watchdog registry, memorize-cancellation
- `hooks.py` — install() / pre_update() / uninstall() lifecycle; uninstall cancels every tracked watchdog task and resets process-global registries

## Local Contracts

- `hooks.py` defines `install`, `pre_update`, `uninstall` (the v2.5 framework hook names). Earlier versions used `initialize` / `shutdown` — the framework silently no-ops missing hooks, so the old names caused a real state leak: watchdog tasks were never cancelled, embedding swaps never reset, etc.
- Each helper module exposes a `reset()` function so uninstall can clear its process-global state.

## v2.5 Status

- Hooks renamed `initialize`→`install` and `shutdown`→`uninstall` in v0.3.0 to match the v2.5 contract. Sync hooks are sufficient (all work is short and I/O-bound).

## Verification

Disable the plugin, confirm in the framework log that `uninstall()` ran and cancelled watchdog tasks. Re-enable, confirm `install()` ran and the registry is empty.

## See also

- `plugin.yaml` — manifest (name, version, settings_sections, per_project_config, per_agent_config)
- `default_config.yaml` — defaults (referenced by `install()` and the WebUI settings UI)
- `README.md` — user-facing docs (what the plugin does from a user's perspective)
- Framework references: `helpers/plugins.py` (lifecycle), `helpers/api.py` (API dispatch), `helpers/ui_server.py` (asset serving)
