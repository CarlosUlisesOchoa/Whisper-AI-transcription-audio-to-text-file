# Plan: Force a Windows shell refresh of each watch root before scanning

> Date: 2026-08-04 · Slug: `watcher-explorer-refresh-roots` · Engram topic: `plan/watcher-explorer-refresh-roots`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

`watcher.py` discovers work by walking each root from `watch-config.yaml` with `os.walk`
(`discover_candidates`, `watcher.py:145`). On the Google Drive for Desktop virtual drive (`G:`),
that is not reliable: files created on another device (phone voice memos, another PC) do **not**
always appear to a plain Win32 `FindFirstFile`/`os.walk` enumeration until the **Windows shell**
enumerates the folder — i.e. until the user opens it in Explorer. Drive's virtual filesystem
materializes the directory listing lazily on shell access.

Consequence today: a new recording can sit in a watched folder indefinitely, the hourly scan finds
nothing, `watcher.log` reports `0 candidate(s) found`, and the file is only picked up after the
user happens to open that folder in Explorer by hand. That defeats the whole point of the watcher.

Fix: before walking a root, force the shell to enumerate it.

## Goal & outcome

Every scan (scheduled tick, manual trigger, `--once`, and `--dry-run`) refreshes each configured
root through the Windows shell namespace **before** `os.walk` runs, so a file added from another
device is discovered without anyone opening Explorer. Refresh is on by default, overridable per
root, and never fails the scan — if it can't run, the walk proceeds as it does today.

## Scope

- **In scope**: `watcher.py` only — a new `refresh_root` helper, its call sites in `process_root`
  and `run_dry_run`, two new `api.*` config defaults, an optional per-root override, the
  `pywin32` dependency, and the doc updates for all of it.
- **Out of scope / non-goals**:
  - The CLI (`audio_to_text_file.py`) and API (`api.py`) — they scan a directory the user
    explicitly points at, in the foreground; the staleness problem is specific to the unattended
    hourly watcher.
  - Any change to discovery rules, skip logic, stability window, backoff, or submission.
  - Non-Windows support beyond degrading gracefully (the watcher is already Windows-shaped:
    `LOCALAPPDATA` state dir, `G:` Drive roots, Scheduled Task registration).
  - Making Drive hydrate file *contents* — this is about directory **listings**. Content
    hydration is already handled by the existing read-error retry path in `submit_and_track`
    (`watcher.py:284`).

## Approach

### 1. Guarded `pywin32` import (`watcher.py`, near the top)

```python
try:
    import win32com.client  # pywin32 — Windows-only, optional
except ImportError:  # not installed, or non-Windows host
    win32com = None
```

`pythoncom` initializes COM for the thread that imports it, and the watcher's loop is
single-threaded (`main`, `watcher.py:508`), so no explicit `CoInitialize` is needed. If a
`CO_E_NOTINITIALIZED` ever surfaces, the try/except in step 2 already turns it into the fallback
path rather than a crash.

### 2. New helper `refresh_root(path, recursive, settle_seconds)`

Placed next to `check_root_available` (`watcher.py:132`), before the Discovery section.

Behavior, in order:

1. If `os.name != "nt"` or `win32com is None` → log **once** (module-level `_refresh_warned`
   flag, same pattern as `_root_available`) and return. Never warn per-tick — this runs hourly.
2. **Primary, headless**: `shell = win32com.client.Dispatch("Shell.Application")`,
   `folder = shell.NameSpace(path)`, then materialize `folder.Items()` (e.g. read `.Name` on each
   item). This goes through the *same shell namespace provider Explorer uses*, which is what
   triggers Drive's refresh — but opens no window.
   - If `recursive` is true for this root, recurse into each item where `item.IsFolder` is true,
     re-entering `NameSpace(item.Path)`. A `recursive: true` root's **subfolders are scanned by
     `os.walk` too**, so they are equally stale — refreshing only the top level would leave the
     bug in place for anything nested. **Assumption flagged for review**: this is the reading
     taken here; it costs one shell enumeration per directory per tick, which is fine for the
     small Drive trees in use but would be noticeable on a very deep root.
3. **Fallback, visible**: on *any* exception from step 2 (Dispatch failure, `NameSpace` returning
   `None`, `Items()` raising), fall back to `os.startfile(path)` + `time.sleep(settle_seconds)` —
   a real Explorer window, exactly what the user does by hand. The window is **left open**; it
   only appears when COM is broken, so it doubles as a visible signal that something is wrong.
   Wrap this in its own try/except too — a failure here logs a warning and returns.
4. Log at INFO on the primary path: `"Refreshed root via shell: %s (%d entries)"`. Log at WARNING
   when falling back. Hourly × few roots is acceptable log volume and makes the refresh auditable
   in `watcher.log` alongside the existing `Scan starting` / `Scan finished` lines.

`refresh_root` **never raises** — a refresh failure must not abort a scan.

### 3. Config

Add to `DEFAULT_API` (`watcher.py:27`):

```python
"explorer_refresh": True,        # force a Windows shell enumeration of each root before walking it
"explorer_settle_seconds": 3,    # only used by the visible-Explorer fallback
```

Per-root override resolved as `root_cfg.get("explorer_refresh", api_cfg["explorer_refresh"])` —
so a purely local root (`D:\...`, no Drive involved) can set `explorer_refresh: false` and skip
the COM work it doesn't need.

### 4. Call sites

- `process_root` (`watcher.py:197`): after `check_root_available(path)` returns true, before
  `discover_candidates(root_cfg)`. Ordering matters — refreshing an unmounted root is pointless
  and `os.startfile` on a nonexistent path raises.
- `run_dry_run` (`watcher.py:421`): after its own `os.path.isdir(path)` check, before
  `discover_candidates`. Without this, `--dry-run` under-reports the backlog on a stale folder
  and stops matching what a real tick would see.

Both paths honor the per-root override. `run_tick` needs no change — it delegates to
`process_root`, so scheduled, manual-trigger and `--once` ticks all inherit the refresh.

### 5. Dependency

`requirements-watcher.txt`: add `pywin32; sys_platform == "win32"`. Keeps the watcher a thin
client (no torch/whisperX) and keeps a non-Windows `pip install` working.

### 6. Docs

- `watch-config.example.yaml`: document `explorer_refresh` / `explorer_settle_seconds` under
  `api:`, and show the per-root override on the commented second root.
- `CLAUDE.md`: extend the **Watcher discovery loop** bullet with the shell-refresh step and the
  reason (Drive listings are stale until the shell enumerates); add both env/config keys where the
  other watcher knobs are described; add the `pywin32` install note under "Running the Watcher".
- `CLAUDE.md` → **Verification status**: record the live result from the acceptance run below.

## Affected areas / files

- `watcher.py` — guarded `win32com` import; new `refresh_root` helper + `_refresh_warned` flag;
  two new `DEFAULT_API` keys; refresh calls in `process_root` and `run_dry_run`.
- `watch-config.example.yaml` — document the two `api.*` keys and the per-root override.
- `requirements-watcher.txt` — add `pywin32; sys_platform == "win32"`.
- `CLAUDE.md` — discovery-loop behavior, config keys, install note, verification status.

## Constraints & risks

- **The headless COM path is the unverified part.** `Shell.Application.NameSpace().Items()` uses
  the same shell provider as Explorer, so it *should* trigger Drive's refresh — but that is a
  hypothesis until the live test below runs. If it turns out COM enumeration alone does **not**
  wake Drive, the fix is to promote `os.startfile` from fallback to primary (behind the same
  config keys, so no config churn) — the plan's structure already accommodates that flip.
- **Recursive refresh cost**: one shell enumeration per directory per tick on `recursive: true`
  roots. Fine hourly on small trees; revisit (depth cap) if a root ever grows deep.
- **COM in a Scheduled Task session**: the watcher runs as the interactive user at logon
  (`scripts/register-watcher-task.ps1`), which is required anyway for Drive's per-user `G:` mount,
  so a shell/COM context exists. If it were ever run as SYSTEM, COM would likely fail — and would
  degrade to the visible-Explorer fallback, which would also fail invisibly. Not a new failure
  mode (SYSTEM already can't see `G:`), but worth remembering.
- **`os.startfile` fallback leaves a window open per root per tick.** Acceptable only because it
  is an error path. If it ever fires on a schedule, that's a bug to fix, not a UX to tune.
- **Degradation is mandatory, not optional**: missing `pywin32`, non-Windows, or any COM error
  must leave the watcher behaving exactly as it does today (walk anyway, warn once).
- No change to the `.txt`-is-source-of-truth contract, so nothing here can cause a re-transcribe.

## Acceptance criteria / verification

Live, against the real `G:` Drive root — the repro **and** the fix, because a passing run alone
doesn't prove the staleness was what got fixed:

1. Add an audio file to a watched Drive root from another device (phone / another PC).
2. Do **not** open that folder in Windows Explorer.
3. With `explorer_refresh: false` for that root, run `py watcher.py --dry-run` → the file is
   **missing** from the output. (Repro confirmed. If it *is* listed, the folder wasn't actually
   stale — start over with a fresh file.)
4. With `explorer_refresh: true` (default), run `py watcher.py --dry-run` → the file **is** listed
   as `[dry-run] Would queue: ...`. (Fix confirmed.)
5. `watcher.log` shows a `Refreshed root via shell: ... (N entries)` line per root per scan, and
   **no** `Explorer fallback` warning — i.e. the headless path did the work and no window opened.
6. Run `py watcher.py --once` and confirm the file transcribes end-to-end and its `.txt` lands
   next to the audio (unchanged existing behavior, just now reachable).
7. Regression: a root with `explorer_refresh: false` logs a skip and still walks normally; a run
   with `pywin32` uninstalled warns once and still walks normally.

Remember `$env:PYTHONUTF8=1` before running (existing project requirement).

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/watcher-explorer-refresh-roots"   # find the observation
mem_get_observation <id>                            # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/watcher-explorer-refresh-roots.md`. The plan is **approved** —
> do not re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/watcher-explorer-refresh-roots"` then `mem_get_observation` on the top hit; if
> Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach steps, and verify with the Acceptance criteria
> before finishing.
