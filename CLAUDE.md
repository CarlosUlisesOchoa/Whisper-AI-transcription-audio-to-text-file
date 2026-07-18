# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python tool that uses `whisperX` (CTranslate2 backend under the hood) to transcribe audio files, with automatic GPU (CUDA) acceleration when available. Adds **speaker diarization** (pyannote 3.1) and **named speaker identification** (voice enrollment) on top of transcription. Two modes: **CLI** for batch transcription of a folder, and **API** (FastAPI + async job queue) for remote transcription over a WireGuard tunnel.

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
```

**Windows console note**: the script prints `✓`/`✗`; PowerShell's default `cp1252` encoding will crash on these. Always run with `PYTHONUTF8=1` set (e.g. `$env:PYTHONUTF8=1` before invoking).

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

The project has two modes: **CLI** (batch transcription) and **API** (HTTP service for remote transcription).

### Core modules

- **`transcriber.py`** — Shared core: model loading (singleton), transcription, diarization, alignment, filename sanitization. Used by both CLI and API.
- **`speaker_registry.py`** — Named speaker ID: loads reference voice embeddings from a `voices/` folder and matches diarized speakers against them by cosine similarity.
- **`audio_to_text_file.py`** — CLI entry point. Batch-transcribes audio files in a directory.
- **`api.py`** — FastAPI HTTP server with async job queue. Endpoints: `/health`, `/transcribe`, `/jobs/{job_id}`.
- **`security.py`** — FastAPI middleware enforcing API key (`API_KEY` env var). `/health` is exempt. IP whitelisting removed — WireGuard tunnel handles network-layer access control.

### Key behaviors

- **Skip logic** (CLI): Before transcribing, checks if a `.txt` file with the sanitized name already exists. If so, the audio file is skipped.
- **Filename sanitization** (`sanitize_filename` in `transcriber.py`): Converts to lowercase, replaces non-alphanumeric characters (except `-` and `_`) with dashes, collapses repeated dashes, strips leading/trailing dashes.
- **Model**: `whisperx.load_model(WHISPER_MODEL, compute_type="float16"|"int8", asr_options=..., vad_method="pyannote")` — whisperX wraps faster-whisper/CTranslate2 internally, so GPU behavior and quality are the same. `compute_type` is `float16` on CUDA, `int8` on CPU. Device is auto-selected (CUDA if available, else CPU). Loaded once as a singleton (a second singleton, `_retry_model`, holds retry-tuned ASR options). Requires **CUDA 12.8** + cuDNN 9 for GPU inference (needs NVIDIA driver ≥ 550.x). **FFmpeg system install IS required** — whisperX's `load_audio` shells out to the `ffmpeg` CLI via subprocess for any non-trivial container format (e.g. `.m4a`); it is not a pure-Python decoder. Confirmed live: an API job against an `.m4a` file failed with `[Errno 2] No such file or directory: 'ffmpeg'` on a container image that only installed `python3`/`python3-pip`. `Dockerfile` now installs `ffmpeg` alongside Python.
- **VAD filter**: pyannote VAD (`vad_method="pyannote"`, `min_duration_off=0.5` seconds) — strips silence/noise at the source, the main mitigation for "y y y..." hallucination loops. Note the option is in **seconds**, not ms (differs from the old faster-whisper `min_silence_duration_ms`).
- **Hallucination retry** (`transcribe_audio` in `transcriber.py`): After a first pass, segments are scanned by `_looks_like_hallucination_loop`. If a short token dominates ≥55% of windows or repeats ≥10 times in a row, a second pass runs via the `_retry_model` singleton (`temperatures=[0.0..1.0]`, `beam_size=5`, `best_of=5`, `suppress_tokens=[-1]`). If the retry still loops, `RuntimeError` is raised. This logic is backend-agnostic and survived the whisperX migration unchanged.
- **Diarization** (`transcribe_audio`, `diarize` param, default from `ENABLE_DIARIZATION` env): lazily loads `DiarizationPipeline(model_name="pyannote/speaker-diarization-3.1", token=HF_TOKEN)` (imported from `whisperx.diarize` — **not** `whisperx.DiarizationPipeline`; `whisperx/__init__.py` never re-exports the class, only lazy-wraps a few functions) and calls it with `return_embeddings=True` to get both the diarization dataframe and a `{SPEAKER_xx: embedding}` dict in one pass — no extra audio crop/embed step needed. `whisperx.assign_word_speakers` copies `speaker` onto each segment. Missing `HF_TOKEN` → warning logged, transcription proceeds without speaker labels (no crash).
  - **PLDA gotcha**: `pyannote-audio` 4.x's `SpeakerDiarization.__init__` unconditionally eager-loads a PLDA calibration checkpoint before checking which clustering method is configured. The `speaker-diarization-3.1` pipeline's own `config.yaml` predates PLDA and never sets that param, so it falls back to the class default — which points at a third gated repo, `pyannote/speaker-diarization-community-1`. That PLDA object is never actually used (3.1's config sets `clustering: AgglomerativeClustering`, not `VBxClustering`) but is still fetched eagerly at pipeline load time, so the license must be accepted anyway. No override path exists through `whisperx.DiarizationPipeline` or `pyannote.audio.Pipeline.from_pretrained` (no constructor-kwarg passthrough); building a workaround would mean hand-constructing `SpeakerDiarization` with a dummy `PLDA` object backed by fake `.npz` files — not worth the fragility for a one-time license click.
- **Named speaker ID** (`speaker_registry.py`, wired into `transcribe_audio` via the `voices_dir` param): `load_registry(voices_dir)` scans the folder for audio files, embeds each with `pyannote/wespeaker-voxceleb-resnet34-LM` (the same embedding model the diarization pipeline already uses internally — no extra downloads), keyed by filename stem. `match_speakers(speaker_embeddings, registry, threshold)` then maps each diarized `SPEAKER_xx` to the best cosine-similarity match ≥ `SPEAKER_MATCH_THRESHOLD` (default `0.5`, env-tunable); below threshold keeps the anonymous label. The registry is cached per `voices_dir` (module-level dict in `transcriber.py`). Two diarized clusters mapping to the same enrolled name is expected behavior (diarization sometimes splits one person into multiple clusters).
- **Alignment** (`transcribe_audio`, `align` param, opt-in, off by default): `whisperx.load_align_model` + `whisperx.align` add a `words` array (per-word timings) to each segment. Cached per language code. Unsupported languages skip with a warning, no crash. Word timings are not written to the `.txt` output (kept for API/JSON consumers only).
- **Result shape** (`transcribe_audio`): Returns a dict `{"segments": [{"start","end","text", ...}, ...], "text": str, "language": str}`. Each segment may additionally carry `speaker` (diarization, possibly name-mapped) and/or `words` (alignment). Callers ignoring those keys keep working — shape is additive, not breaking.
- **Output format**: Each `.txt` file begins with a header block (`===...`, `filename:...`, `===...`), followed by timestamped segments in `[start - end] text` format, or `[start - end] speaker: text` when a `speaker` key is present.
- **Supported audio formats**: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac` (same set for main audio and `voices/` reference samples)
- **GPU serialization** (API): An `asyncio.Lock` ensures only one transcription runs at a time. Additional uploads queue.
- **Job lifecycle** (API): Jobs are in-memory. Completed/failed jobs are purged after `JOB_TTL_SECONDS` (default 3600) counted from **job completion** (`finished_at`, stamped in `_process_job`'s `finally` block), not from job creation — a long-running job counts its TTL only from when it actually finishes, so it stays pollable for the full window after completion.
- **Upload size / long audio**: there is **no 25MB limit** in this project — that figure is OpenAI's *hosted* Whisper API upload cap and does not apply here (whisperX runs locally). The only cap is `MAX_UPLOAD_SIZE_MB` (default `2048`, i.e. ~2GB, env-tunable in `api.py`), sized to fit a ~2h WAV file (~1.3GB). The CLI has no size limit at all. whisperX itself handles long-form audio natively via internal VAD chunking — no manual file splitting needed for transcription. Target envelope: reliably up to **~2 hours**, full diarization + named speaker ID; see `dev-docs/long-audio-compatibility.md` for the live-verification plan and results.

### Environment variables

- `WHISPER_MODEL` (default `medium`), `WHISPER_BATCH_SIZE` (default `16`) — whisperX model config.
- `ENABLE_DIARIZATION` (default `true`) — server-wide diarization default; per-call `diarize` param overrides.
- `HF_TOKEN` — required for diarization and named speaker ID (pyannote model downloads). Missing → both skip gracefully.
- `VOICES_DIR` — directory of enrolled reference voices. CLI resolution order: `--voices` flag > `VOICES_DIR` env > `voices/` next to the script if it exists > disabled. API: read directly at startup, no per-request override (see Phase 2 below).
- `SPEAKER_MATCH_THRESHOLD` (default `0.5`) — cosine similarity floor for a name match; tune based on per-speaker best-score log lines.
- `MAX_UPLOAD_SIZE_MB` (default `2048`) — API upload size cap; raised from the old 500 default to fit a ~2h WAV file. CLI has no equivalent limit.
- `JOB_TTL_SECONDS` (default `3600`) — API: how long a completed/failed job stays pollable via `GET /jobs/{id}`, counted from completion (see Job lifecycle above).

### Verification status

- **CLI**: diarization and named speaker ID are implemented and **verified live** — `SPEAKER_00`/`SPEAKER_01`/... labels confirmed on real multi-speaker audio; named ID confirmed by enrolling a sample and observing the matching `SPEAKER_xx` flip to the enrolled name while other speakers stay anonymous. Graceful degradation (no `HF_TOKEN` → plain transcription, no crash) also confirmed live.
- **API (Phase 2)**: implemented and **verified live** (2026-07-11, local `uvicorn` run — not through Docker/WireGuard): uploaded an `.m4a` via `POST /transcribe` with `diarize=true` and an enrolled `voices/` registry; the completed job's `result.formatted` carried the enrolled name on every segment (same output as the CLI run on the same file). `/health` correctly reported `diarization_enabled: true` and `speaker_id_enabled: true`; missing/wrong `X-API-Key` returned 403. `api.py` accepts optional `align`/`diarize` form fields on `/transcribe` (threaded straight into `transcribe_audio`). Named speaker ID has **no per-request field** — the registry loads once at startup from `VOICES_DIR` (`speaker_registry.load_registry`, in the FastAPI `lifespan` handler) and is passed to every `transcribe_audio` call.
- **Docker/WireGuard deployment**: wraps that same API; the container stack itself has **not been re-verified** since the diarization/named-ID features landed. `docker-compose.yml` mounts `./voices:/app/voices` and sets `VOICES_DIR=/app/voices`.

## Running the API (Docker)

The API is exposed **only through a WireGuard tunnel** — no LAN or public binding. Two containers run: a WireGuard client sidecar that dials the VPS, and `whisper-api` that shares its network namespace. The VPS frontend reaches the API via the PC's WG IP (e.g. `http://10.0.0.5:8000`).

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
