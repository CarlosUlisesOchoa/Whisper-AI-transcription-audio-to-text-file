import asyncio
import functools
import logging
import logging.handlers
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import speaker_registry
import transcriber
from security import SecurityMiddleware
from transcriber import (
    AUDIO_EXTENSIONS,
    ENABLE_DIARIZATION,
    HF_TOKEN,
    format_transcription,
    get_device,
    get_model,
    sanitize_filename,
    transcribe_audio,
)

# --- Configuration ---
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 3600))
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", 2048))
VOICES_DIR = os.environ.get("VOICES_DIR")
# Bind-mounted from ./watcher-control on the host — see docker-compose.yml. The host-side
# watcher.py process (outside Docker; it needs direct access to the Google Drive mount) polls
# this same directory for a trigger file, since the container has no way to reach a host process.
WATCHER_CONTROL_DIR = os.environ.get("WATCHER_CONTROL_DIR")
WATCHER_TRIGGER_FILENAME = "trigger.request"

# Persistent record of trigger requests, written into the same host-visible directory —
# `docker logs` output is easy to lose (container restart, log-driver rotation); this file isn't.
_watcher_trigger_logger = None


def _get_watcher_trigger_logger():
    global _watcher_trigger_logger
    if _watcher_trigger_logger is None and WATCHER_CONTROL_DIR:
        os.makedirs(WATCHER_CONTROL_DIR, exist_ok=True)
        log = logging.getLogger("watcher_trigger")
        log.setLevel(logging.INFO)
        log.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            os.path.join(WATCHER_CONTROL_DIR, "trigger.log"), maxBytes=1 * 1024 * 1024, backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        _watcher_trigger_logger = log
    return _watcher_trigger_logger

# Set at startup once the registry is preloaded (see lifespan).
_speaker_id_enabled = False

# --- In-memory job store ---
# {job_id: {"status": str, "result": str|None, "error": str|None, "created_at": float, "filename": str}}
jobs: dict[str, dict] = {}

# GPU lock — only one transcription at a time
gpu_lock = asyncio.Lock()

# Thread pool for running blocking whisper transcription
executor = ThreadPoolExecutor(max_workers=1)


def _probe_duration_seconds(file_path: str) -> float | None:
    """Best-effort audio duration via ffprobe, for observability on long jobs. None on any failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def _get_allowed_origins() -> list[str]:
    """Return allowed CORS origins from env (comma-separated)."""
    raw_origins = os.environ.get("CORS_ALLOW_ORIGINS", "*").strip()
    if not raw_origins:
        return ["*"]

    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load the Whisper model (and voice registry, if configured) on startup."""
    global _speaker_id_enabled
    print("Loading Whisper model on startup...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, get_model)
    device = get_device()
    print(f"Model loaded on device: {device}")
    if VOICES_DIR:
        registry = await loop.run_in_executor(executor, speaker_registry.load_registry, VOICES_DIR)
        _speaker_id_enabled = bool(registry)
        print(f"Speaker registry: {len(registry)} enrolled voice(s)" if registry else "Speaker registry: none found")
    yield
    executor.shutdown(wait=False)


app = FastAPI(title="Whisper Transcription API", lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _cleanup_old_jobs():
    """Remove jobs older than JOB_TTL_SECONDS, counted from completion (not creation)."""
    now = time.time()
    expired = [
        jid for jid, job in jobs.items()
        if job["status"] in ("completed", "failed")
        and now - job.get("finished_at", job["created_at"]) > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del jobs[jid]


def _run_transcription(
    file_path: str,
    language: str | None,
    original_filename: str,
    align: bool = False,
    diarize: bool | None = None,
    enroll_unknown: bool | None = None,
) -> dict:
    """Run Whisper transcription (blocking — called via executor)."""
    result = transcribe_audio(
        file_path, language=language, align=align, diarize=diarize,
        voices_dir=VOICES_DIR, enroll_unknown=enroll_unknown,
    )
    sanitized_name = sanitize_filename(
        os.path.splitext(original_filename)[0] + ".txt"
    )
    formatted = format_transcription(sanitized_name, result["segments"])
    return {"formatted": formatted, "text": result["text"], "enrolled_speakers": result.get("enrolled_speakers", [])}


async def _process_job(
    job_id: str,
    file_path: str,
    language: str | None,
    original_filename: str,
    align: bool = False,
    diarize: bool | None = None,
    enroll_unknown: bool | None = None,
):
    """Acquire GPU lock, run transcription, update job status."""
    async with gpu_lock:
        jobs[job_id]["status"] = "processing"
        loop = asyncio.get_event_loop()
        try:
            fn = functools.partial(
                _run_transcription, file_path, language, original_filename,
                align=align, diarize=diarize, enroll_unknown=enroll_unknown,
            )
            result = await loop.run_in_executor(executor, fn)
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["result"] = result
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
        finally:
            jobs[job_id]["finished_at"] = time.time()
            # Clean up temp file
            if os.path.exists(file_path):
                os.unlink(file_path)


# --- Endpoints ---

@app.get("/health")
async def health():
    """Health check — no auth required."""
    _cleanup_old_jobs()
    queued = sum(1 for j in jobs.values() if j["status"] in ("queued", "processing"))
    cached_registry = transcriber._speaker_registry_cache.get(VOICES_DIR)
    speaker_id_enabled = bool(cached_registry) if cached_registry is not None else _speaker_id_enabled
    return {
        "status": "ok",
        "device": get_device(),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "queue_depth": queued,
        "jobs_total": len(jobs),
        "diarization_enabled": bool(HF_TOKEN) and ENABLE_DIARIZATION,
        "speaker_id_enabled": speaker_id_enabled,
        "watcher_trigger_configured": bool(WATCHER_CONTROL_DIR),
    }


@app.post("/watcher/trigger")
async def trigger_watcher(request: Request):
    """Signal the host-side watcher (watcher.py) to run a sync pass now.

    The container has no way to reach a process on the host directly (that's the whole reason
    the watcher runs outside Docker — it needs the Google Drive mount), so this writes a signal
    file into a directory shared with the host via a bind mount (WATCHER_CONTROL_DIR). The
    watcher checks for it every `control_check_seconds` (default 5s) if it happens to be running.
    This endpoint cannot confirm the watcher is actually running or that it picked up the signal.
    """
    if not WATCHER_CONTROL_DIR:
        return JSONResponse(
            status_code=503,
            content={"detail": "WATCHER_CONTROL_DIR not configured on this server — watcher trigger unavailable."},
        )

    try:
        os.makedirs(WATCHER_CONTROL_DIR, exist_ok=True)
        trigger_path = os.path.join(WATCHER_CONTROL_DIR, WATCHER_TRIGGER_FILENAME)
        tmp_path = trigger_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
        os.replace(tmp_path, trigger_path)
    except OSError as e:
        return JSONResponse(status_code=500, content={"detail": f"Failed to write trigger signal: {e}"})

    client_host = request.client.host if request.client else "unknown"
    trigger_log = _get_watcher_trigger_logger()
    if trigger_log:
        trigger_log.info("Trigger requested from %s", client_host)
    print(f"Watcher trigger requested from {client_host}")

    return {
        "status": "triggered",
        "note": (
            "Signal written. The host watcher picks it up within a few seconds if it is running — "
            "this endpoint has no visibility into the host process, so it cannot confirm the scan "
            "actually started. Check the host's watcher.log, or watch for new .txt files."
        ),
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    align: bool = Form(default=False),
    diarize: bool | None = Form(default=None),
    enroll_unknown: bool | None = Form(default=None),
):
    """Upload an audio file for transcription. Returns a job_id to poll."""
    # Validate file extension
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in AUDIO_EXTENSIONS:
        return JSONResponse(
            status_code=400,
            content={
                "detail": f"Unsupported file type '{ext}'. Supported: {', '.join(sorted(AUDIO_EXTENSIONS))}"
            },
        )

    # Save upload to a temp file (streaming to avoid loading entire file into memory)
    tmp_dir = tempfile.mkdtemp(prefix="whisper_")
    tmp_path = os.path.join(tmp_dir, file.filename or "upload" + ext)
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return JSONResponse(status_code=500, content={"detail": f"Failed to save upload: {e}"})

    # Check file size
    file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
    if file_size_mb > MAX_UPLOAD_SIZE_MB:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return JSONResponse(
            status_code=413,
            content={"detail": f"File too large ({file_size_mb:.1f}MB). Max: {MAX_UPLOAD_SIZE_MB}MB"},
        )

    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "result": None,
        "error": None,
        "created_at": time.time(),
        "filename": file.filename,
    }

    duration_sec = _probe_duration_seconds(tmp_path)
    duration_note = f", {duration_sec / 60:.1f} min" if duration_sec is not None else ""
    print(f"Job {job_id} queued: {file.filename} ({file_size_mb:.1f}MB{duration_note})")

    # Fire off background processing
    asyncio.create_task(_process_job(
        job_id, tmp_path, language, file.filename or "audio.txt",
        align=align, diarize=diarize, enroll_unknown=enroll_unknown,
    ))

    _cleanup_old_jobs()

    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Check the status of a transcription job."""
    if job_id not in jobs:
        return JSONResponse(status_code=404, content={"detail": "Job not found"})

    job = jobs[job_id]
    response = {
        "job_id": job_id,
        "status": job["status"],
        "filename": job["filename"],
    }

    if job["status"] == "completed":
        response["result"] = job["result"]
    elif job["status"] == "failed":
        response["error"] = job["error"]

    # Show queue position for queued jobs
    if job["status"] == "queued":
        queued_jobs = [
            jid for jid, j in jobs.items()
            if j["status"] == "queued" and j["created_at"] <= job["created_at"]
        ]
        response["queue_position"] = len(queued_jobs)

    return response
