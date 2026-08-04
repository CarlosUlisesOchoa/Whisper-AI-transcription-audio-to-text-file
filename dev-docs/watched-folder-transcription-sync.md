# Plan: Watched-folder transcription sync (host agent → container API)

> Date: 2026-08-03 · Slug: `watched-folder-transcription-sync` · Engram topic: `plan/watched-folder-transcription-sync`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

Today transcription is manual: you run the CLI against a folder, one shot, and it stops.

```bash
py audio_to_text_file.py "G:\Mi unidad\Documents GDrive\Voice Memos Work\test" \
  --enroll-unknown --accept --align --language es
```

That CLI already contains the exact "sync" semantics we want — `get_audio_files_status`
(`audio_to_text_file.py:18`) scans a directory, computes `sanitize_filename(base + '.txt')` for each
audio file, and skips anything whose transcript already exists. What is missing is the *continuous*
part: nothing watches the folder, so new voice memos sit untranscribed until someone remembers to
re-run the command.

The audio lives on a **Google Drive Desktop virtual drive** (`G:\Mi unidad\...`). That drive is
mounted per-user by the Google Drive app on the host OS. It cannot be bind-mounted into the
WSL2-backed Docker containers — the mount simply is not visible inside the VM, and even if it were,
Drive's placeholder/hydration behaviour makes it a poor container volume. So the watcher must live
on the **host**, outside the container. That matches the intuition in the original request.

Meanwhile the GPU work already has a proper home: the `whisper-api` container serialises every
transcription behind an `asyncio.Lock` (`api.py:44`) so exactly one job touches the GPU at a time,
whether it comes from the VPS frontend over WireGuard or from anywhere else. Adding a *second*
independent whisper process on the host (a CLI subprocess) would put two model instances on the
same GPU and invite VRAM contention. Therefore the host agent must be a **thin client** that feeds
the existing container queue, not a second processor.

## Goal & outcome

A host-side agent starts at logon and continuously keeps a configured set of directories in sync:
every audio file that lacks its sanitized `.txt` transcript gets submitted to the `whisper-api`
container, and the returned transcript is written next to the audio in the exact same format the
CLI produces. Adding a voice memo to Google Drive is the only action required; the transcript
appears on its own and syncs back up through Drive.

## Scope

- **In scope**
  - New host-side watcher process (`watcher.py`) — polls, detects, submits, polls jobs, writes `.txt`.
  - YAML config listing **multiple watch roots**, each with its own language and transcription flags.
  - Small refactor: extract `AUDIO_EXTENSIONS` + `sanitize_filename` into a dependency-free
    `naming.py` so the watcher can share the *exact* naming rules without importing torch/whisperX.
  - `docker-compose.yml`: publish the API on host loopback so the watcher can reach it.
  - Separate `requirements-watcher.txt` (thin client deps only — no torch, no whisperX).
  - Windows Task Scheduler registration script + rotating log file.
  - Local state file (outside Drive) for in-flight jobs and failure backoff.
  - README / CLAUDE.md documentation of the new component.

- **Out of scope / non-goals**
  - Re-transcribing files that already have a `.txt`. Transcript presence is the source of truth.
  - Any change to transcription quality, diarization, alignment, or enrollment logic.
  - Bind-mounting `G:\` into Docker. Explicitly rejected — not viable with Drive's virtual FS.
  - Running the watcher as a Windows *service* under `SYSTEM`. Google Drive mounts `G:\` per
    interactive user; a SYSTEM service would see nothing. Scheduled Task as the logged-on user only.
  - Linux/macOS host support. Windows-only for now.
  - Deleting, moving, or renaming source audio.
  - A rename/enrollment UI for `unknown-NN.wav`. Manual rename remains the enrollment workflow.
  - Replacing the CLI. `audio_to_text_file.py` keeps working unchanged.

## Approach

### 1. Extract shared naming rules into `naming.py`

The watcher must compute transcript filenames **identically** to the CLI and API, but it must not
import `transcriber.py` (that pulls in torch + whisperX, ~GBs of deps, and would make the thin
client fat).

- Create `naming.py` containing, moved verbatim: `AUDIO_EXTENSIONS`, `ACCENTED_VOWEL_TRANSLATION`,
  and `sanitize_filename` (currently `transcriber.py:19` and `transcriber.py:58`). Standard library
  only — `os`, `re`, `unicodedata`.
- In `transcriber.py`, replace the moved definitions with `from naming import AUDIO_EXTENSIONS, sanitize_filename`
  and keep them as module-level names so every existing import site keeps working untouched
  (`audio_to_text_file.py:4`, `api.py:20`).
- Add `naming.py` to the Dockerfile `COPY` line (`Dockerfile:29`) — otherwise the container build
  breaks on import.

This is the only change to existing Python behaviour, and it is a pure move.

### 2. Publish the API to host loopback

`whisper-api` runs with `network_mode: "service:wireguard"`, so it has no network namespace of its
own — Docker **refuses** a `ports:` mapping on a service in that mode. The mapping must be declared
on the `wireguard` service, which owns the namespace:

```yaml
  wireguard:
    # ...
    ports:
      - "127.0.0.1:8000:8000"   # host-only; WireGuard peers still reach it via the tunnel IP
```

Binding to `127.0.0.1` (not `0.0.0.0`) preserves the existing security posture: nothing on the LAN
or the public internet gains access, only processes on this PC. `SecurityMiddleware` still enforces
`X-API-Key` on `/transcribe` and `/jobs/{id}` (`security.py:24`), so the watcher authenticates like
any other client.

### 3. `watch-config.yaml` — multiple roots, per-root settings

Hand-edited, commented, lives at the repo root and is **gitignored** (contains local machine paths).
Ship `watch-config.example.yaml` as the committed template.

```yaml
api:
  base_url: http://127.0.0.1:8000
  # API key is read from the API_KEY env var / .env — never stored in this file.
  poll_interval_seconds: 30      # how often each root is rescanned
  stability_seconds: 60          # a file must be size-stable this long before submission
  job_poll_seconds: 10           # how often an in-flight job is polled
  job_timeout_seconds: 14400     # 4h ceiling — a ~2h file must fit comfortably
  max_inflight: 1                # outstanding jobs at once (see Constraints)
  max_files_per_cycle: 5         # backlog throttle
  order: newest_first            # newest_first | oldest_first

watch:
  - path: "G:\\Mi unidad\\Documents GDrive\\Voice Memos Work\\test"
    recursive: true
    language: es
    align: true
    diarize: true
    enroll_unknown: true

  # Add more roots here; each carries its own language and flags.
  # - path: "G:\\Mi unidad\\Documents GDrive\\Some Other Folder"
  #   recursive: false
  #   language: en
  #   align: true
  #   diarize: true
  #   enroll_unknown: false
```

Flag defaults mirror the reference command (`--enroll-unknown --align --language es`, diarization
on). `--accept` has no equivalent — the watcher is non-interactive by definition.

### 4. Discovery loop

Per tick, for each configured root:

1. **Root availability.** If the path does not exist or is not a directory (Google Drive app not
   running, `G:` not mounted, machine still booting), log a warning **once per state transition**
   (not every tick — that floods the log) and skip this root. Never crash, never exit. Re-check next
   tick; log recovery when it comes back. This is the direct answer to "the directory path depends
   on the Google Drive application running".
2. **Walk.** `os.walk` when `recursive: true`, else a flat `os.listdir`. Filter to
   `naming.AUDIO_EXTENSIONS`. Skip zero-byte files.
3. **Transcript check.** For each audio file, `expected = sanitize_filename(stem + '.txt')` resolved
   **in the audio file's own directory**. If it exists → done, skip. This mirrors
   `audio_to_text_file.py:31-38` exactly and keeps each subfolder self-contained.
4. **Stability check.** Google Drive streams files; one can appear while still downloading or while
   still being uploaded from a phone. Record `(size, mtime)` per candidate; only enqueue once both
   are unchanged for `stability_seconds`. Prevents submitting a half-materialised file.
5. **Backoff filter.** Skip anything whose state entry has `next_retry_at` in the future or has
   exhausted `max_attempts`.
6. **Order + throttle.** Sort per `order`, take at most `max_files_per_cycle`. The Work root has a
   substantial untranscribed backlog and year-based subfolders; without a throttle the first run
   would try to drain all of it at once.

### 5. Submission and job polling

Sequential, at most `max_inflight` outstanding:

1. `POST {base_url}/transcribe`, multipart: `file`, `language`, `align`, `diarize`,
   `enroll_unknown`; header `X-API-Key`. Streamed upload — do not read the whole file into memory
   (a 2h WAV is ~1.3GB). Reading the file is what forces Google Drive to hydrate a cloud-only
   placeholder; an `OSError` here is transient → mark for retry, do not count as a hard failure.
2. On the returned `job_id`, persist it to state immediately (before any polling), so a watcher
   restart can resume rather than duplicate.
3. Poll `GET /jobs/{job_id}` every `job_poll_seconds`.
   - `completed` → write `result.formatted` to the expected `.txt` path, UTF-8, **atomically**:
     write to `<name>.txt.tmp` in the same directory then `os.replace`. A non-atomic write lets
     Google Drive sync a half-written transcript. Log any `result.enrolled_speakers` entries so new
     `unknown-NN.wav` files are visible in the log and you know to rename them.
   - `failed` → record `error`, increment attempts, schedule exponential backoff.
   - `404` → the job was purged by `JOB_TTL_SECONDS` (default 3600, `api.py:32`) because the
     watcher was down longer than the TTL. Reset the file to pending and re-submit.
   - Exceeding `job_timeout_seconds` → give up polling, reset to pending with backoff.

### 6. State file

`%LOCALAPPDATA%\whisper-watcher\state.json`. **Deliberately outside the Drive folder** — writing it
into the watched tree would sync junk to Drive and re-trigger the watcher on its own writes.

Per absolute audio path: `status` (pending / stable / submitted / failed), `attempts`, `last_error`,
`last_size`, `last_mtime`, `first_seen_at`, `job_id`, `next_retry_at`.

The `.txt` file remains the **only** source of truth for "done" — state is purely an optimisation
for in-flight tracking and failure backoff. Deleting `state.json` is therefore always safe: the
watcher rebuilds it and re-syncs anything still missing a transcript. Write it atomically too.

### 7. Logging and CLI surface

- Rotating file `%LOCALAPPDATA%\whisper-watcher\watcher.log` (e.g. 5MB × 3) plus stdout.
- `python watcher.py --dry-run` → one pass, print what *would* be queued, submit nothing. This is
  the watcher's analogue of the CLI's confirmation prompt, and the safe way to sanity-check a new
  root before pointing the agent at a 300-file backlog.
- `python watcher.py --once` → single sync pass then exit (useful for manual catch-up and testing).
- `python watcher.py --retry-failed` → clear backoff/attempt counters and try everything again.
- `python watcher.py --config <path>` → override config location.
- Force UTF-8 output (`sys.stdout.reconfigure(encoding="utf-8")` guarded by a try/except) — the same
  `cp1252` crash that affects the CLI's `✓`/`✗` output applies here.

### 8. Scheduled Task registration

`scripts/register-watcher-task.ps1`, run once from an elevated PowerShell:

- Action: the venv's `pythonw.exe` (windowless) running `watcher.py`, working directory = repo root.
- Trigger: **At log on**, for the current interactive user specifically.
- Settings: `-RunOnlyIfNetworkAvailable:$false`, restart on failure (e.g. every 5 min, 3 times),
  **no execution time limit** (`ExecutionTimeLimit = 0`), do not stop on battery/idle.
- Environment: `PYTHONUTF8=1`.
- **Must run as the interactive user, not SYSTEM** — Google Drive mounts `G:` per user session.
- A matching `unregister-watcher-task.ps1` for clean removal.

## Affected areas / files

- `naming.py` — **new**. Dep-free `AUDIO_EXTENSIONS`, `ACCENTED_VOWEL_TRANSLATION`,
  `sanitize_filename`, moved out of `transcriber.py`.
- `transcriber.py` — imports the above from `naming` and re-exports; no behaviour change. Existing
  `from transcriber import sanitize_filename, AUDIO_EXTENSIONS` call sites stay valid.
- `watcher.py` — **new**. The host agent: config load, discovery, stability, submission, job polling,
  atomic `.txt` write, state, logging, CLI flags.
- `watch-config.example.yaml` — **new**, committed template.
- `watch-config.yaml` — **new**, local, gitignored.
- `requirements-watcher.txt` — **new**. Thin-client deps only: `requests`, `pyyaml`,
  `python-dotenv`. Deliberately excludes torch/whisperX so the watcher can run in a plain Python
  env with no GPU stack.
- `docker-compose.yml` — add `ports: ["127.0.0.1:8000:8000"]` to the **wireguard** service.
- `Dockerfile:29` — add `naming.py` to the `COPY` line.
- `.gitignore` — add `watch-config.yaml`.
- `scripts/register-watcher-task.ps1`, `scripts/unregister-watcher-task.ps1` — **new**.
- `README.md`, `CLAUDE.md` — document the watcher as a third mode alongside CLI and API, including
  the "Drive must be running / per-user mount" constraint and the loopback port publish.

## Constraints & risks

- **Google Drive must be running.** `G:` only exists while the Drive Desktop app is up, and only in
  the interactive user's session. Handled by the availability check in step 4.1 and the Scheduled
  Task running as the user. This is a hard architectural constraint, not a bug to fix.
- **Placeholder hydration.** Cloud-only files materialise on read. The upload read can therefore be
  slow or raise `OSError` if Drive is offline. Treated as transient (retry), not a hard failure.
- **Partially-synced files.** The stability check (size+mtime unchanged for `stability_seconds`) is
  the mitigation. Without it, a file still uploading from a phone gets transcribed truncated — and
  because the `.txt` then exists, it would never be retried. This is the single highest-value guard
  in the plan.
- **Filename collisions are real and already present.** `sanitize_filename` maps
  `<base>.m4a` and `<base>.mp3` to the *same* `.txt`. The Work folder already contains such a pair
  today. Whichever transcribes first wins; the other is then permanently "already transcribed".
  This is existing CLI behaviour (`audio_to_text_file.py:31-38`), inherited deliberately. Log a
  warning when two audio files in one directory resolve to the same transcript name so it is at
  least visible.
- **Backlog size.** The Work root plus its year subfolders holds a large untranscribed backlog.
  `max_files_per_cycle` + `--dry-run` exist specifically so the first run is deliberate. Point the
  agent at the `test` subfolder first.
- **Disk pressure from queueing.** `/transcribe` copies each upload to a container temp file before
  queueing (`api.py:204-208`) and only deletes it in `_process_job`'s `finally` (`api.py:160`).
  Submitting 50 files at once would spool 50 full copies inside the container simultaneously — up to
  `MAX_UPLOAD_SIZE_MB` (2048) each. Hence `max_inflight: 1` as the default.
- **Job TTL vs. watcher downtime.** `JOB_TTL_SECONDS` defaults to 3600 from job *completion*. A
  watcher offline longer than that loses the result and must re-transcribe. The `404` → re-submit
  path handles it correctly, at the cost of GPU time. Raising `JOB_TTL_SECONDS` is the mitigation if
  it ever bites.
- **Non-atomic writes sync garbage.** Drive picks up files as they are written. Both the `.txt` and
  `state.json` writes must be write-tmp-then-`os.replace`.
- **Contention with the VPS frontend.** Both feed the same container queue. The `asyncio.Lock`
  serialises correctly, but a large watcher backlog will delay interactive frontend jobs. Accepted
  for now; `max_files_per_cycle` limits the damage. A priority queue is a future concern, not this
  change.
- **Auto-enrollment side effects.** With `enroll_unknown: true` running unattended, the container
  writes `unknown-NN.wav` into the mounted `./voices` continuously. Over a backlog this can produce
  many entries. They are logged per job so you can rename the real ones; consider disabling
  `enroll_unknown` for the initial backlog drain and enabling it for the ongoing steady state.
- **`ports` on the wrong service.** Declaring `ports:` on `whisper-api` while it uses
  `network_mode: "service:wireguard"` is a hard Docker error. It must go on `wireguard`.
- **Windows console encoding.** `PYTHONUTF8=1` required, same as the CLI.

## Acceptance criteria / verification

1. **Existing paths unbroken.** After the `naming.py` extraction:
   `$env:PYTHONUTF8=1; py audio_to_text_file.py "G:\Mi unidad\Documents GDrive\Voice Memos Work\test" --accept`
   → reports all three existing files as already transcribed, no import errors.
   `docker compose build` succeeds (proves `naming.py` reached the image).
2. **API reachable from host.** `docker compose up -d`, then from host PowerShell:
   `curl http://127.0.0.1:8000/health` → 200, `"device":"cuda"`, `"diarization_enabled":true`.
   Confirm it is loopback-only: the same call from another LAN machine must fail.
3. **Dry run.** `py watcher.py --dry-run` against the `test` root → lists nothing (all three files
   already have `.txt`). Move one `.txt` aside → it now lists exactly that one audio file.
4. **End-to-end.** Drop a fresh `.m4a` into the `test` root. Within
   `poll_interval_seconds + stability_seconds` the log shows detected → stable → submitted →
   completed, and a `.txt` with the sanitized name appears next to the audio, containing the
   `===` / `filename:` header and `[start - end] speaker: text` lines with named or `unknown-NN`
   speakers. Compare byte-for-byte against the CLI's output for the same file — must be identical.
5. **Idempotence.** Next tick skips that file. No second job, no duplicate `.txt`.
6. **Recursion.** Place an audio file in a year subfolder → it is picked up and its `.txt` is
   written *in that subfolder*, not in the root.
7. **Drive unavailable.** Quit the Google Drive app so `G:` disappears. The watcher logs one warning
   and keeps running. Restart Drive → the watcher logs recovery and resumes without a restart.
8. **Partial file.** Begin copying a large `.wav` into a watched root. The watcher must not submit
   while the size is still changing; it submits only after the copy settles.
9. **Restart resilience.** Kill the watcher mid-job, restart it. It resumes polling the stored
   `job_id` (or re-submits on 404) and exactly one `.txt` results.
10. **Failure backoff.** Point it at a deliberately corrupt audio file. The job fails, attempts
    increment with growing delay, and after `max_attempts` it is skipped with a logged reason —
    without blocking other files.
11. **Scheduled Task.** Run `register-watcher-task.ps1`, log off and back on. The task is running as
    your user, the log file shows a fresh startup line, and a newly dropped file gets transcribed
    with no terminal open.
12. **Multi-root.** Add a second root with a different `language` to the config. Both are polled and
    each file is submitted with its own root's language and flags.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/watched-folder-transcription-sync"
mem_get_observation <id>
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/watched-folder-transcription-sync.md`. The plan is **approved** —
> do not re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/watched-folder-transcription-sync"` then `mem_get_observation` on the top hit;
> if Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach steps, and verify with the Acceptance criteria
> before finishing.
