# memory_hardening — User Manual

**Version:** 0.4.0 · **Plugin ID:** `memory_hardening` · **Updated:** 2026-07-27

This manual tells you, step by step, how to use the `memory_hardening` plugin. It covers installation, settings, the WebUI dashboard, the stats API, common recipes, and troubleshooting. If you only have time for one section, read **Quick start**.

---

## 1. Quick start

The plugin ships already installed and enabled at recommended defaults. You do not need to do anything.

To verify it is active:

1. Open the Agent Zero WebUI.
2. Click **Settings** in the top-right gear menu.
3. Click the **Agent** tab.
4. Look for the **Memory Hardening** section header.
5. Confirm the green "Plugin enabled" pill is showing.
6. Confirm the **Recall Method Patch** card reads `Last status: applied`.

If all three are true, the plugin is working. No further action needed.

---

## 2. What this plugin protects you from

| Failure mode | What goes wrong | How memory_hardening fixes it |
|---|---|---|
| **FAISS recall timeout** | The message loop blocks for 60+ seconds on a slow FAISS query, freezing the agent and the WebSocket. | **Watchdog** marks the stuck task; **Circuit Breaker** opens after 3 failures in 5 minutes and short-circuits subsequent recalls until cooldown. |
| **Silent memory recall failure (v0.4.0)** | After the upstream `_memory` v1.2.0 update, recall throws `AttributeError: 'RecallMemories' object has no attribute 'search_memories'` and silently fails. | **Recall Method Patch** injects the missing method at runtime via `setattr` — no source modification. |
| **Leaked asyncio coroutines** | During multi-hour provider outages, cancelled `wait_for` tasks leak openai/httpx coroutines, freezing the WebSocket dispatcher. | **Coroutine Guard** sweeps leaked coroutines every 5s; **UI-loop pulse** keeps the event loop responsive. |
| **Stuck `memorize` background threads** | The deferred memorize threads never report back, blocking shutdown. | **Memorize Watchdog** warns when they exceed 30s; **Memorize Hard Cancel** cooperatively cancels them. |
| **Unbounded `Memory.index` cache** | The process-global `Memory.index[memory_subdir]` dict grows forever across agents and projects. | **Index Cache GC** evicts idle entries after 1 hour or 1000 entries. |
| **Thundering retry pattern** | After a slow FAISS query, every agent hits FAISS again immediately. | **Rate Limiter** caps FAISS calls per memory subdir. |
| **Noisy multi-project interference** | One project's FAISS slowness blocks every other project. | **Per-Subdir Breaker** gives each memory subdir its own breaker state. |
| **Corrupt FAISS index** | A bad shutdown or disk error leaves FAISS un-loadable. | **Auto-Recovery** quarantines the corrupt file and triggers a rebuild from the embeddings cache + documents. |
| **Stale indexes** | Indexes older than 30 days still get searched, slowing recall. | **Quarantine** auto-archives old indexes; recall ignores them by default. |
| **Embedding model change downtime** | Changing models means recall returns wrong results for the transition window. | **Embedding Hot-Swap** shadow-validates the new model and switches on consensus. |

---

## 3. Installation

The plugin is installed in `/a0/usr/plugins/memory_hardening/`. To control whether it is enabled:

```
# enable
touch /a0/usr/plugins/memory_hardening/.toggle-1

# disable
touch /a0/usr/plugins/memory_hardening/.toggle-0
```

If neither file exists, the plugin is enabled by default.

To verify installation:

```
ls -la /a0/usr/plugins/memory_hardening/
```

You should see:

```
AGENTS.md            75 lines
README.md           217 lines
USER_MANUAL.md      this file
CHANGELOG.md        103 lines
plugin.yaml
default_config.yaml
hooks.py
helpers/             # 14 modules
api/                 # stats + reset_breaker
extensions/          # 15 extension hooks
webui/               # config.html dashboard
tests/               # test_helpers.py + test_extensions.py
```

---

## 4. WebUI dashboard

Open **Settings -> Agent -> Memory Hardening**. You will see a stack of cards, one per feature.

### 4.1 Top header

- **Plugin status** pill (green = ON, red = OFF)
- **Features active: N / 13** counter
- **Refresh** button (instant update)
- **Master kill switch** checkbox (turns off every feature at once)

### 4.2 Cards

Each card has:
- Feature name and one-line description
- **Enabled** checkbox (toggle that feature)
- **Advanced** collapsible section (numeric thresholds, for tuning)
- Live telemetry fields (auto-refresh every 5 seconds)

Card list:

| Card | What it shows |
|---|---|
| **Watchdog** | Active tasks, reaped count, cancelled count |
| **Circuit Breaker** | State (closed/open/half_open), failure window, threshold, cooldown |
| **Per-Subdir Breaker** | States per memory subdir (one row per subdir) |
| **Telemetry** | Success/fail counters, latency p50/p95/p99 |
| **Health Probe** | FAISS index health, stuck task reports |
| **Rate Limiter** | Allowed/throttled per subdir |
| **Quarantine** | Archived indexes |
| **Embedding Hot-Swap** | Active swap, history |
| **Coroutine Guard** | Tick count, last tick age |
| **Memorize Watchdog** | Stuck memorize threads |
| **Memorize Canceller** | Cancelled + stuck-detected counts |
| **Index Cache GC** | Current size, evicted total |
| **Auto-Recovery** | Rebuild history |
| **Recall Method Patch (v0.4.0)** | Applied/already-present counts, last status |

### 4.3 Typical workflows

**"The agent is slow / stuck."**

1. Open **Settings -> Agent -> Memory Hardening**.
2. Look at the **Circuit Breaker** card. If state is `open`, the breaker has tripped.
3. Look at the **Watchdog** card. Active task count tells you how many are stuck.
4. Check the **Recall Method Patch** card. If `Last status: applied`, the v0.4.0 fix is active.
5. If breaker is open: click **Reset Breaker** in the top header.
6. Wait 60 seconds for the cooldown to elapse, or click **Reset Breaker** to force-close immediately.

**"Memory recall isn't working."**

1. Open **Settings -> Agent -> Memory Hardening**.
2. Look at the **Recall Method Patch** card.
3. If `Last status: import_error`, check the **Last error** field for the import failure.
4. If `Last status: class_not_found`, the `_memory` plugin has been renamed — open an issue.
5. If `Last status: applied` and `Applied: 0`, the method is already present (likely from a newer `_memory` version) and no patch was needed.
6. If `Last status: applied` and `Applied: 1`, the patch is working — recall should function. If it still fails, check the **Auto-Recovery** card for quarantined FAISS files.

**"FAISS is failing to load."**

1. Look at the **Auto-Recovery** card.
2. If `Rebuild attempts > 0`, a FAISS corruption was detected.
3. The quarantined file path appears in the card.
4. The plugin automatically triggers a rebuild from the embeddings cache.
5. If rebuild succeeds, the card shows `Rebuild success: true`.
6. If rebuild fails, the **Last error** field has the failure reason.

---

## 5. Stats API

The plugin exposes one read endpoint and one write endpoint.

### 5.1 `GET /api/plugins/memory_hardening/stats`

Returns the full telemetry snapshot. Use this for monitoring, alerting, or building your own dashboard.

**Example using curl:**

```
curl http://localhost:50001/api/plugins/memory_hardening/stats | jq .
```

**Top-level fields (v0.4.0):**

```json
{
  "telemetry": { "counters": {}, "latency": {} },
  "breaker": { "state": "closed|open|half_open" },
  "watchdogs": {},
  "memorize_watchdogs": {},
  "index_gc": {},
  "faiss_health": {},
  "auto_recover": {},
  "rate_limiter": {},
  "per_subdir_breaker": {},
  "memorize_canceller": {},
  "embedding_swap": {},
  "coroutine_guard": {},
  "recall_patch": {},
  "config": { ...all 52 keys... }
}
```

**Filtering specific fields with jq:**

```
# just the recall_patch status
curl .../stats | jq .recall_patch

# just the breaker state
curl .../stats | jq .breaker.state

# list of per-subdir breaker states
curl .../stats | jq '.per_subdir_breaker | keys[]'

# p99 recall latency
curl .../stats | jq .telemetry.latency.p99_ms
```

### 5.2 `POST /api/plugins/memory_hardening/reset_breaker`

Force-resets the global circuit breaker from `open` back to `closed`. Use this when:
- The breaker has tripped and you want to short-circuit the cooldown.
- You have manually fixed the underlying FAISS issue and want recall to resume immediately.

**Example:**

```
curl -X POST http://localhost:50001/api/plugins/memory_hardening/reset_breaker
```

Returns: `{"status": "ok", "breaker_state": "closed"}`

This only resets the **global** breaker. To reset per-subdir breakers, toggle the **Per-Subdir Breaker** card off and on, or restart the agent.

---

## 6. Common recipes

### 6.1 "I want to temporarily disable everything while I debug."

```
touch /a0/usr/plugins/memory_hardening/.toggle-0
```

This stops the plugin from loading on next agent start. The framework log will show:

```
memory_hardening: plugin disabled
```

To re-enable:

```
rm /a0/usr/plugins/memory_hardening/.toggle-0
touch /a0/usr/plugins/memory_hardening/.toggle-1
```

### 6.2 "I want the breaker to be more aggressive (fewer failures before opening)."

1. Open **Settings -> Agent -> Memory Hardening**.
2. In the **Circuit Breaker** card, click **Advanced**.
3. Change `failure_threshold` from `3` to `2`.
4. Optionally lower `cooldown_sec` from `60` to `30`.
5. The new values take effect on the next agent restart.

### 6.3 "I want to skip the v0.4.0 recall patch because I'm using a fixed version of _memory."

1. Open **Settings -> Agent -> Memory Hardening**.
2. In the **Recall Method Patch** card, uncheck **Enabled**.
3. The patch becomes a no-op on the next message loop.
4. The card will still show telemetry (so you can verify the method is now present).

### 6.4 "I want to add my own alerting on breaker open."

Use the stats API in a cron-style check:

```bash
#!/bin/bash
STATE=$(curl -s http://localhost:50001/api/plugins/memory_hardening/stats | jq -r .breaker.state)
if [ "$STATE" = "open" ]; then
  echo "ALERT: memory_hardening breaker is OPEN"
  # add your Slack/Discord/PagerDuty hook here
fi
```

Run every 60 seconds from cron or systemd.

### 6.5 "I want to export all stats to Prometheus / Grafana."

The stats endpoint returns JSON. Use a JSON-to-Prometheus exporter like `jql_exporter`, or write a small script that reads the JSON every 15s and writes to a textfile collector for `node_exporter`.

A Phase 4 enhancement to add a native `/metrics` Prometheus endpoint is planned but not yet shipped. Subscribe to `CHANGELOG.md` for the announcement.

### 6.6 "I want to change the FAISS recall interval."

This is controlled by the `_memory` plugin's `memory_recall_interval` setting (default `3`):

1. Open **Settings -> Agent -> Memory**.
2. Find `memory_recall_interval`.
3. Set to your preferred value (1-15, default 3).
4. Save.

The plugin does not modify `memory_recall_interval`; whatever you set in **Settings -> Agent -> Memory** is what recall uses.

---

## 7. Testing

Run from the plugin directory:

```
cd /a0/usr/plugins/memory_hardening
/opt/venv/bin/python tests/test_helpers.py    # 17/17 tests
/opt/venv/bin/python tests/test_extensions.py # 5/5 tests
```

Total: **22/22 tests pass.**

If a test fails:
1. Read the failure message — it points to the file and assertion.
2. Check that `/a0/usr/workdir/memory_fix_backups/_50_recall_memories.py.bak` exists (required for `recall_patch` tests).
3. Check that all 14 helper modules are present in `helpers/`.
4. Run with `-v` flag for verbose output: `/opt/venv/bin/python tests/test_helpers.py` already includes verbose per-test output.

---

## 8. Troubleshooting

### 8.1 Plugin does not appear in Settings

**Symptom:** No **Memory Hardening** section in Settings -> Agent.

**Likely causes:**
- `plugin.yaml` is missing or malformed.
- The `.toggle-0` file exists and the framework skipped the plugin.
- The plugin directory does not have a `plugin.yaml` at its root.

**Fix:**

```
ls /a0/usr/plugins/memory_hardening/plugin.yaml
ls /a0/usr/plugins/memory_hardening/.toggle-*
```

If the toggle file shows `.toggle-0`, delete it and restart the agent.

### 8.2 Recall Method Patch shows `Last status: import_error`

**Symptom:** The card shows `import_errors > 0` and `Last error` has a Python traceback.

**Likely causes:**
- The `_memory` plugin has been renamed or moved.
- The class name `RecallMemories` has changed.
- A syntax error in `_memory` v1.2.0 prevents it from importing.

**Fix:**

1. Read the `Last error` field. The traceback will name the missing module.
2. If the path is `plugins._memory.extensions.python.message_loop_prompts_after._50_recall_memories`, the import target is correct.
3. If the error mentions a different path, your `_memory` plugin has been reorganized.
4. Open an issue with the traceback.

### 8.3 Recall Method Patch shows `Last status: already_present` but recall still fails

**Symptom:** `applied: 0`, `already_present: N>0`, but memory recall still raises `AttributeError`.

**Likely causes:**
- The `_memory` plugin defines `search_memories` but it raises its own `AttributeError` internally.
- The `search_memories` method exists but is broken.

**Fix:**

1. Disable the recall patch: **Settings -> Agent -> Memory Hardening -> Recall Method Patch -> Enabled**.
2. Check if `_memory` has a known issue with the current Agent Zero version.
3. Report the underlying error to the `_memory` plugin author.

### 8.4 Circuit breaker is always `open`

**Symptom:** `breaker.state: open` for hours, even after `Reset Breaker`.

**Likely causes:**
- FAISS is genuinely failing every recall (corrupt index, embedding model mismatch).
- The cooldown is too short for the underlying issue to resolve.

**Fix:**

1. Check the **Auto-Recovery** card for quarantined FAISS files.
2. If `quarantined_to` is set, the rebuild attempt is in progress.
3. If `Rebuild success: false`, check `Last error`.
4. If FAISS keeps failing, the underlying issue is not transient — debug FAISS itself.

### 8.5 WebUI dashboard does not refresh

**Symptom:** Numbers on the cards do not update.

**Likely causes:**
- JavaScript console error.
- The plugin's `webui/config.html` is not being served by the framework.

**Fix:**

1. Open browser dev tools (F12), check the Console tab for errors.
2. Reload the settings page.
3. If still broken, check that `webui/config.html` exists and is readable.

### 8.6 Tests fail after a clean install

**Symptom:** `test_helpers.py` reports 0/17 or 1/17.

**Likely causes:**
- The `memory_fix_backups/_50_recall_memories.py.bak` file is missing (required for `recall_patch` tests).
- A helper module failed to import.

**Fix:**

```
ls /a0/usr/workdir/memory_fix_backups/_50_recall_memories.py.bak
ls /a0/usr/plugins/memory_hardening/helpers/
```

If the backup is missing, restore it from the original commit.

---

## 9. Compatibility

| Component | Required version |
|---|---|
| Agent Zero framework | v2.5 or later |
| `_memory` plugin | v0.x, v1.0, v1.1, v1.2 (patched by v0.4.0), v1.3+ (patch becomes no-op) |
| Python | 3.10+ |
| OS | Linux (primary), macOS (best-effort), Windows (WSL2) |

The plugin was tested against Agent Zero framework v2.5 and `_memory` plugin v1.2.0.

---

## 10. Privacy and data

The plugin **does not** send any data off-device. All telemetry is stored in process-local Python dicts and exposed via the `/stats` API endpoint on `localhost`.

The plugin **does not** modify any file outside of `/a0/usr/plugins/memory_hardening/`. The only filesystem side-effect is the v0.4.0 recall patch, which uses `setattr` to attach a method to an in-memory Python class — no files are written.

The plugin **does** quarantine corrupt FAISS files. Quarantined files are moved to:

```
/a0/usr/plugins/memory_hardening/quarantine/
```

This is so they can be inspected before deletion. To permanently delete them, run:

```
rm -rf /a0/usr/plugins/memory_hardening/quarantine/
```

---

## 11. Where to get help

1. Check this manual section 8 (Troubleshooting).
2. Check `README.md` for feature summaries.
3. Check `AGENTS.md` for architecture and contracts (developer-facing).
4. Check `CHANGELOG.md` for version history.
5. Run the tests: `python tests/test_helpers.py && python tests/test_extensions.py`.
6. Inspect the stats endpoint: `curl http://localhost:50001/api/plugins/memory_hardening/stats | jq .`.
7. Open an issue in your Agent Zero install log with the output of the above.

---

## 12. License

MIT — see `LICENSE`.
