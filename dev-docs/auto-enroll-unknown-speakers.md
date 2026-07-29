# Plan: Auto-enroll unknown speakers into Voices

> Date: 2026-07-18 · Slug: `auto-enroll-unknown-speakers` · Engram topic: `plan/auto-enroll-unknown-speakers`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

The project already does speaker diarization (pyannote 3.1 via whisperX) and **named speaker ID**:
diarized clusters are matched by cosine similarity against reference samples in a `voices/` dir
(`speaker_registry.py`). But a speaker is only ever named if someone **manually** recorded and
dropped a sample into `voices/` first. Unknown speakers stay as anonymous `SPEAKER_xx` forever.

Requested feature: when a diarized speaker matches **no** registry entry, the app should
automatically capture a 10–30 s voice sample from that speaker's own audio, save it into the
voices dir under a placeholder name, and use that placeholder as the speaker label. The user later
renames the file to the person's real name; from then on, every future audio identifies that
speaker automatically.

Key enabler already in the code: `transcribe_audio` calls the diarization pipeline with
`return_embeddings=True`, so per-cluster wespeaker embeddings AND per-turn timestamps
(`diarize_segments` DataFrame with `start`/`end`/`speaker` columns) are already available at the
exact point where unmatched speakers are known. The in-memory audio (`whisperx.load_audio` →
16 kHz mono float32 numpy array) is also in scope — cropping is pure numpy index math.

## Goal & outcome

A run with enrollment enabled writes `voices/unknown-NN.wav` (10–30 s, 16 kHz mono) for every
unmatched speaker with enough speech, labels them `unknown-NN` in the transcript, and — after the
user renames the file — identifies that person by name in all future runs. CLI and API both.

## Decisions (user-confirmed 2026-07-18)

| Question | Decision |
|----------|----------|
| Scope | **CLI + API** |
| Activation | **Opt-in**: CLI `--enroll-unknown`, API `enroll_unknown` form field, `AUTO_ENROLL_UNKNOWN` env default (`false`) |
| Placeholder naming | **`unknown-NN`** (`unknown-01.wav`, `unknown-02.wav`, …) — prefix makes un-renamed entries obvious, no visual collision with pyannote's `SPEAKER_xx` |
| Short-speech edge | **Skip below 10 s** of usable speech (`ENROLL_MIN_SECONDS=10`, env-tunable); speaker stays `SPEAKER_xx`, reason logged |

## Scope

- **In scope**:
  - Sample extraction + WAV writing + placeholder naming in `speaker_registry.py`.
  - `enroll_unknown` parameter threaded through `transcribe_audio` (env default), CLI flag, API form field.
  - In-place registry cache update so later files/jobs in the same process match fresh unknowns
    without restart.
  - Within-run duplicate handling: a second unmatched cluster is checked against just-enrolled
    unknowns before creating another file.
  - Docs + `.env.example` updates.
- **Out of scope / non-goals**:
  - No rename UI or API endpoint (user renames files by hand — that IS the workflow).
  - No auto-merge of cross-run duplicates (same person scoring below threshold vs their own
    `unknown-NN` sample creates a new entry; user deletes/renames, threshold tunable via the
    existing per-speaker best-score log lines).
  - No relabeling of previously written transcripts.
  - No file-locking for CLI + API writing the same voices dir concurrently.

## Approach

### New env vars (read in `transcriber.py`, documented in `.env.example`)

- `AUTO_ENROLL_UNKNOWN` (default `false`) — process-wide default; per-call param overrides.
- `ENROLL_MIN_SECONDS` (default `10`) — skip enrollment below this much usable speech.
- `ENROLL_MAX_SECONDS` (default `30`) — cap written sample length.

### Steps

1. **`speaker_registry.py` — enrollment primitives**
   - `next_unknown_name(voices_dir) -> str`: scan dir for stems matching `unknown-(\d+)`,
     return `unknown-NN` with `NN = max + 1`, zero-padded to 2 digits.
   - `extract_speaker_sample(audio, turns, min_seconds, max_seconds) -> np.ndarray | None`:
     `audio` is the 16 kHz mono float32 array already loaded by `transcribe_audio`; `turns` is a
     list of `(start, end)` for one speaker from the diarization DataFrame.
     - Prefer turns that do **not** overlap other speakers' turns (cleaner embedding); if the
       non-overlapping subset totals < `min_seconds`, fall back to all turns.
     - Greedy: sort by duration desc, take longest turns until total ≥ `max_seconds` or exhausted.
     - Total < `min_seconds` → return `None` (caller logs and skips).
     - Crop by sample index (`int(t * 16000)`), concatenate.
     - **Continuity NOT required**: the minimum is the SUM of the speaker's turns, spliced from
       anywhere in the file (e.g. 7 s early + 4 s later = 11 s → enrolled). Splice cuts are
       harmless — the clip is only fed to the wespeaker embedding model (voice traits averaged
       over the whole clip), never to Whisper.
   - `enroll_speaker(sample, voices_dir, name, hf_token) -> np.ndarray | None`: write
     `voices_dir/name.wav` with **soundfile** (`sf.write(path, sample, 16000)` — already in the
     dependency tree via `pyannote-audio`; no new requirement), then embed the **written file**
     with the existing `_embed(path, hf_token)` and return that embedding. Re-embedding the file
     (rather than reusing the cluster embedding) guarantees the in-memory cache value is identical
     to what `load_registry` will compute on every future run.

2. **`transcriber.py` — wire into `transcribe_audio`**
   - Add param `enroll_unknown=None` (None → `AUTO_ENROLL_UNKNOWN` env default), read the two
     `ENROLL_*` env vars at module level next to `SPEAKER_MATCH_THRESHOLD`.
   - Restructure the diarize block: currently `match_speakers` only runs `if registry:` — change
     so the flow continues into enrollment even when the registry is empty (first-ever run with an
     empty voices dir must still enroll). `name_map = match_speakers(...) if registry else {}`.
   - Enrollment (runs when `enroll_unknown and voices_dir and speaker_embeddings and HF_TOKEN`):
     - `os.makedirs(voices_dir, exist_ok=True)`.
     - Unmatched = labels in `speaker_embeddings` not in `name_map`, processed in sorted order.
     - For each unmatched cluster:
       - Guard: skip if embedding is not finite (`np.isfinite(...).all()` — pyannote can emit NaN
         embeddings for tiny clusters).
       - **Within-run dedup**: cosine-check the cluster embedding against entries added to the
         registry during THIS run (same `SPEAKER_MATCH_THRESHOLD`); on match, map the label to
         that existing `unknown-NN` instead of creating a new file (diarization sometimes splits
         one person into two clusters).
       - Otherwise: build turns list from the diarization DataFrame for that label,
         `extract_speaker_sample` → `None` means log + skip (stays `SPEAKER_xx`);
         else `next_unknown_name` → `enroll_speaker` → insert returned embedding into
         `_speaker_registry_cache[voices_dir]` **in place** (later files in a CLI batch and later
         API jobs see it without reload) → add `label → unknown-NN` to `name_map`.
     - Existing segment loop then applies the enlarged `name_map`, so the current transcript
       already shows `unknown-NN` — the user can read the transcript, listen to
       `voices/unknown-NN.wav`, and rename with confidence.
   - Wrap enrollment in try/except mirroring the existing diarization error style: a failed
     enrollment must never fail the transcription.

3. **`audio_to_text_file.py` — CLI**
   - Add `--enroll-unknown` (store_true). Pass through to `transcribe_audio`.
   - `resolve_voices_dir`: when the flag is set and nothing resolves (no `--voices`, no
     `VOICES_DIR`, no existing `voices/`), default to `voices/` next to the script anyway —
     enrollment needs a destination and will create it.
   - Print a line when enrollment is active, and per-file lines for each enrolled `unknown-NN`.

4. **`api.py` — API**
   - `enroll_unknown: bool | None = Form(default=None)` on `/transcribe`, threaded through
     `_process_job` → `_run_transcription` → `transcribe_audio` exactly like `align`/`diarize`.
   - `/health`: `speaker_id_enabled` is currently a startup-time snapshot (`_speaker_id_enabled`)
     that goes stale (false → should be true) after the first enrollment into an initially empty
     voices dir. Recompute from `transcriber._speaker_registry_cache.get(VOICES_DIR)` when
     present, falling back to the startup flag.
   - No Docker changes needed: `docker-compose.yml` already mounts `./voices:/app/voices` rw, so
     enrolled samples land on the host and survive restarts.

5. **Docs**
   - `.env.example`: the three new vars with comments.
   - `CLAUDE.md`: new "Auto-enrollment" bullet under Key behaviors (naming scheme, opt-in, skip
     rule, cache update, rename workflow); env var list; verification status line.
   - `README.md`: short user-facing section — enable flag, rename `unknown-NN.wav` workflow.

6. **Live verification** (see Acceptance criteria) — run CLI + API against real multi-speaker
   audio; this project's convention is features are not "done" until verified live.

## Affected areas / files

- `speaker_registry.py` — new: `next_unknown_name`, `extract_speaker_sample`, `enroll_speaker`.
- `transcriber.py` — `enroll_unknown` param, `AUTO_ENROLL_UNKNOWN`/`ENROLL_MIN_SECONDS`/`ENROLL_MAX_SECONDS` env config, restructured diarize block (match even with empty registry → dedup → enroll → in-place cache update → label mapping).
- `audio_to_text_file.py` — `--enroll-unknown` flag, voices-dir default/creation when flag set.
- `api.py` — `enroll_unknown` form field, threading, `/health` dynamic `speaker_id_enabled`.
- `.env.example`, `CLAUDE.md`, `README.md` — docs.

## Constraints & risks

- **HF_TOKEN required**: enrollment embeds with the wespeaker model — no token → log warning,
  skip enrollment, transcription unaffected (same graceful-degradation pattern as diarization).
- **Overlapping speech contaminates samples** — mitigated by preferring non-overlapping turns,
  falling back to all turns only when below the minimum.
- **NaN cluster embeddings** (pyannote edge case on tiny clusters) — guard and skip.
- **Cross-run duplicates accepted**: same person scoring below `SPEAKER_MATCH_THRESHOLD` (0.5
  default) against their own earlier sample creates `unknown-NN+1`. The existing per-speaker
  best-score log lines are the tuning signal; user cleans up by deleting/renaming.
- **Embedding-space consistency**: registry entries must come from `_embed` on the written file
  (window="whole" Inference), NOT the raw cluster embedding — otherwise the in-memory cache and
  the next run's `load_registry` disagree slightly for the same file.
- **Rename discipline**: registry names are filename stems, verbatim. Two files renamed for the
  same person get distinct stems (`carlos.wav` + `carlos-2.wav` → labels `carlos`, `carlos-2`) —
  prefer deleting the weaker duplicate. Document in README.
- **Concurrency**: API jobs are serialized (GPU `asyncio.Lock` + single-worker executor), CLI is
  a single process — `unknown-NN` numbering has no race inside one process. Simultaneous CLI +
  API on the same voices dir is explicitly out of scope.
- **Windows console**: any new prints follow the existing `PYTHONUTF8=1` caveat (avoid adding new
  non-ASCII glyphs beyond the existing ✓/✗).
- Sample WAV format: 16 kHz mono, written by soundfile — `whisperx.load_audio` (ffmpeg) reads it
  back fine on future `load_registry` runs.

## Acceptance criteria / verification

Run live (project convention: verified-live or it didn't happen). Set `PYTHONUTF8=1`.

1. **Fresh enrollment (CLI)**: multi-speaker audio, empty/partial `voices/`,
   `py audio_to_text_file.py <dir> --enroll-unknown` → each unmatched speaker with ≥ 10 s speech
   produces `voices/unknown-NN.wav` (10–30 s, 16 kHz mono — inspect with ffprobe) and transcript
   segments show `unknown-NN:` labels.
2. **Rename → future ID**: rename `unknown-01.wav` → `maria.wav`, delete the output `.txt`,
   re-run on the same audio → that speaker is labeled `maria`.
3. **Short-speech skip**: speaker with < 10 s total speech → no file written, label stays
   `SPEAKER_xx`, skip reason in the log.
4. **No regression**: without the flag (and `AUTO_ENROLL_UNKNOWN` unset) → zero writes to
   `voices/`, output identical to current behavior. Matched (already-enrolled) speakers are never
   re-enrolled even with the flag on.
5. **API**: `POST /transcribe` with `-F "enroll_unknown=true"` → completed job's
   `result.formatted` shows `unknown-NN`, sample appears in mounted `./voices`, and a **second**
   job with the same voice matches `unknown-NN` without restarting the server (in-place cache
   update proven). `/health` flips `speaker_id_enabled` to `true` after first enrollment into an
   initially empty voices dir.
6. **Graceful degradation**: unset `HF_TOKEN` → run with flag on → plain transcription, warning
   logged, no crash.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/auto-enroll-unknown-speakers"   # find the observation
mem_get_observation <id>                          # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/auto-enroll-unknown-speakers.md`. The plan is **approved** — do
> not re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/auto-enroll-unknown-speakers"` then `mem_get_observation` on the top hit; if
> Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this project's
> `CLAUDE.md` conventions, implement the Approach steps, and verify with the Acceptance criteria
> before finishing.
