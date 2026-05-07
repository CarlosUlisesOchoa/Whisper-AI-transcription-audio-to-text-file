import asyncio
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from security import SecurityMiddleware
from transcriber import (
    AUDIO_EXTENSIONS,
    format_transcription,
    get_device,
    get_model,
    sanitize_filename,
    transcribe_audio,
)

# --- Configuration ---
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 3600))
MAX_UPLOAD_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", 500))

# --- In-memory job store ---
# {job_id: {"status": str, "result": str|None, "error": str|None, "created_at": float, "filename": str}}
jobs: dict[str, dict] = {}

# GPU lock — only one transcription at a time
gpu_lock = asyncio.Lock()

# Thread pool for running blocking whisper transcription
executor = ThreadPoolExecutor(max_workers=1)


def _get_allowed_origins() -> list[str]:
    """Return allowed CORS origins from env (comma-separated)."""
    raw_origins = os.environ.get("CORS_ALLOW_ORIGINS", "*").strip()
    if not raw_origins:
        return ["*"]

    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    return origins or ["*"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Pre-load the Whisper model on startup so the first request is fast."""
    print("Loading Whisper model on startup...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, get_model)
    device = get_device()
    print(f"Model loaded on device: {device}")
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
    """Remove jobs older than JOB_TTL_SECONDS."""
    now = time.time()
    expired = [
        jid for jid, job in jobs.items()
        if job["status"] in ("completed", "failed") and now - job["created_at"] > JOB_TTL_SECONDS
    ]
    for jid in expired:
        del jobs[jid]


def _run_transcription(file_path: str, language: str | None, original_filename: str) -> dict:
    """Run Whisper transcription (blocking — called via executor)."""
    result = transcribe_audio(file_path, language=language)
    sanitized_name = sanitize_filename(
        os.path.splitext(original_filename)[0] + ".txt"
    )
    formatted = format_transcription(sanitized_name, result["segments"])
    return {"formatted": formatted, "text": result["text"]}


async def _process_job(job_id: str, file_path: str, language: str | None, original_filename: str):
    """Acquire GPU lock, run transcription, update job status."""
    async with gpu_lock:
        jobs[job_id]["status"] = "processing"
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                executor, _run_transcription, file_path, language, original_filename
            )
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["result"] = result
        except Exception as e:
            jobs[job_id]["status"] = "failed"
            jobs[job_id]["error"] = str(e)
        finally:
            # Clean up temp file
            if os.path.exists(file_path):
                os.unlink(file_path)


# --- Endpoints ---

@app.get("/health")
async def health():
    """Health check — no auth required."""
    _cleanup_old_jobs()
    queued = sum(1 for j in jobs.values() if j["status"] in ("queued", "processing"))
    return {
        "status": "ok",
        "device": get_device(),
        "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
        "queue_depth": queued,
        "jobs_total": len(jobs),
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
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

    # Fire off background processing
    asyncio.create_task(_process_job(job_id, tmp_path, language, file.filename or "audio.txt"))

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
