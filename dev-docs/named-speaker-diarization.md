# Plan: Named speaker diarization (whisperX + voice enrollment)

> Date: 2026-07-10 · Slug: `named-speaker-diarization` · Engram topic: `plan/named-speaker-diarization`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

The project transcribes audio (faster-whisper, CTranslate2) via CLI and a WireGuard-only FastAPI.
The user wants more than text: **who said each phrase, by real name**.

Two layers are needed:

1. **Diarization** (anonymous `SPEAKER_00`/`SPEAKER_01` labels) — **already implemented** via a
   whisperX migration sitting in `git stash stash@{0}` on branch `feat/whisper-x`
   ("whisperx diarization migration WIP"). Deep technical plan: `feat-whisper-x.md`.
   Resume guide with exact install order and gotchas: `NEXT-MIGRATION-diarization.md`.
   It was paused because whisperX 3.8.5 requires `torch~=2.8.0` (cu128) and a careless
   `pip install` pulls CPU torch and kills GPU (happened before in this project).
2. **Named identification** (NEW, this plan's main addition) — match diarized speakers against
   enrolled reference voices so output reads `Carlos: hola` instead of `SPEAKER_00: hola`.

User decisions (locked, 2026-07-10):

- **Named identification**, not just anonymous labels.
- **Enrollment via reference audio folder**: one clean 10–30 s sample per person in `voices/`
  (e.g. `voices/carlos.wav` → name `carlos`). No post-hoc manual renaming flow.
- **CLI first, API later**: implement + verify on CLI; API/Docker wiring is phase 2 in this plan
  (code kept API-ready, verification deferred).

## Goal & outcome

Running the CLI on a multi-speaker audio file produces a transcript where each segment is
prefixed with the enrolled person's real name (`[12.3s - 15.7s] Carlos: hola`), falling back to
`SPEAKER_xx` for voices not in the registry — with GPU transcription still working.

## Scope

- **In scope**:
  - Phase 0 — resume the stashed whisperX migration and get anonymous diarization working (CLI).
  - Phase 1 — named speaker identification: `voices/` registry, embedding match, name substitution,
    CLI flag, env config, README/CLAUDE.md updates.
  - Phase 2 (spec only, implement last, verify later) — API form field + Docker volume for `voices/`.
- **Out of scope / non-goals**:
  - API/Docker end-to-end verification (deferred; CLI is the acceptance surface).
  - Post-hoc manual rename / interactive mapping UI.
  - Real-time/streaming transcription.
  - Speaker ID without diarization enabled.

## Approach

### Phase 0 — Resume whisperX diarization (prerequisite)

Follow `NEXT-MIGRATION-diarization.md` **exactly** (it exists because install order breaks GPU):

1. On `feat/whisper-x`, working tree clean → `git stash pop` (recovers whisperX versions of
   `transcriber.py`, `api.py`, `audio_to_text_file.py`, `requirements.txt`, `Dockerfile`,
   `.env.example`). On conflict: `git stash apply stash@{0}` and resolve by hand.
2. Verify NVIDIA driver supports CUDA 12.8 (`nvidia-smi`, driver ≥ 550.x) **before** installing.
3. Install torch FIRST, pinned to the cu128 index (never default PyPI for torch):
   `.venv/Scripts/python.exe -m pip install "torch~=2.8.0" "torchaudio~=2.8.0" --index-url https://download.pytorch.org/whl/cu128`
4. Then `.venv/Scripts/python.exe -m pip install whisperx==3.8.5 "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"`
   — if the resolver tries to downgrade torch to CPU, stop it (torch already pinned above).
5. HuggingFace setup: accept licenses on `hf.co/pyannote/speaker-diarization-3.1` and
   `hf.co/pyannote/segmentation-3.0`; read token into `.env` as `HF_TOKEN=`; set
   `ENABLE_DIARIZATION=true`, `WHISPER_MODEL=medium`, `WHISPER_BATCH_SIZE=16`.
6. Sanity: `.venv/Scripts/python.exe -c "import torch, whisperx; print(torch.cuda.is_available())"` → `True`.
7. CLI run with diarization on a multi-speaker file in `temp/` (set `PYTHONUTF8=1` first — the
   script prints `✓` and cp1252 explodes otherwise). Expect `SPEAKER_00:` / `SPEAKER_01:` prefixes.

### Phase 1 — Named identification (new code)

Design principle: reuse the embedding model the diarization pipeline **already downloads** —
`pyannote/wespeaker-voxceleb-resnet34-LM` (the embedding backbone inside
`pyannote/speaker-diarization-3.1`). Zero new pip dependencies: `pyannote-audio` arrives with
whisperx.

1. **New module `speaker_registry.py`**:
   - `load_registry(voices_dir: str) -> dict[str, np.ndarray]`
     - Scan `voices_dir` for `.wav .mp3 .m4a .ogg .flac` (same extension set as the transcriber).
     - Name = filename stem, used verbatim in output (user controls capitalization via filename).
     - Decode each file with `whisperx.load_audio(path)` (same decoder as the main path → 16 kHz
       float32 mono np array; avoids pyannote's soundfile I/O choking on m4a).
     - Embed with `pyannote.audio.Inference("pyannote/wespeaker-voxceleb-resnet34-LM", window="whole", use_auth_token=HF_TOKEN, device=device)` fed
       `{"waveform": torch.from_numpy(audio)[None, :], "sample_rate": 16000}`.
     - Lazy singleton for the `Inference` model (same pattern as `_model` in `transcriber.py`).
     - Missing/empty folder → return `{}` (feature silently off).
   - `match_speakers(speaker_embeddings: dict[str, np.ndarray], registry, threshold) -> dict[str, str]`
     - Cosine similarity each diarized speaker centroid vs every registry entry.
     - Best match ≥ threshold → map `SPEAKER_xx` → name. Below threshold → keep `SPEAKER_xx`.
     - Two diarized clusters may map to the same name (diarization sometimes splits one person);
       that is correct behavior, allow it.
2. **Speaker centroids for the target audio** (in `transcriber.py`, diarization step):
   - Preferred: call the pyannote pipeline with `return_embeddings=True` →
     `(annotation, embeddings)` gives one centroid per speaker directly. whisperX's
     `DiarizationPipeline.__call__` wrapper may not expose this — if so, invoke the wrapped
     pipeline (`_diarize_pipeline.model(audio_dict, return_embeddings=True)` or instantiate
     `pyannote.audio.Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")` directly) and
     convert the annotation to the DataFrame shape `whisperx.assign_word_speakers` expects
     (columns: `start`, `end`, `speaker`).
   - Fallback if `return_embeddings` proves awkward: per speaker, concatenate their longest
     segments up to ~30 s and run the same `Inference` model on the crop.
3. **Hook into `transcribe_audio`** (`transcriber.py`): after diarization assigns `speaker` keys,
   if a registry is loaded, apply the `SPEAKER_xx → name` mapping over `segment["speaker"]`.
   `format_transcription` already prefixes the speaker label, so names flow to the `.txt` with
   **no further changes**. Result shape unchanged: `{"segments": [...], "text", "language"}` with
   optional `speaker` per segment (now possibly a real name).
4. **CLI** (`audio_to_text_file.py`): add `--voices <dir>` (default: env `VOICES_DIR`, else
   `voices/` if it exists next to the script, else disabled).
5. **Env** (`.env.example`): add `VOICES_DIR=voices` and `SPEAKER_MATCH_THRESHOLD=0.5`
   (cosine; tune after first real run — see risks).
6. **Docs**: README (new feature section, `voices/` setup, HF token steps) + CLAUDE.md
   (architecture section: speaker_registry, env vars, phase-2 note). Add `voices/` to
   `.gitignore` (personal voice samples must not be committed).

### Phase 2 — API surface (implement-last, verify later)

- `api.py`: diarize/align form fields already in stashed code; named ID needs no per-request
  field — registry loads from `VOICES_DIR` at startup. Add `"speaker_id_enabled": bool(registry)`
  to `/health`.
- `docker-compose.yml`: volume `./voices:/app/voices`, env `VOICES_DIR=/app/voices`.
- Verification of this phase is **deferred** (user decision); do not block merge on it.

## Affected areas / files

- `transcriber.py` — whisperX engine (from stash) + speaker-centroid extraction + name mapping hook.
- `speaker_registry.py` — **new**: registry load, embedding, cosine matching.
- `audio_to_text_file.py` — whisperX flags from stash (`--align`, `--no-diarize`) + new `--voices`.
- `requirements.txt`, `Dockerfile` — from stash (whisperx==3.8.5, torch~=2.8.0 cu128 base image).
- `.env.example` — from stash (`HF_TOKEN`, `ENABLE_DIARIZATION`, …) + `VOICES_DIR`, `SPEAKER_MATCH_THRESHOLD`.
- `api.py`, `docker-compose.yml` — phase 2 only.
- `README.md`, `CLAUDE.md` — engine name, HF token setup, `voices/` enrollment docs.
- `.gitignore` — add `voices/`.

## Constraints & risks

- **torch CPU trap (critical)**: installing torch without `--index-url .../cu128` silently
  replaces CUDA torch and kills GPU. Install order in Phase 0 is mandatory. Verify
  `torch.cuda.is_available()` after every pip operation that could touch torch.
- **Driver**: CUDA 12.8 needs NVIDIA driver ≥ 550.x. Check `nvidia-smi` before Phase 0 step 3.
- **HF gating**: `pyannote/speaker-diarization-3.1` + `segmentation-3.0` require accepted licenses
  + `HF_TOKEN`. Without token: diarization (and therefore naming) gracefully skips — plain
  transcription must keep working (stashed code already behaves this way; preserve it).
- **Match threshold**: 0.5 cosine is a starting point for wespeaker-resnet34-LM; real-world
  same-speaker scores vary with mic/noise. Make it env-tunable, log per-speaker best scores at
  match time so tuning is data-driven.
- **Reference sample quality**: 10–30 s, one speaker only, minimal noise. Bad samples → misses or
  false matches. Document in README.
- **whisperX wrapper opacity**: `DiarizationPipeline` may hide `return_embeddings`. Fallback path
  (crop + embed) is specified above; do not let this become a blocker.
- **First run downloads ~1 GB** (whisper medium + pyannote + wav2vec2 if aligning) into
  `~/.cache/huggingface`; Docker needs a volume to persist it.
- **Windows console Unicode**: always `PYTHONUTF8=1` in PowerShell or the `✓` prints crash cp1252.
- **numpy ≥ 2.1** comes with whisperx's deps; nothing in the project pins numpy<2 today.
- **Overlapping speech**: diarization quality is imperfect on crosstalk (whisperX known
  limitation); set expectations in README.
- Existing hallucination-loop detection + retry (`_looks_like_hallucination_loop`) must survive
  the migration untouched (stashed code already keeps it).

## Acceptance criteria / verification

1. `.venv/Scripts/python.exe -c "import torch, whisperx; print(torch.cuda.is_available())"` → `True`.
2. **Anonymous diarization** (Phase 0): `PYTHONUTF8=1` + CLI on a multi-speaker file in `temp/` →
   `.txt` segments carry `SPEAKER_00:` / `SPEAKER_01:` prefixes.
3. **Named ID** (Phase 1): with `voices/carlos.wav` (etc.) present, same run → enrolled voices
   appear by name (`[12.3s - 15.7s] carlos: …`), non-enrolled voices stay `SPEAKER_xx`.
4. **Feature-off parity**: `voices/` missing or empty → output byte-identical in structure to
   plain diarization; `--no-diarize` → output structurally identical to today's text-only format.
5. **Graceful degradation**: unset `HF_TOKEN` → no crash, transcription still produces text,
   warning logged.
6. **Hallucination retry**: known-looping audio still triggers the retry path and either recovers
   or raises the same `RuntimeError`.
7. `git stash list` empty afterwards (stash consumed), branch `feat/whisper-x` holds all changes.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/named-speaker-diarization"   # find the observation
mem_get_observation <id>                      # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/named-speaker-diarization.md`. The plan is **approved** — do not
> re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/named-speaker-diarization"` then `mem_get_observation` on the top hit; if
> Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach phases in order (Phase 0 → 1 → 2), and verify
> with the Acceptance criteria before finishing. Companion docs: `feat-whisper-x.md` (deep
> whisperX migration spec) and `NEXT-MIGRATION-diarization.md` (install-order gotchas) — read
> both before touching pip.
