# Plan — Migrate from `openai-whisper` to `faster-whisper`

## Context

Current stack uses `openai-whisper==20240930` with the `medium` model on CUDA. Transcription is the main bottleneck for both CLI batch runs and the API (single GPU, serialized via `asyncio.Lock`). `faster-whisper` (CTranslate2 backend) reports up to **4x throughput** with the same accuracy and **lower VRAM**, supports **INT8 on CPU**, and removes the need for system FFmpeg (uses PyAV). Goal: drop-in replacement that keeps the public CLI/API surface identical (output filename, header format, segment line format, `/transcribe` + `/jobs/{id}` payload shape) while gaining speed and reducing the hallucination-loop incidence (better VAD).

Constraints to preserve:
- Output `.txt` format from `format_transcription()` is unchanged.
- `transcribe_audio()` return shape stays a dict (`{"text": str, "segments": [{"start","end","text"}, ...]}`) so callers in `api.py` and `audio_to_text_file.py` don't change.
- CUDA auto-detect via `torch.cuda.is_available()` keeps working.
- Hallucination-loop detection (`_looks_like_hallucination_loop`) keeps working — it consumes `segment["text"]`.

## Critical files to modify

- `transcriber.py` — model load + transcribe + options translation. Bulk of the change.
- `requirements.txt` — swap dependency.
- `Dockerfile` — keep base image; install cuDNN 9 + cuBLAS via pip wheels. Drop `ffmpeg` apt install (PyAV bundles its own).
- `api.py` — no functional change; only confirm `result["segments"]` / `result["text"]` shape preserved by `transcriber.py`.
- `audio_to_text_file.py` — no change if shape preserved.
- `CLAUDE.md` + `README.md` — update notes (model class, FFmpeg no longer required, CUDA 12 + cuDNN 9 prerequisite).

## Approach

Wrap `faster-whisper.WhisperModel` inside `transcriber.py` and **shim the result back into the existing dict shape** so nothing downstream changes.

### 1. `requirements.txt`

Replace:
```
openai-whisper==20240930
ffmpeg-python>=0.2.0
```
with:
```
faster-whisper>=1.0.3
```
Keep `torch>=2.0.0` (still used by `get_device()` for CUDA detection and by `api.py` for `torch.cuda.get_device_name`).

### 2. `transcriber.py` rewrite

Imports:
```python
from faster_whisper import WhisperModel
```
Drop `import whisper`. Keep `torch`.

Model loader — pick `compute_type` from device:
```python
def get_model():
    global _model
    if _model is None:
        device = get_device()
        compute_type = "float16" if device == "cuda" else "int8"
        _model = WhisperModel("medium", device=device, compute_type=compute_type)
    return _model
```

Translate options (`BASE_TRANSCRIBE_OPTIONS`):

| openai-whisper key            | faster-whisper key            | Notes |
|-------------------------------|-------------------------------|-------|
| `task`                        | `task`                        | same |
| `temperature`                 | `temperature`                 | scalar or tuple — same |
| `condition_on_previous_text`  | `condition_on_previous_text`  | same |
| `compression_ratio_threshold` | `compression_ratio_threshold` | same |
| `logprob_threshold`           | `log_prob_threshold`          | **renamed** |
| `no_speech_threshold`         | `no_speech_threshold`         | same |
| `fp16`                        | (drop)                        | replaced by `compute_type` at model init |
| `language`                    | `language`                    | same |
| `beam_size` / `best_of`       | `beam_size` / `best_of`       | same |
| `suppress_tokens`             | `suppress_tokens`             | accepts list of int; `""` → `[-1]` (default suppress) |

Add new defaults (confirmed):
- `vad_filter=True` — Silero VAD strips silence/noise at the source. Cleanest fix for the "y y y…" hallucination loop. **GPU note**: Silero VAD itself runs on **CPU** inside faster-whisper (tiny ONNX model, ~negligible cost) — it does **not** steal GPU cycles from Whisper decoding. The main Whisper model still runs fully on CUDA.
- `vad_parameters={"min_silence_duration_ms": 500}` — moderate threshold.

`_build_transcribe_options()` returns a dict for `model.transcribe()` (no `fp16` key, `log_prob_threshold` instead of `logprob_threshold`, `suppress_tokens=[-1]` instead of `""` on retry).

Rewrite `transcribe_audio()` — materialize generator into the legacy dict shape:
```python
def transcribe_audio(file_path, language=None):
    model = get_model()
    segments_iter, info = model.transcribe(file_path, **_build_transcribe_options(language=language, retry=False))
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segments_iter]
    result = {
        "segments": segments,
        "text": " ".join(s["text"].strip() for s in segments).strip(),
        "language": info.language,
    }

    if _looks_like_hallucination_loop(result["segments"]):
        retry_iter, retry_info = model.transcribe(file_path, **_build_transcribe_options(language=language, retry=True))
        retry_segments = [{"start": s.start, "end": s.end, "text": s.text} for s in retry_iter]
        if _looks_like_hallucination_loop(retry_segments):
            raise RuntimeError("Whisper detected a repetition loop ...")
        return {
            "segments": retry_segments,
            "text": " ".join(s["text"].strip() for s in retry_segments).strip(),
            "language": retry_info.language,
        }
    return result
```

**Key gotcha**: `model.transcribe()` returns a **lazy generator**. Iterating is what triggers actual decoding. List-comprehending the segments is mandatory before passing them to `_looks_like_hallucination_loop` (which calls `len()` and indexes).

`format_transcription`, `sanitize_filename`, `get_device`, `_normalize_segment_text`, `_longest_consecutive_run`, `_looks_like_hallucination_loop` — **unchanged**.

### 3. `Dockerfile`

CTranslate2 + CUDA 12 needs **cuDNN 9** runtime libs. Current base `nvidia/cuda:12.1.0-runtime-ubuntu22.04` does **not** include cuDNN. **Decision: keep base image, ship cuDNN 9 via pip wheels** (smaller diff, no base image churn):
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# faster-whisper requires cuDNN 9 + cuBLAS for CUDA 12 — install from pip:
RUN pip3 install --no-cache-dir nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*
```
`nvidia-cudnn-cu12` wheel ships the libs; CTranslate2 picks them up via `LD_LIBRARY_PATH`. Set:
```dockerfile
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib
```
(Verify the actual path with `python3 -c "import nvidia.cudnn, os; print(os.path.dirname(nvidia.cudnn.__file__))"` during build.)

Drop `ffmpeg` from `apt-get install` — PyAV (faster-whisper dep) handles audio decoding.

Keep the `pip install torch ... --index-url cu121` line — `api.py:124` still uses `torch.cuda.get_device_name`.

### 4. `docker-compose.yml`

No change required — GPU reservation + WireGuard sidecar topology untouched. The `whisper-cache` volume still works (`XDG_CACHE_HOME=/cache` covers HuggingFace cache where faster-whisper downloads CTranslate2-converted weights from `Systran/faster-whisper-medium`).

### 5. `api.py`

If `transcriber.transcribe_audio()` returns the same dict shape, `api.py` is **untouched**. Verify `_run_transcription()` still reads `result["segments"]` and `result["text"]` correctly.

### 6. Docs

- `CLAUDE.md` — under "Architecture › Key behaviors": replace `whisper.load_model("medium")` with `WhisperModel("medium", compute_type=…)`. Note FFmpeg system install is **no longer required** (PyAV bundled). Note CUDA 12 + cuDNN 9 prerequisite.
- `README.md` — same edits.
- Drop FFmpeg mention from "Installing Dependencies" section.

## Out of scope (deliberately not doing)

- `BatchedInferencePipeline` — biggest perf win (17s vs 1m03s benchmark) but changes concurrency model; the `gpu_lock` + 1-worker `ThreadPoolExecutor` becomes wrong. Worth a follow-up issue, not bundled with the migration.
- `word_timestamps=True` — not in current output format; not needed.
- Switching model size (e.g. `large-v3`) — orthogonal. **Confirmed: keep `medium`** for this migration to isolate output-parity validation from quality drift.

## Verification

End-to-end checks (run on the host):

1. **Local Python** (no Docker):
   ```powershell
   pip install -r requirements.txt
   py .\audio_to_text_file.py ".\ignore" --accept --language es
   ```
   - Confirm console prints `CUDA is available. Using GPU: ...`.
   - Confirm `.txt` file is created with the **same header format** (`====`, `filename:`, `====`, `[start - end] text` lines).
   - Time vs previous run — expect ~3-4x faster on GPU.

2. **Hallucination retry path**:
   - Reuse the audio file that originally triggered the "y y y…" loop — confirm `vad_filter=True` prevents the loop in the first pass; if the retry path fires, confirm it still raises the user-facing `RuntimeError`.

3. **Docker build + WireGuard API**:
   ```powershell
   docker compose build whisper-api
   docker compose up -d
   docker compose logs whisper-api   # expect "Model loaded on device: cuda"
   docker compose exec whisper-api python3 -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cuda', compute_type='float16'); print('cuda OK')"
   ```
   If `cuda OK` fails, the cuDNN 9 path in `LD_LIBRARY_PATH` is wrong — fix env var.

4. **API smoke test** (from VPS / WG peer):
   ```bash
   curl http://10.0.0.5:8000/health
   curl -X POST http://10.0.0.5:8000/transcribe \
     -H "X-API-Key: $API_KEY" -F "file=@sample.mp3" -F "language=es"
   curl http://10.0.0.5:8000/jobs/{job_id} -H "X-API-Key: $API_KEY"
   ```
   - `/health` returns `device: "cuda"`, `gpu_name` populated.
   - `/jobs/{id}` payload shape unchanged (`status`, `result.formatted`, `result.text`).

## Rollback

`git revert` the migration commit. The original `openai-whisper` requirement and FFmpeg apt install restore the previous stack. Model cache volume (`whisper-cache`) does not need clearing — old `.pt` weights coexist with new CTranslate2 dir.
