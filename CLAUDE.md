# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A single-script CLI tool that uses OpenAI's Whisper model to batch-transcribe audio files, with automatic GPU (CUDA) acceleration when available.

## Running the Script

```bash
# Basic usage
py audio_to_text_file.py "path/to/audio/folder"

# Specify language (default: auto-detect)
py audio_to_text_file.py "path/to/audio/folder" --language es

# Skip confirmation prompt
py audio_to_text_file.py "path/to/audio/folder" --accept
```

## Installing Dependencies

```bash
pip install -r requirements.txt
```

FFmpeg must also be installed and available on the system PATH (not a Python package).

## Architecture

The project has two modes: **CLI** (batch transcription) and **API** (HTTP service for remote transcription).

### Core modules

- **`transcriber.py`** — Shared core: model loading (singleton), transcription, filename sanitization. Used by both CLI and API.
- **`audio_to_text_file.py`** — CLI entry point. Batch-transcribes audio files in a directory.
- **`api.py`** — FastAPI HTTP server with async job queue. Endpoints: `/health`, `/transcribe`, `/jobs/{job_id}`.
- **`security.py`** — FastAPI middleware enforcing IP whitelist (`ALLOWED_IPS` env var) and API key (`API_KEY` env var). `/health` is exempt.

### Key behaviors

- **Skip logic** (CLI): Before transcribing, checks if a `.txt` file with the sanitized name already exists. If so, the audio file is skipped.
- **Filename sanitization** (`sanitize_filename` in `transcriber.py`): Converts to lowercase, replaces non-alphanumeric characters (except `-` and `_`) with dashes, collapses repeated dashes, strips leading/trailing dashes.
- **Model**: Hardcoded to `whisper.load_model("medium")`. Device is auto-selected (CUDA if available, else CPU). Loaded once as a singleton.
- **Output format**: Each `.txt` file begins with a header block (`===...`, `filename:...`, `===...`), followed by timestamped segments in `[start - end] text` format.
- **Supported audio formats**: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`
- **GPU serialization** (API): An `asyncio.Lock` ensures only one transcription runs at a time. Additional uploads queue.
- **Job lifecycle** (API): Jobs are in-memory. Completed/failed jobs are purged after `JOB_TTL_SECONDS` (default 3600).

## Running the API (Docker)

```bash
# 1. Configure environment
cp .env.example .env
# Edit .env: set API_KEY and ALLOWED_IPS

# 2. Build and start
docker compose up --build -d

# 3. Check health
curl http://localhost:8000/health

# 4. Submit transcription
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-Key: your-key" \
  -F "file=@recording.mp3" \
  -F "language=en"

# 5. Poll for result
curl http://localhost:8000/jobs/{job_id} \
  -H "X-API-Key: your-key"
```

## Running the API (without Docker)

```bash
pip install -r requirements.txt
# Set env vars or create .env file
uvicorn api:app --host 0.0.0.0 --port 8000
```

## GPU / CUDA Notes

CUDA availability is detected at runtime via `torch.cuda.is_available()`. No configuration needed — if a CUDA-compatible GPU is present with the correct PyTorch CUDA build, it will be used automatically. The Docker setup uses `nvidia/cuda:12.1.0-runtime-ubuntu22.04` and requires the NVIDIA Container Toolkit. The `ignore/` directory is gitignored and can be used for local test files.
