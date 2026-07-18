# Plan: Long-audio compatibility (up to ~2 hours, CLI + API)

> Date: 2026-07-17 · Slug: `long-audio-compatibility` · Engram topic: `plan/long-audio-compatibility`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

The user believed a **25MB file size limit** applied to Whisper transcription. Investigation
(2026-07-16/17 planning session) **confirmed this is false for this project**:

- 25MB is the upload cap of **OpenAI's hosted Whisper API** (`api.openai.com`). We run
  **whisperX locally** — no such limit exists here.
- whisperX was *designed* for long-form audio: it VAD-chunks internally into ~30s windows and
  batch-transcribes them. Manual file splitting is **redundant for transcription itself**.
- The only hard cap in this codebase is self-imposed: `MAX_UPLOAD_SIZE_MB` in `api.py:31`
  (default **500MB**, env-tunable). The CLI has **no size limit at all**.

Actual constraints for long files found during investigation:

| Concern | Reality at 2h |
|---|---|
| Waveform RAM | ~230MB per audio-hour (float32 @ 16kHz) → ~460MB for 2h. Fine. |
| File size | 2h mp3/m4a ≈ 170–230MB (fits 500 default). 2h WAV ≈ 1.3GB (**exceeds** 500 default). |
| Transcription | whisperX handles natively via internal VAD chunking. |
| Diarization (pyannote 3.1) | The real risk: memory/time grow with duration. 2h expected to work on GPU but **unverified live** — must be load-tested. |
| Hallucination retry | `transcriber.py:206` re-transcribes the whole file on loop detection → doubles wall time worst-case. Acceptable; no change planned. |
| **Job TTL bug (found during planning)** | `api.py:86-94`: `_cleanup_old_jobs` purges completed jobs when `now - created_at > JOB_TTL_SECONDS` — TTL counts from **creation**, not completion. A job queued 30min + processed 90min is purged the instant it completes → client polls, gets 404, **result lost**. Must fix for long jobs. |

Strategy chosen: **verify-first, chunk-as-fallback**. Do NOT build a chunking pipeline
speculatively — config fixes + live 2h verification first; build the chunked pipeline (Phase 3)
only if verification fails (OOM / crash / unusable wall time).

## Goal & outcome

Both CLI and API reliably transcribe audio files up to ~2 hours **with full diarization and
named speaker ID**, delivering the transcript as **one consolidated `.txt` file** (any splitting,
if ever needed, uses temporary files only and is invisible in the output).

## Scope

- **In scope**:
  - CLI (`audio_to_text_file.py`) and API (`api.py`) long-file support up to ~2h.
  - Full feature set on long files: diarization + named speaker ID + optional alignment.
  - API upload-size and job-lifecycle fixes needed for long jobs.
  - Live verification with a real ~2h multi-speaker recording.
  - Chunked-processing fallback design (implemented only if verification demands it).
- **Out of scope / non-goals**:
  - Guarantees beyond ~2h (10h/unbounded = future work; Phase 3 design would extend naturally).
  - Streaming / real-time transcription.
  - Resumable/chunked HTTP uploads.
  - Persistent (non-in-memory) job store.

## Approach

### Phase 1 — Config + hardening (no new pipeline code)

1. **Fix the job TTL bug** (`api.py`): stamp `finished_at` on the job dict when status flips to
   `completed`/`failed` (in `_process_job`), and make `_cleanup_old_jobs` purge on
   `now - finished_at > JOB_TTL_SECONDS` instead of `created_at`. Keep `created_at` for queue
   position.
2. **Raise `MAX_UPLOAD_SIZE_MB` default** from 500 to **2048** (`api.py:31`) so a 2h WAV
   (~1.3GB) passes. Keep env-tunable. Update `.env.example` and `docker-compose.yml` comments.
3. **(Optional, cheap)** Log estimated duration at job start (ffprobe already available —
   whisperX requires system ffmpeg) so long jobs are observable in logs.
4. Update docs (`CLAUDE.md`, `README.md`): state explicitly there is **no 25MB limit** (that is
   OpenAI's cloud API), document the real knobs (`MAX_UPLOAD_SIZE_MB`, `JOB_TTL_SECONDS`) and
   the ~2h verified envelope.

### Phase 2 — Live verification (gate for Phase 3)

5. Obtain a real ~2h multi-speaker recording (user supplies; fallback: concatenate existing
   test audio with ffmpeg to ~2h). Place in `ignore/` (gitignored).
6. **CLI run**: `py audio_to_text_file.py "ignore/<folder>" --accept` with `HF_TOKEN` set and an
   enrolled `voices/` registry. `$env:PYTHONUTF8=1` first (Windows console gotcha). Record:
   peak RAM, peak VRAM (`nvidia-smi`), wall time, output correctness.
7. **API run**: same file via `POST /transcribe` with `diarize=true`; poll `/jobs/{id}` until
   completed; confirm result retrievable **after** completion (validates TTL fix) and no 413.
8. **Decision gate**: if both runs succeed → done, skip Phase 3, document measured limits.
   If diarization OOMs or wall time is unusable → implement Phase 3.

### Phase 3 — Chunked fallback (ONLY if Phase 2 fails)

9. New module `long_audio.py`: split input with ffmpeg into ~30–60min chunks at silence
   boundaries (`silencedetect` filter to pick cut points near target boundaries), written to a
   `tempfile.mkdtemp` dir, deleted in `finally`.
10. Per chunk: `transcribe_audio`-equivalent pass; **offset every segment's start/end by the
    chunk's start offset** in the original file.
11. Diarize per chunk with `return_embeddings=True` (already used — `transcriber.py:245`).
    **Reconcile speakers across chunks**: per-chunk `SPEAKER_xx` labels are not globally
    consistent; match clusters across chunks by cosine similarity of their embeddings (reuse
    the matching approach in `speaker_registry.match_speakers`), relabel to global speakers,
    then apply named speaker ID against the global embedding set.
12. Merge all chunk segments in order into one result dict (`{"segments", "text", "language"}` —
    same additive shape) → existing `format_transcription` writes **one** `.txt`. Wire behind
    env knobs `CHUNK_THRESHOLD_MINUTES` / `CHUNK_LENGTH_MINUTES`; files under the threshold use
    the untouched single-pass path.

## Affected areas / files

- `api.py` — TTL-from-completion bugfix (`finished_at`); `MAX_UPLOAD_SIZE_MB` default 500→2048;
  optional duration logging.
- `.env.example`, `docker-compose.yml` — document/adjust `MAX_UPLOAD_SIZE_MB`, `JOB_TTL_SECONDS`.
- `CLAUDE.md`, `README.md` — kill the 25MB myth; document real limits + verified 2h envelope.
- `transcriber.py` — **unchanged in Phases 1–2**; Phase 3 only: threshold branch into chunked path.
- `long_audio.py` (new) — Phase 3 only: split / offset / speaker-reconcile / merge.
- `ignore/` — long test audio (gitignored, not committed).

## Constraints & risks

- **Diarization memory on 2h audio is the unverified unknown** — the whole plan gates on the
  Phase 2 live test. Do not skip it.
- Cross-chunk speaker reconciliation (Phase 3 #11) is the hardest piece: diarization already
  sometimes splits one person into multiple clusters on a *single* file (documented expected
  behavior); across chunks this multiplies. Embedding cosine matching mitigates but is not perfect.
- Hallucination retry doubles wall time on trigger; on a 2h file that could mean ~2× processing.
  Known, accepted; do not silently disable the retry.
- GPU work is serialized (`gpu_lock`) — one 2h job blocks the API queue for its full duration.
  Accepted for now (single-user deployment); queue_position endpoint already signals waiting.
- `JOB_TTL_SECONDS` also bounds how long a *completed* result stays pollable — after the TTL fix
  the default 3600s counts from completion, which is sufficient.
- Windows console: set `PYTHONUTF8=1` for CLI runs (✓/✗ output crashes cp1252).
- Torch CPU trap: any pip operation during implementation must not touch torch without the
  cu128 index (see `CLAUDE.md`); verify `torch.cuda.is_available()` after installs.

## Acceptance criteria / verification

- [ ] A real ~2h multi-speaker file transcribes end-to-end via **CLI**: one `.txt`, header block,
      monotonic timestamps spanning the full duration, `SPEAKER_xx` labels present, enrolled
      voice's name applied to their segments.
- [ ] The same file via **API**: upload accepted (no 413 at default config), job completes, and
      the result is retrievable via `GET /jobs/{id}` **after** completion (TTL bugfix proven —
      poll after completion, not before).
- [ ] Short-file regression: an existing short test file produces identical-shaped output to
      pre-change behavior (CLI skip logic, formatting, diarization untouched).
- [ ] Peak RAM/VRAM and wall time for the 2h run recorded in the PR/summary (documents the envelope).
- [ ] If Phase 3 was triggered: speaker labels are consistent across chunk boundaries (the same
      real person keeps one label/name through the whole transcript) and no temp chunk files
      remain after the run.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/long-audio-compatibility"   # find the observation
mem_get_observation <id>                      # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/long-audio-compatibility.md`. The plan is **approved** — do not
> re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/long-audio-compatibility"` then `mem_get_observation` on the top hit; if
> Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach steps **in phase order** (Phase 3 only if the
> Phase 2 live test fails), and verify with the Acceptance criteria before finishing.
