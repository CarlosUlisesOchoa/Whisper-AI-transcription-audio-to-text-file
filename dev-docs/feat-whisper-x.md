# feat: swap engine from faster-whisper to whisperX

## Context

Current engine is `faster-whisper` (CTranslate2). Goal: replace with [`whisperX`](https://github.com/m-bain/whisperx) to unlock:

- **Speaker diarization** (pyannote 3.1) — adds `SPEAKER_xx` labels to segments.
- **Word-level alignment** (wav2vec2) — opt-in per request.
- **Pyannote VAD** + batched inference — stronger than current silero VAD.

whisperX wraps faster-whisper internally, so the CTranslate2 quality stays. We gain speaker labels, word timings, and batched throughput. The CLI/API contract stays the same: `transcribe_audio(file_path, language=None)` returns `{segments:[{start,end,text}], text, language}` — diarization adds a `speaker` key on each segment when enabled; words live on segments only when alignment is requested.

User decisions (locked):
- Diarization: **ON by default** (needs `HF_TOKEN` + one-time license accept on `pyannote/speaker-diarization-3.1`).
- Alignment: **opt-in flag** (CLI `--align`, API form field `align=true`). Off by default.
- Output shape: **keep current shape**; new fields layered additively.
- Hallucination retry: **keep** the existing `_looks_like_hallucination_loop` + retry pass.
- Infra: **bump to CUDA 12.8 + torch ~=2.8.0** (whisperX 3.8.5 requirement).
- Model + batch: **`medium`**, **`batch_size=16`**.

---

## Files to change

| File | Change |
|------|--------|
| `transcriber.py` | Rewrite model load + transcribe path against whisperX. Keep public API. |
| `requirements.txt` | Drop `faster-whisper`. Add `whisperx`, bump `torch`. Keep cuDNN/cuBLAS wheels. |
| `Dockerfile` | Base → `nvidia/cuda:12.8.0-runtime-ubuntu22.04`. Torch wheel index → `cu128`. |
| `.env.example` | Add `HF_TOKEN`, `ENABLE_DIARIZATION`, `WHISPER_BATCH_SIZE`, `WHISPER_MODEL`. |
| `api.py` | Accept optional `align: bool` and `diarize: bool` form fields; thread through to `transcribe_audio`. |
| `audio_to_text_file.py` | Add `--align` and `--no-diarize` CLI flags. |
| `README.md` | Update engine name, install steps, HF token instructions. |
| `CLAUDE.md` | Update architecture section. |

---

## `transcriber.py` — target shape

Reuse existing utilities verbatim: `sanitize_filename` (transcriber.py:39), `_normalize_segment_text` (transcriber.py:69), `_longest_consecutive_run` (transcriber.py:75), `_looks_like_hallucination_loop` (transcriber.py:92), `format_transcription` (transcriber.py:154). They are backend-agnostic.

Rewrite:

1. **Imports** (transcriber.py:7) — `import whisperx` instead of `from faster_whisper import WhisperModel`.

2. **Model singleton** (transcriber.py:59–66):
   ```python
   _model = None
   _align_cache: dict[str, tuple] = {}   # language_code -> (model_a, metadata)
   _diarize_pipeline = None

   def get_model():
       global _model
       if _model is None:
           device = get_device()
           compute_type = "float16" if device == "cuda" else "int8"
           _model = whisperx.load_model(
               WHISPER_MODEL,          # "medium" from env, default "medium"
               device=device,
               compute_type=compute_type,
               asr_options=ASR_OPTIONS,        # see below
               vad_method="pyannote",
               vad_options={"min_duration_off": 0.5},   # mirrors current min_silence_duration_ms=500
           )
       return _model
   ```

3. **ASR options** — port current `BASE_TRANSCRIBE_OPTIONS` into whisperX's `asr_options` (faster-whisper kwargs, passed at load time, not at transcribe time):
   ```python
   ASR_OPTIONS = {
       "temperatures": [0.0],            # whisperX uses plural form
       "compression_ratio_threshold": 2.0,
       "log_prob_threshold": -1.0,
       "no_speech_threshold": 0.45,
       "condition_on_previous_text": False,
       "suppress_tokens": [-1],
   }
   RETRY_ASR_OPTIONS = {                 # for retry path
       "temperatures": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
       "beam_size": 5,
       "best_of": 5,
   }
   ```
   whisperX `load_model` accepts `asr_options`. For the retry pass we need a second model instance with retry options OR we override `model.options` between calls. Cleanest: hold a second lazily-loaded singleton `_retry_model` with `RETRY_ASR_OPTIONS` merged in. Avoids mutating the live model.

4. **`transcribe_audio(file_path, language=None, align=False, diarize=None)`** (transcriber.py:126):
   - `diarize=None` → resolve from `ENABLE_DIARIZATION` env (default `true`).
   - `audio = whisperx.load_audio(file_path)` — replaces faster-whisper's internal PyAV path.
   - `result = model.transcribe(audio, batch_size=WHISPER_BATCH_SIZE, language=language)` — returns `{segments, language}`.
   - Materialize into `{start,end,text}` dicts (drop whisperX-only metadata to keep shape stable).
   - Run `_looks_like_hallucination_loop` on materialized segments — **unchanged signature**. On loop, retry via `_retry_model.transcribe(...)`; if still looping, raise the same `RuntimeError` text from transcriber.py:141.
   - **If `align=True` and `result["language"]` is in supported set**: load (cached) `whisperx.load_align_model(language_code=lang, device=device)` and call `whisperx.align(...)`. Copy resulting `words` onto each segment dict. If language unsupported → log warning, skip alignment (do not raise).
   - **If `diarize=True`**: lazily init `DiarizationPipeline(use_auth_token=HF_TOKEN, device=device)`. Run on `audio`, then `whisperx.assign_word_speakers(diarize_segments, result)`. Copy `speaker` onto each materialized segment. If `HF_TOKEN` missing → log warning, skip (do not raise) so the API stays usable without diarization.
   - Return `{"segments": [...], "text": " ".join(...), "language": ...}`. Each segment may now optionally carry `speaker` and/or `words`. Callers ignoring those keys keep working.

5. **`format_transcription`** (transcriber.py:154): extend the per-segment line to prefix `SPEAKER_xx` when present, no change to format otherwise:
   ```
   [12.34s - 15.78s] SPEAKER_01: text here
   ```
   When no speaker key exists, format is identical to today. Word-level timings are **not** dumped into the .txt to keep the file readable (they only live in the JSON API result if a future endpoint exposes them).

---

## `api.py` — minimal surface change

- Endpoint `POST /transcribe` (api.py:130): add two optional form fields:
  ```python
  align: bool = Form(default=False),
  diarize: bool | None = Form(default=None),  # None -> server default (env)
  ```
- Thread through `_run_transcription` → `transcribe_audio(file_path, language=language, align=align, diarize=diarize)` (api.py:84).
- `/health` (api.py:116): add `"diarization_enabled": bool(HF_TOKEN) and ENABLE_DIARIZATION`.

---

## `audio_to_text_file.py` — CLI flags

- Add `--align` (default `False`) and `--no-diarize` (default `False`, inverted to `diarize=True`).
- Pass through to `transcribe_audio`.

---

## `requirements.txt`

```diff
- faster-whisper>=1.0.3
+ whisperx==3.8.5
+ torch~=2.8.0
+ torchaudio~=2.8.0
  nvidia-cublas-cu12
- nvidia-cudnn-cu12==9.*
+ nvidia-cudnn-cu12==9.*
```

`whisperx` will pull `faster-whisper`, `ctranslate2`, `pyannote-audio`, `transformers`, `huggingface-hub`, `nltk`, `pandas`, `numpy>=2.1.0`. Keep cuDNN/cuBLAS wheels — CTranslate2 (under whisperX) still needs them.

Add `--index-url https://download.pytorch.org/whl/cu128` install note in README for host installs (or split torch into its own requirements file as today's Dockerfile already does).

---

## `Dockerfile`

```diff
- FROM nvidia/cuda:12.1.0-runtime-ubuntu22.04
+ FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04
...
- RUN pip3 install --no-cache-dir torch>=2.0.0 --index-url https://download.pytorch.org/whl/cu121
+ RUN pip3 install --no-cache-dir "torch~=2.8.0" "torchaudio~=2.8.0" --index-url https://download.pytorch.org/whl/cu128
```

`LD_LIBRARY_PATH` line stays — same cuDNN/cuBLAS wheel paths.

Host driver requirement: NVIDIA driver supporting CUDA 12.8 (≥ 550.x). Note this in README.

---

## `.env.example`

```
# WhisperX
WHISPER_MODEL=medium
WHISPER_BATCH_SIZE=16
ENABLE_DIARIZATION=true

# HuggingFace token for pyannote diarization (https://hf.co/settings/tokens)
# Required if ENABLE_DIARIZATION=true. Also accept license on:
#   https://hf.co/pyannote/speaker-diarization-3.1
#   https://hf.co/pyannote/segmentation-3.0
HF_TOKEN=
```

---

## One-time setup notes (for README)

1. Create HF account, accept license at `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`.
2. Generate read token at `https://hf.co/settings/tokens`, put in `.env` as `HF_TOKEN`.
3. First run downloads ~1 GB of models (whisper medium + wav2vec2 align + pyannote). Cache lives in `HF_HOME` / `~/.cache/huggingface`. In Docker, mount a volume to persist.

---

## Verification (end-to-end after implementation)

1. `pip install -r requirements.txt` in `.venv` — no resolver conflicts; torch reports CUDA 12.8.
2. `python -c "import torch, whisperx; print(torch.cuda.is_available())"` → `True`.
3. CLI no-diarize sanity: `py audio_to_text_file.py ignore/test --language en --no-diarize` — output `.txt` identical structurally to today's output.
4. CLI with diarization: same folder, drop `--no-diarize`. Expect `SPEAKER_00:` / `SPEAKER_01:` prefixes on segments.
5. CLI with alignment: add `--align`. JSON-dump one segment in a debug print to confirm `words` array populated with `{word,start,end}`.
6. Hallucination retry: feed an audio file known to loop on faster-whisper. Confirm retry path triggers (add a one-shot debug log behind an env flag) and either recovers or raises the same `RuntimeError`.
7. Docker: `docker compose up --build -d`, `docker compose logs whisper-api` shows `Model loaded on device: cuda`. `curl http://10.0.0.5:8000/health` returns `"diarization_enabled": true`.
8. API upload via curl with `-F align=true -F diarize=true`, poll `/jobs/{id}`, verify formatted result has speaker prefixes.
9. Run a sample without `HF_TOKEN` in env → server boots, `/health` reports `diarization_enabled: false`, transcription still works (graceful skip, no crash).

---

## Risks / open items

- **CUDA 12.8 driver**: host (Windows + WSL2 or bare Linux VPS) must have a driver new enough. Confirm before merge.
- **`numpy>=2.1.0`** via transformers/pandas: if anything else in the stack pins `numpy<2`, resolver will fight. Nothing else in current `requirements.txt` pins numpy, so should be clean.
- **whisperX VAD options** are seconds, not ms — `min_duration_off=0.5` ≠ `min_silence_duration_ms=500` exactly (whisperX semantics differ slightly). Tune after first real run.
- **Alignment language gaps**: any language outside the supported list will silently skip alignment. Log it.
- **Diarization quality on overlapping speech** is imperfect per whisperX README — set expectations in docs.
- **`without_timestamps=True`** is forced by whisperX during batched inference; segment-level timestamps come from VAD windows, not Whisper itself. Should be fine for our use, but flag if granularity feels off.
