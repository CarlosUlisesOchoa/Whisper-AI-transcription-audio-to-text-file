# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python tool that uses `whisperX` (CTranslate2 backend under the hood) to transcribe audio files, with automatic GPU (CUDA) acceleration when available. Adds **speaker diarization** (pyannote 3.1) and **named speaker identification** (voice enrollment) on top of transcription. Three modes: **CLI** for batch transcription of a folder, **API** (FastAPI + async job queue) for remote transcription over a WireGuard tunnel, and **Watcher** — a host-side agent (`watcher.py`) that continuously syncs Google Drive folders against the API.

## Running the Script

```bash
# Basic usage
py audio_to_text_file.py "path/to/audio/folder"

# Specify language (default: auto-detect)
py audio_to_text_file.py "path/to/audio/folder" --language es

# Skip confirmation prompt
py audio_to_text_file.py "path/to/audio/folder" --accept

# Word-level alignment (opt-in)
py audio_to_text_file.py "path/to/audio/folder" --align

# Disable diarization for this run
py audio_to_text_file.py "path/to/audio/folder" --no-diarize

# Named speaker ID from a custom voices folder
py audio_to_text_file.py "path/to/audio/folder" --voices "path/to/voices"

# Auto-enroll unmatched speakers as voices/unknown-NN.wav
py audio_to_text_file.py "path/to/audio/folder" --enroll-unknown
```

**Windows console note**: the script prints `✓`/`✗`; PowerShell's default `cp1252` encoding will crash on these. Always run with `PYTHONUTF8=1` set (e.g. `$env:PYTHONUTF8=1` before invoking).

## Running the Watcher

Continuous host-side sync — see `dev-docs/watched-folder-transcription-sync.md` for the full design. Requires `whisper-api` running and reachable at `http://127.0.0.1:8000` (see `docker-compose.yml`'s `wireguard` service `ports:` mapping).

```bash
pip install -r requirements-watcher.txt      # thin client only — no torch/whisperX
cp watch-config.example.yaml watch-config.yaml   # edit paths, language, flags per root
$env:PYTHONUTF8=1
py watcher.py --dry-run   # preview what would be queued, submit nothing
py watcher.py --once      # single sync pass
py watcher.py             # continuous — the intended mode
```

Register it to start at logon (as the interactive user, **not** SYSTEM — Google Drive mounts `G:` per-user session): `scripts/register-watcher-task.ps1` (elevated PowerShell); remove with `scripts/unregister-watcher-task.ps1`.

Automatic scanning defaults to hourly (`api.poll_interval_seconds: 3600`) and can be turned off entirely via `api.enabled: false` in `watch-config.yaml` — the process keeps running (so it still answers `POST /watcher/trigger`, see below), it just never scans on a schedule. A separate `api.control_check_seconds` (default 5) governs how often it checks for a manual trigger, independent of the scan interval.

## Installing Dependencies

**Torch must be installed first, pinned to the CUDA 12.8 wheel index** — a plain `pip install -r requirements.txt` (or any bare `pip install torch`) silently pulls the CPU build from PyPI and kills GPU acceleration:

```bash
pip install "torch~=2.8.0" "torchaudio~=2.8.0" --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
```

Verify after any pip operation that could touch torch: `python -c "import torch; print(torch.cuda.is_available())"` → must print `True`.

`.env` is auto-loaded via `python-dotenv` (`load_dotenv()` at the top of `transcriber.py`) — this applies to the CLI and to `uvicorn api:app` run directly; it does **not** override real process env vars (e.g. Docker's `env_file:`), which always win.

Diarization/named speaker ID additionally require an `HF_TOKEN` (see Environment Variables below) and accepted licenses at **three** gated HF repos:
- `hf.co/pyannote/speaker-diarization-3.1`
- `hf.co/pyannote/segmentation-3.0`
- `hf.co/pyannote/speaker-diarization-community-1` — not actually used by the pipeline we run (see the PLDA gotcha under Diarization below), but `pyannote-audio` 4.x eager-loads it unconditionally and 403s without it.

Without a token, both features skip gracefully — plain transcription still works.


## Architecture

The project has three modes: **CLI** (batch transcription), **API** (HTTP service for remote transcription), and **Watcher** (continuous host-side sync agent).

### Core modules

- **`naming.py`** — Dependency-free (stdlib only: `os`, `re`, `unicodedata`) filename rules shared by every mode: `AUDIO_EXTENSIONS`, `ACCENTED_VOWEL_TRANSLATION`, `sanitize_filename`. Exists specifically so `watcher.py` can compute transcript filenames identically to the CLI/API without importing torch/whisperX.
- **`transcriber.py`** — Shared core: model loading (singleton), transcription, diarization, alignment. Imports `AUDIO_EXTENSIONS`/`sanitize_filename` from `naming.py` and re-exports them, so existing `from transcriber import sanitize_filename, AUDIO_EXTENSIONS` call sites (`audio_to_text_file.py`, `api.py`) keep working unchanged.
- **`speaker_registry.py`** — Named speaker ID: loads reference voice embeddings from a `voices/` folder and matches diarized speakers against them by cosine similarity.
- **`audio_to_text_file.py`** — CLI entry point. Batch-transcribes audio files in a directory.
- **`api.py`** — FastAPI HTTP server with async job queue. Endpoints: `/health`, `/transcribe`, `/jobs/{job_id}`, `/watcher/trigger`.
- **`security.py`** — FastAPI middleware enforcing API key (`API_KEY` env var). `/health` is exempt. IP whitelisting removed — WireGuard tunnel handles network-layer access control.
- **`watcher.py`** — Host-side watcher agent (thin client — deps in `requirements-watcher.txt`: `requests`, `pyyaml`, `python-dotenv`, no torch/whisperX). Polls one or more configured directories (`watch-config.yaml`), submits new audio to `whisper-api` over HTTP, polls the job, writes the transcript back. See "Watcher behaviors" below.

### Key behaviors

- **Skip logic** (CLI): Before transcribing, checks if a `.txt` file with the sanitized name already exists. If so, the audio file is skipped.
- **Filename sanitization** (`sanitize_filename` in `transcriber.py`): Converts to lowercase, replaces non-alphanumeric characters (except `-` and `_`) with dashes, collapses repeated dashes, strips leading/trailing dashes.
- **Model**: `whisperx.load_model(WHISPER_MODEL, compute_type="float16"|"int8", asr_options=..., vad_method="pyannote")` — whisperX wraps faster-whisper/CTranslate2 internally, so GPU behavior and quality are the same. `compute_type` is `float16` on CUDA, `int8` on CPU. Device is auto-selected (CUDA if available, else CPU). Loaded once as a singleton (a second singleton, `_retry_model`, holds retry-tuned ASR options). Requires **CUDA 12.8** + cuDNN 9 for GPU inference (needs NVIDIA driver ≥ 550.x). **FFmpeg system install IS required** — whisperX's `load_audio` shells out to the `ffmpeg` CLI via subprocess for any non-trivial container format (e.g. `.m4a`); it is not a pure-Python decoder. Confirmed live: an API job against an `.m4a` file failed with `[Errno 2] No such file or directory: 'ffmpeg'` on a container image that only installed `python3`/`python3-pip`. `Dockerfile` now installs `ffmpeg` alongside Python.
- **VAD filter**: pyannote VAD (`vad_method="pyannote"`, `min_duration_off=0.5` seconds) — strips silence/noise at the source, the main mitigation for "y y y..." hallucination loops. Note the option is in **seconds**, not ms (differs from the old faster-whisper `min_silence_duration_ms`).
- **Hallucination retry** (`transcribe_audio` in `transcriber.py`): After a first pass, segments are scanned by `_looks_like_hallucination_loop`. If a short token dominates ≥55% of windows or repeats ≥10 times in a row, a second pass runs via the `_retry_model` singleton (`temperatures=[0.0..1.0]`, `beam_size=5`, `best_of=5`, `suppress_tokens=[-1]`). If the retry still loops, `RuntimeError` is raised. This logic is backend-agnostic and survived the whisperX migration unchanged.
- **Diarization** (`transcribe_audio`, `diarize` param, default from `ENABLE_DIARIZATION` env): lazily loads `DiarizationPipeline(model_name="pyannote/speaker-diarization-3.1", token=HF_TOKEN)` (imported from `whisperx.diarize` — **not** `whisperx.DiarizationPipeline`; `whisperx/__init__.py` never re-exports the class, only lazy-wraps a few functions) and calls it with `return_embeddings=True` to get both the diarization dataframe and a `{SPEAKER_xx: embedding}` dict in one pass — no extra audio crop/embed step needed. `whisperx.assign_word_speakers` copies `speaker` onto each segment and, when word timings are present, onto each word too. Missing `HF_TOKEN` → warning logged, transcription proceeds without speaker labels (no crash).
  - **PLDA gotcha**: `pyannote-audio` 4.x's `SpeakerDiarization.__init__` unconditionally eager-loads a PLDA calibration checkpoint before checking which clustering method is configured. The `speaker-diarization-3.1` pipeline's own `config.yaml` predates PLDA and never sets that param, so it falls back to the class default — which points at a third gated repo, `pyannote/speaker-diarization-community-1`. That PLDA object is never actually used (3.1's config sets `clustering: AgglomerativeClustering`, not `VBxClustering`) but is still fetched eagerly at pipeline load time, so the license must be accepted anyway. No override path exists through `whisperx.DiarizationPipeline` or `pyannote.audio.Pipeline.from_pretrained` (no constructor-kwarg passthrough); building a workaround would mean hand-constructing `SpeakerDiarization` with a dummy `PLDA` object backed by fake `.npz` files — not worth the fragility for a one-time license click.
- **Word-level speaker attribution** (`transcriber.py`: `_align_segments`, `_split_segments_by_word_speaker`): `whisperx.assign_word_speakers` assigns speaker by dominant time-overlap over the **whole** Whisper segment, so a short interjection from speaker B inside a segment otherwise dominated by speaker A used to get swallowed into speaker A's line. Fixed by running word-level alignment **automatically whenever diarization runs** (no `--align` flag needed — `align=True` still controls only whether the `words` array is present in the returned result/JSON, not whether alignment itself runs) and splitting each segment into sub-segments at word-speaker-change boundaries after `assign_word_speakers`. Segments with a single dominant word-speaker (or no word timings) are kept verbatim — only genuinely mixed segments split, so output is additive/finer-grained, never coarser. On alignment failure (unsupported language, model load error) falls back to the old segment-level attribution with a warning, no crash — verified live by forcing a failure. First diarized run downloads the wav2vec2 alignment model (~360MB) even for users who never pass `--align`.
  - **`whisperx.align()` index-mismatch gotcha (found + fixed during live verification)**: `whisperx.align()`'s returned `segments` list is **not** 1:1 with the input segments it was given — internally it re-segments each input segment by NLTK sentence boundaries (`sentence_spans`) and flattens the result across the whole document (`aligned_segments += aligned_subsegments` per input segment, `whisperx/alignment.py:410`), so a segment with 2+ sentences produces 2+ output entries. Pairing by list index (`align_result["segments"][i]` against `segments[i]`) — the pattern the original opt-in `--align` code used — silently scrambles text and timestamps once index and segment count drift (confirmed live: produced an inverted-timestamp segment, `end < start`). `_align_segments` instead buckets `align_result["word_segments"]` (the flat, already-chronological per-word list) back onto the original segments by walking a pointer against segment `start` boundaries — safe because each word's aligned time is mathematically bounded within its source segment's own `[start, end]` window (alignment runs on that segment's own audio slice), so timestamp order alone determines the correct segment unambiguously.
- **Named speaker ID** (`speaker_registry.py`, wired into `transcribe_audio` via the `voices_dir` param): `load_registry(voices_dir)` scans the folder for audio files, embeds each with `pyannote/wespeaker-voxceleb-resnet34-LM` (the same embedding model the diarization pipeline already uses internally — no extra downloads), keyed by filename stem. `match_speakers(speaker_embeddings, registry, threshold)` then maps each diarized `SPEAKER_xx` to the best cosine-similarity match ≥ `SPEAKER_MATCH_THRESHOLD` (default `0.5`, env-tunable); below threshold keeps the anonymous label. The registry is cached per `voices_dir` (module-level dict in `transcriber.py`). Two diarized clusters mapping to the same enrolled name is expected behavior (diarization sometimes splits one person into multiple clusters).
- **Auto-enroll unknown speakers** (`speaker_registry.py` + `transcribe_audio`'s `enroll_unknown` param, default from `AUTO_ENROLL_UNKNOWN` env, off by default): after matching, any diarized speaker still unmatched gets a sample spliced from their own turns (`extract_speaker_sample` — prefers non-overlapping turns, falls back to all turns if that subset is under `ENROLL_MIN_SECONDS`; takes longest turns first, capped at `ENROLL_MAX_SECONDS`; returns `None` below the minimum, which skips that speaker and logs the reason). A sample is written via `enroll_speaker` as `voices_dir/unknown-NN.wav` (`next_unknown_name` scans the dir for the highest existing `unknown-NN` stem) and **re-embedded from the written file** (not the diarization cluster embedding) so the in-memory registry matches exactly what a future `load_registry` run would compute. The new embedding is inserted into `_speaker_registry_cache[voices_dir]` **in place**, so later files in the same CLI batch or later API jobs match it immediately, no reload needed. Within-run dedup: a second unmatched cluster is checked against embeddings enrolled earlier in the *same call* (same `SPEAKER_MATCH_THRESHOLD`) before writing another file — covers diarization splitting one person into two clusters. NaN cluster embeddings (a pyannote edge case on tiny clusters) are guarded and skipped. A failed enrollment (per speaker, try/except) never fails the transcription. `transcribe_audio` returns the newly enrolled names in `result["enrolled_speakers"]` (empty list if none). The user renames `unknown-NN.wav` to the real name by hand — that rename **is** the enrollment workflow; there is no rename UI/endpoint.
- **Alignment** (`transcribe_audio`, `align` param, opt-in, off by default): `whisperx.load_align_model` + `whisperx.align` (via the shared `_align_segments` helper) add a `words` array (per-word timings) to each segment. Cached per language code. Unsupported languages skip with a warning, no crash. Word timings are not written to the `.txt` output (kept for API/JSON consumers only). Note: when `diarize=True`, alignment runs internally regardless of this flag (see Word-level speaker attribution above) — `align` only controls whether `words` stays on the returned segments (stripped when `False`, even though alignment ran to produce the split).
- **Result shape** (`transcribe_audio`): Returns a dict `{"segments": [{"start","end","text", ...}, ...], "text": str, "language": str, "enrolled_speakers": [str, ...]}`. Each segment may additionally carry `speaker` (diarization, possibly name-mapped) and/or `words` (alignment). `enrolled_speakers` lists any `unknown-NN` names newly written this call (empty list otherwise). Callers ignoring those keys keep working — shape is additive, not breaking.
- **Output format**: Each `.txt` file begins with a header block (`===...`, `filename:...`, `===...`), followed by timestamped segments in `[start - end] text` format, or `[start - end] speaker: text` when a `speaker` key is present.
- **Supported audio formats**: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac` (same set for main audio and `voices/` reference samples)
- **GPU serialization** (API): An `asyncio.Lock` ensures only one transcription runs at a time. Additional uploads queue.
- **Job lifecycle** (API): Jobs are in-memory. Completed/failed jobs are purged after `JOB_TTL_SECONDS` (default 3600) counted from **job completion** (`finished_at`, stamped in `_process_job`'s `finally` block), not from job creation — a long-running job counts its TTL only from when it actually finishes, so it stays pollable for the full window after completion.
- **Upload size / long audio**: there is **no 25MB limit** in this project — that figure is OpenAI's *hosted* Whisper API upload cap and does not apply here (whisperX runs locally). The only cap is `MAX_UPLOAD_SIZE_MB` (default `2048`, i.e. ~2GB, env-tunable in `api.py`), sized to fit a ~2h WAV file (~1.3GB). The CLI has no size limit at all. whisperX itself handles long-form audio natively via internal VAD chunking — no manual file splitting needed for transcription. Target envelope: reliably up to **~2 hours**, full diarization + named speaker ID; see `dev-docs/long-audio-compatibility.md` for the live-verification plan and results.
- **Watcher scheduling** (`watcher.py`, `main`): the main loop is decoupled into two independent clocks — `poll_interval_seconds` (default 3600, i.e. hourly) governs the automatic scan-all-roots tick, while `control_check_seconds` (default 5) governs how often the loop wakes up to check for a manual-trigger signal file, regardless of the scan schedule. `api.enabled: false` disables only the automatic tick (`due = api_cfg["enabled"] and now >= next_scan_at`); the process keeps running and a manual trigger (or `--once`) still runs a full tick either way. A tick (auto or manual) always reschedules `next_scan_at` to `now + poll_interval_seconds`.
- **Watcher manual trigger** (`watcher.py`: `consume_trigger`/`DEFAULT_CONTROL_DIR`; `api.py`: `POST /watcher/trigger`): the container (`whisper-api`) has no way to reach a process on the host directly — that's the whole reason the watcher runs outside Docker in the first place — so the bridge is a shared directory instead of a network call. `api.py` writes `trigger.request` atomically (tmp + `os.replace`) into `WATCHER_CONTROL_DIR` (env var, bind-mounted from `./watcher-control` on the host per `docker-compose.yml`); `watcher.py` polls the same physical folder (`--control-dir`, defaults to `./watcher-control` next to the script) every `control_check_seconds` and deletes the file the moment it sees it, then runs an immediate tick. There is no ack channel back to the container — `POST /watcher/trigger` can only confirm the signal file was written, never that the watcher (a process the container can't see) actually picked it up. `/health` reports `watcher_trigger_configured: bool(WATCHER_CONTROL_DIR)` for visibility.
- **Watcher/API logging** (added same day as the trigger feature, closing a gap where a scheduled tick that found nothing produced zero log output): `run_tick` (`watcher.py`) now always logs `"Scan starting (source=%s, roots=%d)"` and `"Scan finished (source=%s): %d candidate(s) found, %d submitted"` to `watcher.log`, where `source` is `"scheduled"` (hourly timer due), `"manual"` (a trigger file was consumed — takes priority in the label even if the schedule also happened to be due that tick), or `"once"` (forced via `--once` with nothing else due). Separately, `api.py`'s `POST /watcher/trigger` writes a line to `WATCHER_CONTROL_DIR/trigger.log` (a small `RotatingFileHandler`, 1MB × 2 backups, `propagate=False` so it never mixes into uvicorn's own output) every time it's hit, in addition to its existing `print()` (visible via `docker logs`) — the file exists because `docker logs` output is easy to lose across a container restart or log-driver rotation, while this file, sitting in the already-host-visible `watcher-control` mount, isn't.
- **Watcher discovery loop** (`watcher.py`, per tick, per configured root): checks the root directory exists (logs a warning only on the unavailable→available or available→unavailable *transition*, not every tick — handles Google Drive not being mounted yet), walks it (recursive or flat per root config), filters to `naming.AUDIO_EXTENSIONS`, skips zero-byte files, and skips anything whose `naming.sanitize_filename(stem + '.txt')` already exists in that file's own directory — same rule the CLI/API use, so a file transcribed by any mode is recognized as done by every other mode.
- **Watcher stability check**: a candidate's `(size, mtime)` must be unchanged for `stability_seconds` (config, default 60s) before it's submitted — otherwise a Google Drive file still uploading/downloading would get transcribed truncated, and since the `.txt` would then exist it would never be retried. Tracked per-file in `state.json` as `stable_since`, reset whenever size or mtime changes.
- **Watcher submission + polling**: streams the file to `POST /transcribe` (never loads the whole file into memory — matters for ~2h/~1.3GB WAVs and for not needlessly holding open a hydrating Drive placeholder), persists the returned `job_id` to `state.json` **before** polling starts (so a watcher restart resumes polling instead of resubmitting), then polls `GET /jobs/{job_id}` every `job_poll_seconds`. `completed` writes `result.formatted` to the `.txt` path atomically (`tmp` + `os.replace`, same reasoning as the CLI's write — a non-atomic write lets Drive sync a half-written file); `failed` or a timeout past `job_timeout_seconds` schedules an exponential backoff retry (`backoff_base_seconds * 2^attempts`, capped at `backoff_max_seconds`, giving up after `max_attempts`); a `404` means the job was purged by the server's `JOB_TTL_SECONDS` while the watcher was down longer than that window, so the file is reset to pending and re-submitted (costs GPU time, but is correct).
- **Watcher state** (`%LOCALAPPDATA%\whisper-watcher\state.json`, deliberately outside any watched Drive folder so its own writes don't get picked up as new files): tracks only in-flight jobs and failure backoff, keyed by absolute audio path. The `.txt` file remains the sole source of truth for "done" — a completed entry is deleted from state once written, so deleting `state.json` is always safe (the watcher just re-syncs anything still missing a transcript).
- **Watcher `--dry-run`**: lists every file currently missing a transcript (and not backed off past `max_attempts`), **without** waiting for the stability window — it's a preview of the backlog, not a snapshot of what would submit on the very next tick. `--once` runs one real tick (discovery, stability, backoff, submit, poll) then exits. `--retry-failed` clears `attempts`/`next_retry_at`/`status` on every `failed` entry before the run starts.
- **Watcher submission concurrency**: fully synchronous/single-threaded by design — each submit is polled to completion before the next candidate is considered, so `max_inflight` caps how many submit-then-poll cycles happen per tick rather than providing real concurrency. This matches the server anyway, which only ever runs one job at a time behind its own `asyncio.Lock` regardless of how many clients (watcher, VPS frontend) submit concurrently.

### Environment variables

- `WHISPER_MODEL` (default `medium`), `WHISPER_BATCH_SIZE` (default `16`) — whisperX model config.
- `ENABLE_DIARIZATION` (default `true`) — server-wide diarization default; per-call `diarize` param overrides.
- `HF_TOKEN` — required for diarization and named speaker ID (pyannote model downloads). Missing → both skip gracefully.
- `VOICES_DIR` — directory of enrolled reference voices. CLI resolution order: `--voices` flag > `VOICES_DIR` env > `voices/` next to the script if it exists > disabled. API: read directly at startup, no per-request override (see Phase 2 below).
- `SPEAKER_MATCH_THRESHOLD` (default `0.5`) — cosine similarity floor for a name match; tune based on per-speaker best-score log lines.
- `AUTO_ENROLL_UNKNOWN` (default `false`) — process-wide default for auto-enrolling unmatched speakers; per-call `enroll_unknown` param (CLI `--enroll-unknown`, API `enroll_unknown` form field) overrides.
- `ENROLL_MIN_SECONDS` (default `10`) — skip enrollment for a speaker with less usable speech than this.
- `ENROLL_MAX_SECONDS` (default `30`) — cap on the written enrollment sample's length.
- `MAX_UPLOAD_SIZE_MB` (default `2048`) — API upload size cap; raised from the old 500 default to fit a ~2h WAV file. CLI has no equivalent limit.
- `JOB_TTL_SECONDS` (default `3600`) — API: how long a completed/failed job stays pollable via `GET /jobs/{id}`, counted from completion (see Job lifecycle above).
- `WATCHER_CONTROL_DIR` — API: directory shared with the host's `watcher.py` (bind-mounted from `./watcher-control`) used to signal `POST /watcher/trigger` requests. Unset → the endpoint returns `503`.

### Verification status

- **CLI**: diarization and named speaker ID are implemented and **verified live** — `SPEAKER_00`/`SPEAKER_01`/... labels confirmed on real multi-speaker audio; named ID confirmed by enrolling a sample and observing the matching `SPEAKER_xx` flip to the enrolled name while other speakers stay anonymous. Graceful degradation (no `HF_TOKEN` → plain transcription, no crash) also confirmed live.
- **API (Phase 2)**: implemented and **verified live** (2026-07-11, local `uvicorn` run — not through Docker/WireGuard): uploaded an `.m4a` via `POST /transcribe` with `diarize=true` and an enrolled `voices/` registry; the completed job's `result.formatted` carried the enrolled name on every segment (same output as the CLI run on the same file). `/health` correctly reported `diarization_enabled: true` and `speaker_id_enabled: true`; missing/wrong `X-API-Key` returned 403. `api.py` accepts optional `align`/`diarize` form fields on `/transcribe` (threaded straight into `transcribe_audio`). Named speaker ID has **no per-request field** — the registry loads once at startup from `VOICES_DIR` (`speaker_registry.load_registry`, in the FastAPI `lifespan` handler) and is passed to every `transcribe_audio` call.
- **Docker/WireGuard deployment**: wraps that same API; the container stack itself has **not been re-verified** since the diarization/named-ID features landed. `docker-compose.yml` mounts `./voices:/app/voices` and sets `VOICES_DIR=/app/voices`.
- **Auto-enroll unknown speakers**: **verified live on CLI** (2026-07-18): ran `--enroll-unknown` against a real 19-minute multi-speaker meeting recording with a partially-populated `voices/` dir (1 pre-enrolled speaker, `Carlos-Ochoa`). `Carlos-Ochoa` was correctly matched by name on his turns; the 8 other diarized speakers were auto-enrolled as `unknown-01.wav` … `unknown-08.wav` (confirmed 16kHz mono WAV, capped at 30s each) and labeled `unknown-NN` in the transcript. Two other pre-enrolled samples with no match in this recording (`Alan`, `Nikola`) correctly stayed unmatched — not overwritten, not falsely enrolled. **Not yet verified**: rename → future-run identification, the short-speech skip path, the API `enroll_unknown` round trip + `/health` flip, and graceful degradation without `HF_TOKEN`. See `dev-docs/auto-enroll-unknown-speakers.md` for the full acceptance criteria.
- **Watched-folder sync (`watcher.py`)**: implemented per `dev-docs/watched-folder-transcription-sync.md` (2026-08-03). **Verified in this session** (no live Docker/`whisper-api`/Google Drive available in the dev environment, so verification used a stand-in): `naming.py` extraction confirmed importable standalone (no torch/whisperX pulled in) and `transcriber.py`/`Dockerfile` still parse/reference it correctly; a throwaway venv with only `requirements-watcher.txt` installed ran `watcher.py` against a fixture directory with a minimal mock HTTP server standing in for `whisper-api` — confirmed: `--dry-run` correctly excludes a file with an existing `.txt` and includes it again once the `.txt` is moved aside (matches acceptance criterion re: dry-run listing), recursion into subfolders works, `--once` performs a full submit → poll → atomic `.txt` write cycle end-to-end, `enrolled_speakers` are logged, and a second `--once` run does not resubmit the already-completed file (idempotence) while correctly still submitting a separate file that genuinely still lacked a transcript. **Not yet verified**: real Google Drive placeholder-hydration behavior, the loopback port publish against a real `docker compose up`, the Scheduled Task registration script end-to-end (logon → transcription with no terminal open), and the stability-window behavior against an actually-uploading file. See the plan doc for the full acceptance criteria list.
- **Watcher hourly schedule / enable-disable / manual trigger** (2026-08-03, same-day follow-up): user found the original 30s scan interval too aggressive and asked for hourly scanning, a config toggle to disable automatic scanning without stopping the process, and a `POST /watcher/trigger` endpoint reachable over WireGuard to force a scan on demand. Implemented and **smoke-tested live** in this session (see verification pass below) using the same mock-server approach as the original watcher work — confirmed: `api.enabled: false` correctly idles the automatic tick while `--once`/a written trigger file still runs a pass; a trigger file dropped into the control dir is picked up within one `control_check_seconds` cycle and consumed (deleted) exactly once; `docker compose config` confirms `WATCHER_CONTROL_DIR`/`./watcher-control` wiring on `whisper-api`. **Not yet verified**: the actual `POST /watcher/trigger` HTTP round-trip through a running container (only the file-drop side was smoke-tested directly, not api.py's endpoint handler itself under uvicorn).

## Running the API (Docker)

The API is exposed **only through a WireGuard tunnel** — no LAN or public binding. Two containers run: a WireGuard client sidecar that dials the VPS, and `whisper-api` that shares its network namespace. The VPS frontend reaches the API via the PC's WG IP (e.g. `http://10.0.0.5:8000`).

`docker-compose.yml`'s `wireguard` service additionally publishes `127.0.0.1:8000:8000` — host-loopback only, for the local `watcher.py` (see "Running the Watcher" above). This must live on the `wireguard` service, not `whisper-api`: Docker refuses a `ports:` mapping on a service using `network_mode: "service:wireguard"` (it has no network namespace of its own). Binding to `127.0.0.1` rather than `0.0.0.0` means nothing on the LAN or internet gains access — only host processes.

```bash
# 1. Copy the example config and write your real wg0.conf
cp wireguard/wg0.conf.example wireguard/wg0.conf
# Edit wireguard/wg0.conf: fill in PrivateKey, Address, Peer PublicKey, Endpoint

# 2. Configure environment
cp .env.example .env
# Edit .env: set API_KEY, and HF_TOKEN if you want diarization/named speaker ID

# 2b. (Optional) Named speaker ID: drop reference samples into ./voices —
# docker-compose.yml mounts it to /app/voices and sets VOICES_DIR automatically.

# 3. Build and start
docker compose up --build -d

# 4. Confirm WireGuard tunnel is up
docker compose logs wireguard   # look for: wg-quick: [#] wg setconf wg0 ...
docker compose exec wireguard wg show  # expect latest handshake populated

# 5. Check health (from any WireGuard peer, e.g. the VPS)
curl http://10.0.0.5:8000/health
# -> includes "diarization_enabled" and "speaker_id_enabled"

# 6. Submit transcription
curl -X POST http://10.0.0.5:8000/transcribe \
  -H "X-API-Key: your-key" \
  -F "file=@recording.mp3" \
  -F "language=en" \
  -F "diarize=true"

# 7. Poll for result
curl http://10.0.0.5:8000/jobs/{job_id} \
  -H "X-API-Key: your-key"
```

## Running the API (without Docker)

```bash
pip install -r requirements.txt
# Set env vars or create .env file
uvicorn api:app --host 0.0.0.0 --port 8000
```

## GPU / CUDA Notes

CUDA availability is detected at runtime via `torch.cuda.is_available()`. No manual configuration needed — if a CUDA 12.8 compatible GPU is present with the correct PyTorch CUDA build, it will be used automatically.

- **torch CPU trap (critical)**: never `pip install torch` without `--index-url https://download.pytorch.org/whl/cu128` — a plain/default-index install silently replaces CUDA torch with the CPU build and kills GPU acceleration. Always install torch first, pinned to the cu128 index, before installing the rest of `requirements.txt`. Re-verify `torch.cuda.is_available()` after any pip operation that could touch torch.
- **Driver requirement**: CUDA 12.8 needs NVIDIA driver ≥ 550.x. Check with `nvidia-smi` before installing.
- **cuDNN 9 + cuBLAS** are required by CTranslate2 (whisperX's backend) for GPU inference. They are shipped via the `nvidia-cudnn-cu12==9.*` and `nvidia-cublas-cu12` pip wheels (`requirements.txt`).
- **Linux / Docker** also needs `LD_LIBRARY_PATH` set to the wheel install dirs so the loader finds the libs. The `Dockerfile` sets this; for a bare-metal Linux host you'll need to export it yourself (Windows does not need it).
- The Docker setup uses `nvidia/cuda:12.8.0-runtime-ubuntu22.04` and requires the NVIDIA Container Toolkit (or WSL2 GPU passthrough on Windows Docker Desktop).
- First run downloads ~1 GB of models (whisper medium + pyannote diarization/segmentation + wespeaker embedding, plus wav2vec2 if `--align` is used) into `~/.cache/huggingface`. Docker mounts a volume (`whisper-cache`) so this persists across container restarts.
- **Windows console + Unicode**: the script prints `✓`/`✗`; PowerShell's default `cp1252` encoding crashes on them. Always set `PYTHONUTF8=1` before running.
- The `ignore/` directory is gitignored and can be used for local test files.
