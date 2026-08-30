# Plan: watcher startup readiness — wait for Google Drive before the first scan

> Date: 2026-08-10 · Slug: `watcher-startup-drive-readiness` · Engram topic: `plan/watcher-startup-drive-readiness`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

`watcher.py` runs from a Windows Scheduled Task at logon (`scripts/register-watcher-task.ps1`,
registered as the **interactive user** because Google Drive mounts `G:` per-user session). At
logon it races the Drive mount, and today it loses that race badly:

- `main()` sets `next_scan_at = 0.0`, so the first tick fires **immediately** at logon.
- If `G:` isn't mounted yet, `check_root_available()` logs `Root unavailable (Google Drive not
  mounted, or still starting up?)` and `process_root()` returns `[]`.
- `run_tick()` then reschedules `next_scan_at = now + poll_interval_seconds` — **3600s**.

Net effect: a boot where Drive is even 20 seconds late costs a **full hour** of no transcription,
with a single warning line in `watcher.log` as the only evidence. On this host the failure is
worse than "late": Google Drive's behavior at logon **varies** — sometimes it starts, sometimes it
doesn't start at all, in which case the hourly scans keep finding nothing forever.

## Goal & outcome

At logon the watcher waits (bounded) for its configured roots to appear — launching Google Drive
itself if it isn't running — and scans **as soon as** the roots are available, instead of blindly
firing one doomed tick and sleeping for an hour. A root that disappears mid-run is retried in
~60s, not 3600s.

## Scope

- **In scope**:
  - A bounded startup readiness gate in `watcher.py` (initial delay → poll → timeout → scan anyway).
  - Optional Google Drive autostart via Google's own `launch.bat` when roots are missing at startup.
  - Short-interval rescheduling when a tick found any root unavailable (instead of the full hour).
  - New `api:` config keys + `watch-config.example.yaml` + `CLAUDE.md` documentation.
  - **Optional / flagged** (step 6 below — beyond the literal ask, drop it if unwanted): the same
    bounded wait against `GET /health` so the first tick doesn't burn its retry budget while
    Docker/whisper-api is still starting.
- **Out of scope / non-goals**:
  - No change to the stability window, submission, job polling, backoff, or Explorer-refresh logic.
  - No Drive account/auth/sign-in handling — if Drive launches but isn't signed in, that's the
    user's problem; the watcher just times out and scans what it can.
  - No relaunching Drive **mid-run** (explicitly decided against — startup-only launch).
  - No Task Scheduler trigger-delay change; the in-process wait replaces that idea (a fixed task
    delay would penalize every boot, including the ones where Drive is already up).

## Approach

### 1. New config keys (`DEFAULT_API` in `watcher.py` + `watch-config.example.yaml`)

```yaml
api:
  startup_delay_seconds: 30       # grace period before the first readiness re-check (user's "at least 30s")
  root_wait_timeout_seconds: 300  # stop waiting for roots and scan anyway after this
  root_wait_poll_seconds: 5       # how often roots are re-checked while waiting
  unavailable_retry_seconds: 60   # next-scan interval after a tick where any root was unavailable
  drive_autostart: true           # launch Google Drive (launch.bat) if roots are missing at startup — Windows only
  drive_launcher: ""              # explicit path to launch.bat / GoogleDriveFS.exe; empty = auto-detect
```

Keep the existing `DEFAULT_API`-merge pattern (`{**DEFAULT_API, **(raw.get("api") or {})}`) — every
key stays optional, old `watch-config.yaml` files keep working untouched.

### 2. `resolve_drive_launcher(configured)` → `str | None`

Google ships `C:\Program Files\Google\Drive File Stream\launch.bat`, a version-agnostic launcher
(inspected during planning). It resolves the exe in two steps:

1. `reg.exe query HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\{6BBAE539-2232-434A-A4E5-9A33560C6283} /v InstallLocation`
   → full path to `GoogleDriveFS.exe`.
2. Fallback: newest (`/o:-d /t:c`) versioned subdir next to itself containing `GoogleDriveFS.exe`
   (this host currently has `128.0.0.0/` and `129.0.1.0/`).

Resolution order in the watcher (never hardcode `C:\Program Files` or a version number):

1. `api.drive_launcher` if set and `os.path.isfile`.
2. **The same registry key, read directly via `winreg`** — preferred: it yields the `.exe`, so we
   skip `cmd.exe` and `launch.bat` entirely.
   ```python
   import winreg
   key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\{6BBAE539-2232-434A-A4E5-9A33560C6283}"
   with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
       install_location, _ = winreg.QueryValueEx(k, "InstallLocation")
   ```
   Accept it only if it exists and basename is `GoogleDriveFS.exe` (mirrors `launch.bat`'s own check).
3. `launch.bat` under, in order: `%ProgramW6432%`, `%ProgramFiles%`, `%ProgramFiles(x86)%`
   → `\Google\Drive File Stream\launch.bat`; then `%LOCALAPPDATA%\Google\DriveFS\launch.bat`.
4. Nothing found → return `None` and warn **once** (module-level `_drive_launch_warned`, same
   one-shot pattern as `_refresh_warned` / `_root_available`).

Wrap the whole thing in try/except — a registry read must never abort a scan.

### 3. `drive_is_running()` → `bool`

```python
subprocess.run(["tasklist", "/FI", "IMAGENAME eq GoogleDriveFS.exe", "/NH"],
               capture_output=True, text=True, timeout=10)
```
`"GoogleDriveFS.exe" in stdout` → running. Any exception/timeout → return `False` (assume not
running). Deliberately **not** `psutil`: `requirements-watcher.txt` is a thin client
(`requests`, `pyyaml`, `python-dotenv`, `pywin32`) and stays that way — `subprocess` is stdlib.

### 4. `launch_drive(launcher_path)` → `bool`

```python
cmd = ["cmd", "/c", launcher_path] if launcher_path.lower().endswith(".bat") else [launcher_path]
subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                 stderr=subprocess.DEVNULL, close_fds=True,
                 creationflags=0x08000000 | 0x00000008)  # CREATE_NO_WINDOW | DETACHED_PROCESS
```

- `stdin=subprocess.DEVNULL` is **mandatory**: `launch.bat`'s `:FAIL` branch calls `pause`, which
  under `pythonw.exe` (no console) would otherwise leave a `cmd.exe` hanging forever. With DEVNULL
  it reads EOF and exits.
- A `.bat` cannot be `Popen`'d directly (CreateProcess won't run it) — hence `cmd /c`.
- Never `wait()`; `launch.bat` itself uses `start`, so it returns immediately anyway.
- Never raises: log a warning on failure and continue.

### 5. `wait_for_roots(watch_roots, api_cfg)` → `bool`

```
missing = [r["path"] for r in watch_roots if not os.path.isdir(r["path"])]
if not missing: return True                      # zero cost on a healthy boot — no 30s penalty
log.warning("Waiting for %d root(s) to become available: %s", len(missing), missing)
if api_cfg["drive_autostart"] and os.name == "nt" and not drive_is_running():
    launcher = resolve_drive_launcher(api_cfg["drive_launcher"])
    if launcher: launch_drive(launcher)          # logs which path it used
deadline = time.time() + api_cfg["root_wait_timeout_seconds"]
time.sleep(api_cfg["startup_delay_seconds"])     # counts against the same deadline
while time.time() < deadline:
    if all roots isdir: log.info("All roots available after %.0fs", elapsed); return True
    time.sleep(api_cfg["root_wait_poll_seconds"])
log.warning("Timed out after %.0fs waiting for: %s — scanning anyway.", elapsed, still_missing)
return False
```

Use plain `os.path.isdir` here, **not** `check_root_available()` — the latter owns the
`_root_available` transition-logging state and would emit misleading recovered/unavailable pairs
during the wait. `check_root_available` keeps doing its job inside `process_root`.

Called from `main()` **before** the loop, so it covers continuous mode and `--once`. Also call it
in the `--dry-run` path: it costs nothing when roots are present, and a dry-run right after boot
is exactly when you want it.

### 6. Optional — `wait_for_api(api_cfg)` (flagged addition, easy to drop)

At logon Docker Desktop / `whisper-api` is likely *also* still starting. Today the first tick
submits, gets a connection error, and walks the backoff ladder (60 → 120 → 240 → 480s); with
`max_attempts: 5` the file is marked `failed` in ~15 minutes and needs a manual `--retry-failed`.
Same bounded-wait shape against `GET {base_url}/health` (new `api_wait_timeout_seconds`, default
300; no API key needed — `/health` is exempt in `security.py`). Log and proceed on timeout.
**This is beyond the literal ask** — implement it only if wanted; nothing else in the plan depends
on it.

### 7. Short-retry rescheduling on unavailable roots

- `process_root(...)` currently returns `[]` for an unavailable root, indistinguishable from
  "available, nothing new". Change its return to `(ready, available)` and update the single call
  site in `run_tick`.
- `run_tick(...)` returns `any_unavailable: bool` (and includes it in the existing
  `"Scan finished (source=%s): ..."` line).
- `main()`:
  ```python
  any_unavailable = run_tick(...)
  interval = api_cfg["unavailable_retry_seconds"] if any_unavailable else api_cfg["poll_interval_seconds"]
  next_scan_at = time.time() + interval
  ```
  Log which interval was chosen and why when it's the short one. Recovery already logs
  `"Root recovered: %s"` via `check_root_available`.
- Applies to scheduled ticks; `--once` still exits after one pass regardless.

### 8. Documentation

- `watch-config.example.yaml`: the six new keys with inline comments (match the existing style).
- `CLAUDE.md`: a new bullet under **Key behaviors**, "Watcher startup readiness / Drive autostart",
  covering the gate, the `launch.bat`/registry resolution, the `pause`/`stdin=DEVNULL` gotcha, and
  the short-retry interval. Update the "Running the Watcher" section to mention that the watcher
  can now start Drive itself.

## Affected areas / files

- `watcher.py` — `DEFAULT_API` (6 new keys), new `resolve_drive_launcher` / `drive_is_running` /
  `launch_drive` / `wait_for_roots` (+ optional `wait_for_api`), `process_root` returns
  `(ready, available)`, `run_tick` returns `any_unavailable`, `main()` calls the gate before the
  loop and picks the short interval after an unavailable tick. New stdlib imports: `subprocess`,
  `winreg` (guarded — Windows-only).
- `watch-config.example.yaml` — document the new `api:` keys.
- `CLAUDE.md` — new Watcher behaviors bullet + "Running the Watcher" note.
- `scripts/register-watcher-task.ps1` — **no change** (the in-process wait is the fix; a Task
  Scheduler `Delay` would tax every boot).
- `requirements-watcher.txt` — **no change** (stdlib only; no `psutil`).

## Constraints & risks

- **`launch.bat` `pause` trap**: `:FAIL` blocks on a keypress. Under `pythonw.exe` there's no
  console to press it in. `stdin=subprocess.DEVNULL` is what makes this safe — do not omit it.
- **`.bat` via `Popen`** needs `["cmd", "/c", path]`; a bare path silently fails.
- **`isdir(root)` is not "Drive is ready"**: the mount existing does not mean that folder's listing
  is hydrated. The existing `refresh_root()` shell enumeration (see the Explorer-refresh work in
  `dev-docs/watcher-explorer-refresh-roots.md`) still owns that problem and runs unchanged after
  the gate. Don't conflate the two.
- **Log noise**: while Drive is missing, a 60s retry interval means ~60 `Scan starting/finished`
  pairs per hour instead of 1. Acceptable and tunable via `unavailable_retry_seconds`; the rotating
  handler (5MB × 3) absorbs it.
- **Launching a running Drive**: guarded by `drive_is_running()`, but `tasklist` failure falls
  through to "not running". GoogleDriveFS is effectively single-instance, so a duplicate launch is
  benign (worst case it focuses the UI) — but it's why the launch only happens when a root is
  actually missing.
- **Admin/permissions**: the task runs as the interactive user, `RunLevel Limited`. Reading the
  `HKLM\...\Uninstall` key and launching `launch.bat` both work unelevated — verify anyway.
- **`--dry-run` UX**: the gate can now make a dry-run pause up to `root_wait_timeout_seconds` when
  a root is genuinely missing. That's intended (it's the honest answer), but mention it in the log.
- **Non-Windows**: `winreg`/`tasklist`/`launch.bat` are all Windows-only. Guard on `os.name == "nt"`
  and degrade to wait-only, warn once — same pattern `refresh_root` already uses.

## Acceptance criteria / verification

Run with `$env:PYTHONUTF8=1`; check `%LOCALAPPDATA%\whisper-watcher\watcher.log` for each.

1. **No cost on a healthy boot**: all roots present → `py watcher.py --once` scans immediately, no
   wait log line, no 30s delay.
2. **Late mount (simulated)**: point a root at a not-yet-created dir, start the watcher, create the
   dir ~45s later → log shows the wait, then `All roots available after ~Ns`, then a scan — **not**
   an hour later.
3. **Permanently missing root**: point a root at `Z:\nope` → Drive launch attempt (if
   `drive_autostart`), bounded wait, `Timed out after 300s ... scanning anyway`, and the *other*
   roots still scan normally in that same tick.
4. **Mid-run drop**: after a good tick, rename the root dir → next tick logs the unavailable root
   and reschedules at `unavailable_retry_seconds` (60s, stated in the log), not 3600s; restore the
   dir → `Root recovered:` line and a normal scan.
5. **Real logon test** (the actual repro): disable Drive's own autostart, reboot, log in, don't
   touch anything → watcher launches Drive via the resolved launcher, `G:` appears, first scan
   completes within ~2 minutes of logon. Confirm no stray `cmd.exe` / Drive window is left behind
   (Task Manager) and that the resolved launcher path is in the log.
6. **`drive_autostart: false`** → wait-only, zero launch attempts logged.
7. **Launcher not found** (temporarily set `drive_launcher` to a bogus path *and* rename the
   Program Files dir, or fake the `winreg` import failure) → one warning, no crash, wait continues.
8. **Config back-compat**: an existing `watch-config.yaml` with none of the new keys runs
   unchanged with the documented defaults.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/watcher-startup-drive-readiness"   # find the observation
mem_get_observation <id>                            # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/watcher-startup-drive-readiness.md`. The plan is **approved** —
> do not re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/watcher-startup-drive-readiness"` then `mem_get_observation` on the top hit; if
> Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach steps, and verify with the Acceptance criteria
> before finishing.
