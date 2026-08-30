# Plan: watcher deadline-aware rescan (+ restore Explorer refresh in the venv)

> Date: 2026-08-11 · Slug: `watcher-deadline-aware-rescan` · Engram topic: `plan/watcher-deadline-aware-rescan`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

After `dev-docs/watcher-startup-drive-readiness.md` landed, the boot sequence works: PC powers on,
Docker starts, Google Drive mounts, `wait_for_roots` / `wait_for_api` gate correctly, and the first
scheduled tick runs against an available root. But **no file gets transcribed** until the user
manually fires `POST /watcher/trigger` (from the host or another WireGuard peer). The manual trigger
always works, which made this look like a trigger/scheduling bug — it isn't.

### Root cause (confirmed from `%LOCALAPPDATA%\whisper-watcher\watcher.log`)

```
2026-08-11 16:25:59 [INFO] Watcher starting. Roots: 1. ... Enabled: True. Scan interval: 3600s ...
2026-08-11 16:25:59 [INFO] Scan starting (source=scheduled, roots=1).
2026-08-11 16:25:59 [WARNING] Explorer refresh unavailable (non-Windows host or pywin32 not installed) ...
2026-08-11 16:25:59 [INFO] Scan finished (source=scheduled): 0 candidate(s) found, 0 submitted, any_unavailable=False.
2026-08-11 16:30:49 [INFO] Scan starting (source=manual, roots=1).
2026-08-11 16:30:50 [INFO] Submitting: G:\...\2026-08-11 daily.m4a
2026-08-11 16:33:13 [INFO] Scan finished (source=manual): 3 candidate(s) found, 3 submitted, any_unavailable=False.
```

The root **was** available at 16:25:59 (`any_unavailable=False`, no "Root unavailable" warning), so the
startup gate is not at fault. The three files were already on Drive and **were discovered** by that
16:25:59 tick — proof: at 16:30:49 they submitted *immediately*. `stability_seconds` is `60`; if
16:30:49 had been their first sighting, `stable_since` would have been stamped at 16:30:49 and they
would have needed another 60s, submitting 0 that tick. They went out instantly, so their
`stable_since` was recorded at 16:25:59.

So the first tick did exactly what it was written to do: discover the files, start the 60-second
stability clock, submit nothing, then run

```python
next_scan_at = time.time() + api_cfg["poll_interval_seconds"]   # +3600s
```

Next automatic scan: 17:25. The manual trigger at 16:30 simply happened to be the *second* tick,
by which time the stability window was long satisfied. **The stability window always eats the first
tick of any newly-seen file, and the reschedule is unconditionally a full hour.**

### Same bug class, second victim

`schedule_retry` (`watcher.py:496`) sets `entry["next_retry_at"] = time.time() + backoff`, where
`backoff` starts at `backoff_base_seconds` (60s) and doubles. But `next_retry_at` is only *checked*
inside a tick, and ticks are hourly. The whole exponential ladder (60 → 120 → 240 → 480) collapses
into "1h, 1h, 1h, 1h". `unavailable_retry_seconds` (60s, added by the previous plan) is currently the
only short-wake path that exists — the fix generalizes exactly that mechanism.

### Why it was invisible

`run_tick`'s log line reads `"%d candidate(s) found"` but the number is `len(all_ready)` — the count
*after* stability and backoff filtering. Files held by the stability window report as `0 candidate(s)
found`, indistinguishable from an empty folder.

### Second, independent bug found while diagnosing

The scheduled task runs `.venv\Scripts\pythonw.exe` (see `scripts/register-watcher-task.ps1`), and
that venv has no pywin32:

```
> D:\...\.venv\Scripts\python.exe -c "import win32com.client"
ModuleNotFoundError: No module named 'win32com'
```

Every process start logs `Explorer refresh unavailable (non-Windows host or pywin32 not installed)`.
The earlier `pip install --user pywin32` landed in user-site, which the venv never sees. **The entire
Explorer/COM staleness fix from `dev-docs/watcher-explorer-refresh-roots.md` has never actually run in
production** — it has been silently degraded to the plain-`os.walk` path since it shipped. Today's
files happened to be visible anyway; a genuinely stale Drive listing would still be missed.

## Goal & outcome

The user powers the PC off, drops a new recording into the watched Drive folder from another device,
powers on, logs in, and touches nothing — the transcript appears next to the audio within a few
minutes of logon, with no manual `POST /watcher/trigger`. Additionally, the Explorer/COM refresh is
actually active in the production venv, and a future silent degrade is loud in the log.

## Scope

- **In scope**:
  - Deadline-aware next-wake computation in `watcher.py` — the tick reschedules at the earliest real
    deadline (stability, backoff retry, root-unavailable), capped by `poll_interval_seconds`.
  - Honest tick logging: report discovered / ready / held counts so a held file is visible in
    `watcher.log`.
  - Install `pywin32` into the project `.venv`; make `scripts/register-watcher-task.ps1` verify it and
    fail loudly if absent; make the runtime warning name the interpreter so the degrade is obvious.
  - Doc updates in `CLAUDE.md`.
- **Out of scope / non-goals**:
  - No new dependency in `requirements-watcher.txt` beyond the already-declared `pywin32` (it is
    already listed with a Windows marker — the bug is that it was never installed into the venv).
  - No event-driven / filesystem-notification watcher (`ReadDirectoryChangesW`, `watchdog`). The
    polling design stays; only the wake *timing* changes.
  - No change to `stability_seconds` semantics (still first-sighting-based, not mtime-based — a file
    still uploading must not be transcribed truncated).
  - No change to `api.py`, `POST /watcher/trigger`, the control-dir bridge, or the startup readiness
    gate (`wait_for_roots` / `wait_for_api`) — all confirmed working.
  - No change to `max_files_per_cycle` / `max_inflight` throttling behavior.

## Approach

### Part A — deadline-aware rescan (the actual fix)

1. **`process_root` reports held deadlines.** It currently returns `(ready, available)`. Change it to
   return `(ready, available, next_deadline)` where `next_deadline` is the earliest absolute
   timestamp at which *this root* has work that is currently blocked, or `None`:
   - for a candidate held by the stability window: `stable_since + stability_seconds`
   - for a candidate held by backoff: its `next_retry_at`
   - entries already `submitted` (in-flight) or terminally `failed` (`attempts >= max_attempts`)
     contribute nothing.

   Note the two `continue` branches in the current loop (`watcher.py:464-468`) skip terminal-failed
   and backed-off entries *before* the stability bookkeeping — the backoff branch must now record its
   `next_retry_at` into the deadline accumulator before it `continue`s, otherwise the backoff ladder
   stays collapsed.

2. **`run_tick` aggregates and returns a wake hint.** It currently returns `any_unavailable` (a bool).
   Change it to return a small result — either a `(any_unavailable, next_deadline)` tuple or a tiny
   dict; pick one and keep it consistent. `next_deadline` is the `min` of every non-`None` root
   deadline. Files submitted during this tick are done and contribute nothing.

3. **`main()` computes the next wake as a min, not a constant.** Replace the current
   if/else on `any_unavailable` with:

   ```python
   interval = api_cfg["poll_interval_seconds"]
   if any_unavailable:
       interval = min(interval, api_cfg["unavailable_retry_seconds"])
   if next_deadline is not None:
       interval = min(interval, max(next_deadline - time.time(), 0) + WAKE_MARGIN_SECONDS)
   interval = max(interval, api_cfg["control_check_seconds"])   # floor — never busy-loop
   next_scan_at = time.time() + interval
   ```

   - `WAKE_MARGIN_SECONDS`: a small module constant (2–5s) so the tick lands *after* the deadline, not
     exactly on it (avoids waking one loop iteration too early and re-holding the file).
   - The `max(..., control_check_seconds)` floor is mandatory: a `next_retry_at` already in the past
     (e.g. state carried over from a previous run) would otherwise yield `interval = 0` and spin the
     loop at full speed against the API.
   - Log the chosen interval and *why*, e.g.
     `"Next scan in %ds (reason=%s)."` with reason ∈ `stability` / `retry` / `unavailable` /
     `poll_interval`. This is the line that proves the fix in `watcher.log`.

4. **Keep `api.enabled: false` honored.** The wake computation must not resurrect automatic scanning
   when `enabled` is false — the `due = api_cfg["enabled"] and now >= next_scan_at` guard already
   covers this, so just don't touch it. Manual triggers keep working unchanged.

5. **Fix the misleading tick log.** `run_tick`'s finish line must distinguish the three numbers:

   ```
   Scan finished (source=%s): %d discovered, %d ready, %d submitted, %d held (stability/backoff), any_unavailable=%s.
   ```

   `discovered` = total candidates seen across roots before stability/backoff filtering (have
   `process_root` return or accumulate this count). Without it, this whole bug is invisible again next
   time.

6. **`--once` semantics unchanged**: it runs one tick and exits; it must not start waiting for a
   computed deadline. `--dry-run` unchanged.

### Part B — restore the Explorer refresh in production

7. **Install pywin32 into the venv** used by the scheduled task:
   `.venv\Scripts\python.exe -m pip install pywin32`. Verify with
   `.venv\Scripts\python.exe -c "import win32com.client; print('ok')"`.

8. **Make `scripts/register-watcher-task.ps1` verify it.** After the existing `Test-Path $VenvPythonw`
   check, run the interpreter's `python.exe` sibling with `-c "import win32com.client"` and, if it
   fails, `Write-Error` with the exact remediation command and `exit 1` — same shape as the existing
   pythonw/watcher.py guards. Registering a task whose Explorer refresh is dead is a silent
   half-broken install; fail loud instead.

9. **Make the runtime warning diagnosable.** In `refresh_root` (`watcher.py:207-214`), include
   `sys.executable` in the one-shot warning text so `watcher.log` says *which* interpreter is missing
   the module. The current wording ("non-Windows host or pywin32 not installed") gives no way to tell
   a venv gap from a platform gap.

### Part C — docs

10. **`CLAUDE.md`**: update the "Watcher scheduling" bullet to describe deadline-aware rescheduling
    (replacing the current "reschedules to `now + poll_interval_seconds` normally, or
    `now + unavailable_retry_seconds` if a root was unavailable" sentence), update the
    "Watcher/API logging" bullet's quoted `Scan finished` format string, and add a line to the
    "Watcher Explorer refresh" bullet noting pywin32 must be installed **into the venv the scheduled
    task runs** (`.venv\Scripts\python.exe -m pip install pywin32`), not user-site.

## Affected areas / files

- `watcher.py` — `process_root` (return a deadline + discovered count), `run_tick` (aggregate, log
  honest counts, return the wake hint), `main()` (min-based `next_scan_at` with a floor + reason log),
  `refresh_root` (warning names `sys.executable`), new `WAKE_MARGIN_SECONDS` constant.
- `scripts/register-watcher-task.ps1` — pywin32 preflight check against the venv interpreter.
- `CLAUDE.md` — scheduling bullet, logging bullet, Explorer-refresh bullet.
- `.venv` (environment, not tracked) — `pywin32` installed.
- **Unchanged**: `api.py`, `naming.py`, `transcriber.py`, `docker-compose.yml`,
  `watch-config.example.yaml` (no new config keys), `requirements-watcher.txt`.

## Constraints & risks

- **No new config keys.** The chosen approach deliberately reuses `poll_interval_seconds`,
  `stability_seconds`, `unavailable_retry_seconds`, `backoff_*` and `control_check_seconds`. Existing
  `watch-config.yaml` files (including the user's, which predates the last plan's keys) keep working
  untouched — same `{**DEFAULT_API, **raw}` back-compat as before.
- **Busy-loop risk is the main hazard.** A past-due `next_retry_at`, a `stable_since` in the past, or
  clock skew must never produce a zero/negative interval. The `max(interval, control_check_seconds)`
  floor is non-negotiable; without it the watcher hammers `/transcribe`.
- **Don't defeat the stability window.** The wake must land *after* `stable_since + stability_seconds`
  (hence `WAKE_MARGIN_SECONDS`), otherwise the tick re-holds the file and schedules another wake —
  correct but wasteful, and it re-introduces a multi-tick delay.
- **Interaction with `max_files_per_cycle` (5):** a backlog larger than the cap leaves untouched
  candidates that are *already stable*. Their deadline is in the past → the floor makes the next wake
  `control_check_seconds` (5s), which is the desired behavior (drain the backlog quickly) but means
  rapid consecutive ticks. Acceptable — each tick is cheap and the server serializes on its own lock —
  but the reason log should make this visible rather than looking like a spin.
- **`refresh_root` cost per tick**: shorter intervals mean more shell-refresh passes. The root is
  `recursive: false` with ~148 entries, so this is negligible; note it, don't optimize it.
- **`pip install pywin32` in the venv**: the `pywin32_postinstall.py` COM registration step needs
  admin and will likely be skipped again — that is fine and already proven; `Dispatch("Shell.Application")`
  worked without it (verified in the earlier Explorer-refresh session). Do **not** add a
  requirements pin or an admin step over this.
- **Regression to watch**: `process_root`'s return arity changes; every call site
  (`run_tick`, and confirm nothing else) must be updated in the same pass.

## Acceptance criteria / verification

1. **Real reboot test — the user runs this and it is the bar for "done":** shut the host PC down,
   upload a new audio file into `G:\Mi unidad\Documents GDrive\Voice Memos Work` from another device,
   power on, log in, touch nothing. The `.txt` transcript must appear next to the audio **without any
   `POST /watcher/trigger`**. `watcher.log` should show a first tick that discovers and holds, a
   `Next scan in ~65s (reason=stability)` line, then a second tick that submits — all within a few
   minutes of logon.
2. `watcher.log`'s `Scan finished` line reports `discovered` and `held` separately from `submitted`, so
   a held file is no longer indistinguishable from an empty folder.
3. A tick that discovers nothing at all still reschedules at the full `poll_interval_seconds` (3600s)
   — the hourly idle behavior must not regress into constant scanning. Confirm via the reason log
   (`reason=poll_interval`).
4. Backoff is honored at real resolution: force a submit failure (stop the container, or point
   `base_url` at a closed port) with a fixture root and short intervals, and confirm the next wake is
   ~`backoff_base_seconds`, not 3600 — verifiable in the scratchpad with a mock server, no reboot
   needed.
5. No busy loop: with a state entry whose `next_retry_at` is already in the past, the interval floors
   at `control_check_seconds`, not 0. Assert on the reason log across several ticks.
6. `api.enabled: false` still idles (no automatic ticks) while `--once` and a dropped trigger file
   still run a full pass.
7. `.venv\Scripts\python.exe -c "import win32com.client"` succeeds, and a real watcher run logs
   `Refreshed root via shell: G:\... (N entries)` with **no** `Explorer refresh unavailable` warning —
   i.e. the COM path is finally live in production.
8. `scripts/register-watcher-task.ps1` fails with a clear message (and does not register the task) when
   run against a venv without pywin32.
9. Existing `watch-config.yaml` (no new keys) loads unchanged and all behavior above holds.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/watcher-deadline-aware-rescan"   # find the observation
mem_get_observation <id>                          # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/watcher-deadline-aware-rescan.md`. The plan is **approved** — do not
> re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/watcher-deadline-aware-rescan"` then `mem_get_observation` on the top hit; if
> Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach steps, and verify with the Acceptance criteria
> before finishing. Acceptance criterion 1 (real reboot test) is the user's to run — do not claim it.
