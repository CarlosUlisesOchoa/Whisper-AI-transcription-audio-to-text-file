# Plan: Word-level speaker attribution (fix short-utterance misattribution)

> Date: 2026-07-18 · Slug: `word-level-speaker-attribution` · Engram topic: `plan/word-level-speaker-attribution`
>
> This file is **self-contained**: it holds the full plan and is enough to implement from on its
> own. Engram is a convenience layer for recovery; this file is the backup of record.

## Context

After landing auto-enrollment (`dev-docs/auto-enroll-unknown-speakers.md`), a live run showed
that when someone speaks only briefly (a few-second phrase), the transcript does **not**
separate them — their words land on the previous speaker's line, attributed to the wrong
person.

**Root cause (confirmed in code, not hypothesized):**

- `whisperx.assign_word_speakers` (`.venv/.../whisperx/diarize.py:217-229`) assigns **one
  speaker per whole Whisper segment** by dominant time-overlap against the diarization
  DataFrame. Whisper cuts segments at pauses/punctuation, **not** at speaker changes. A short
  phrase from speaker B inside a segment dominated by speaker A → the entire segment is labeled
  A. Exactly the reported bug.
- The same function **already computes per-word speakers** (`word['speaker']`,
  `diarize.py:238-257`) — but only when segments carry a `words` array, i.e. only when
  alignment ran. And even then, our `transcriber.py:256-259` copies only the segment-level
  label; word-level data is thrown away.
- Pyannote diarization itself **does** detect the short speaker: the `diarize_segments`
  DataFrame contains their turns and `speaker_embeddings` contains their cluster. The loss
  happens purely at transcript-attribution time.

**Enrollment is NOT broken.** `transcribe_audio` builds enrollment samples from the diarization
DataFrame (`transcriber.py:288-297`), independent of transcript labels. A speaker whose short
phrases sum to ≥ `ENROLL_MIN_SECONDS` (default 10 s) across the file is **already enrolled**
as `voices/unknown-NN.wav` today — even if the transcript never shows their label. Below the
minimum they are skipped by design (user confirmed the criteria stay as-is). This plan makes
the transcript attribution match.

## Goal & outcome

With diarization on, a short interjection by another speaker appears as its **own segment with
the correct speaker label** instead of being absorbed into the neighboring speaker's segment.
Short-turn speakers whose total speech meets `ENROLL_MIN_SECONDS` keep being auto-enrolled
(unchanged), and their now-separated segments carry their `unknown-NN` (or matched) name.

## Decisions (user-confirmed 2026-07-18)

| Question | Decision |
|----------|----------|
| When does word-level attribution run? | **Auto whenever diarization runs** — alignment executes internally, no new flag/env. Chosen over opt-in (`--align`-gated) and over a dedicated flag. Cost accepted: extra wav2vec2 alignment pass (~10–20% runtime) on every diarized run. |
| Enrollment criteria | **Unchanged** — `ENROLL_MIN_SECONDS` (10 s) / `ENROLL_MAX_SECONDS` (30 s) stay; short speakers below the minimum stay skipped. |
| Alignment failure / unsupported language | Warn + **fall back to current segment-level attribution** (no crash). |

## Scope

- **In scope**:
  - Internal auto-alignment when diarization runs (reuse the existing `_get_align_model`
    per-language cache in `transcriber.py`).
  - Per-word speaker assignment via the existing `whisperx.assign_word_speakers` word path.
  - Splitting Whisper segments at word-speaker-change boundaries into sub-segments
    `{start, end, text, speaker}`.
  - Applying the existing `name_map` (registry match + fresh enrollments) **after** the split,
    so split segments carry enrolled/matched names; when `align=True` was requested, also map
    `word['speaker']` values so the words array is consistent.
  - Docs updates (`CLAUDE.md`, `README.md`).
- **Out of scope / non-goals**:
  - No pyannote pipeline tuning (`num_speakers`/`min_speakers`/`max_speakers`, clustering
    params).
  - No merging/suppression of tiny backchannel segments; no smoothing pass over word-speaker
    flips (possible follow-up if boundary noise annoys).
  - No change to enrollment thresholds or the rename workflow.
  - No new env vars, no opt-out switch — the fallback path covers failures; add a kill-switch
    later only if the perf cost proves painful.
  - No relabeling of previously written transcripts.
  - Whisper ASR missing ultra-short/overlapped speech entirely is out of scope — we can only
    label words Whisper actually transcribed.

## Approach

All changes live in `transcriber.py`. `speaker_registry.py`, `api.py`, and
`audio_to_text_file.py` need **no logic changes** (API/CLI already thread `align`/`diarize`
through; result shape change is additive).

1. **Track whether the caller asked for words.** In `transcribe_audio`, remember
   `words_requested = align`. The existing `if align:` block stays as-is (it populates
   `seg["words"]`).

2. **Auto-align inside the diarize block.** In the `if diarize:` path (after the HF_TOKEN
   check, before `assign_word_speakers`): if segments don't already have `words` (i.e.
   `align` was False or alignment failed), run the same alignment code path internally —
   `_get_align_model(detected_language)` + `whisperx.align(...)` — wrapped in try/except.
   On failure (unsupported language, any error): `logger.warning(...)` and proceed **without**
   words — `assign_word_speakers` then behaves exactly as today (segment-level dominant
   overlap), which is the agreed fallback. Factor the align-and-attach-words code shared by
   the `align` block and this auto-align into a small helper (e.g. `_align_segments(result,
   audio, language)`) to avoid duplication.

3. **Split segments by word speaker.** New helper
   `_split_segments_by_word_speaker(segments) -> list[dict]`, called after
   `assign_word_speakers` copies word+segment speakers onto `result["segments"]`:
   - For each segment **without** `words` or with ≤1 distinct `word['speaker']` value: keep it
     verbatim (original `text`, original bounds, segment-level `speaker`). This preserves exact
     Whisper text/punctuation for the common single-speaker case.
   - For each segment with ≥2 distinct word speakers: walk words in order, group consecutive
     words by `word['speaker']`; each group becomes a sub-segment:
     - `start` = first grouped word's `start` (first group inherits the segment's `start`),
     - `end` = last grouped word's `end` (last group inherits the segment's `end`),
     - `text` = `" ".join(word['word'] ...)` stripped (whisperX word tokens carry their
       punctuation),
     - `speaker` = the group's word speaker,
     - when `words_requested`, attach the group's word list as `words`.
   - Edge rules: a word with no `start` (wav2vec2 sometimes can't align digits/symbols) or no
     `speaker` (no diarization overlap) never opens a new group — attach it to the current
     group; if it appears before any speakered word, buffer it into the first group.
   - When `words_requested` is False, strip `words` from all output segments at the end
     (contract unchanged: `words` only appears when the caller asked for alignment).

4. **Apply `name_map` after the split** (registry match + enrollment logic itself is
   untouched): the existing final loop mapping `seg["speaker"]` runs over the split segments;
   when `words_requested`, also map `word["speaker"]` inside each segment's words.
   Note enrollment order is fine as-is: samples are cut from the diarization DataFrame, which
   the split does not touch.

5. **Rebuild `result["text"]`** — no change needed; the existing join over
   `result["segments"]` runs after all of this. Verify segment ordering stays chronological
   (split preserves order by construction).

6. **Docs.**
   - `CLAUDE.md` → Key behaviors → Diarization bullet: word-level attribution is automatic
     when diarizing (internal alignment, fallback on unsupported language); note the extra
     wav2vec2 model download on first diarized run for users who never used `--align`; note
     segments may be finer-grained than before (additive shape change).
   - `README.md`: short note under the diarization/speakers section — short interjections now
     get their own labeled lines.
   - `.env.example`: no changes (no new vars).

## Affected areas / files

- `transcriber.py` — `words_requested` tracking; shared `_align_segments` helper; auto-align
  inside the diarize block with warn-and-fallback; new `_split_segments_by_word_speaker`;
  `name_map` application over split segments (+ word speakers when requested); strip `words`
  when not requested.
- `CLAUDE.md`, `README.md` — behavior + docs updates. `.env.example` untouched.
- No changes: `speaker_registry.py`, `api.py`, `audio_to_text_file.py` (flags/fields already
  exist; result shape change is additive).

## Constraints & risks

- **Perf**: alignment now runs on every diarized transcription (user accepted, ~10–20% on
  GPU). First diarized run downloads the wav2vec2 model (~360 MB) for users who never passed
  `--align`.
- **Boundary flips**: wav2vec2 word timings vs. diarization turn edges can disagree by tens of
  ms — an edge word can land on the neighbor speaker, creating a 1-word segment. Accepted for
  v1; smoothing is an explicit non-goal/follow-up.
- **Split-segment text**: rebuilt from word tokens joined with spaces — spacing/punctuation can
  differ subtly from Whisper's segment text. Only affects segments that actually split;
  single-speaker segments keep verbatim text.
- **API consumers**: more, shorter segments per file (additive change; `formatted` output same
  line pattern). Job JSON gets no new keys.
- **Hallucination retry path**: untouched — split runs downstream of retry logic.
- **Windows console**: no new non-ASCII prints; existing `PYTHONUTF8=1` caveat stands.
- **ASR limits**: if Whisper never transcribed the interjection (heavy overlap), no label can
  fix that — diarization DataFrame/enrollment still see the speaker, the transcript cannot.

## Acceptance criteria / verification

Live-verify (project convention). Set `PYTHONUTF8=1`. Use real multi-speaker audio containing
short interjections (e.g. the 19-minute meeting recording used for auto-enroll verification).

1. **Interjection separated (CLI)**: run with diarization on → a known short phrase by another
   speaker appears as its own `[start - end] speaker: text` line with the correct label, not
   absorbed into the previous speaker's segment. Compare against a pre-change transcript of
   the same file.
2. **Enrollment + labeling**: with `--enroll-unknown`, a short-turn speaker whose phrases sum
   to ≥ 10 s → `voices/unknown-NN.wav` written AND their split segments labeled `unknown-NN`.
3. **Short-speech skip**: speaker under 10 s total → no file, log line, their split segments
   stay `SPEAKER_xx`.
4. **Words contract**: `--align` run → segments still carry `words` (with `speaker` per word);
   run without `--align` → no `words` key anywhere in the result, but splitting still happened.
5. **Fallback**: force an unsupported alignment language (or simulate load failure) →
   warning logged, segment-level attribution (old behavior), no crash.
6. **No-diarize regression**: `--no-diarize` (and API `diarize=false`) → alignment does NOT
   run (no wav2vec2 load in logs), output byte-identical to current behavior.
7. **API**: `POST /transcribe` with `diarize=true` → completed job's `result.formatted` shows
   the split segments; `align=true` form field still yields word arrays.

## Engram recovery (optional convenience)

Full context is also saved to Engram. To recover it in a new chat:

```
mem_search "plan/word-level-speaker-attribution"   # find the observation
mem_get_observation <id>                            # full untruncated content of the top hit
```

If Engram is unavailable, ignore this section — everything needed is already above.

## Implementation prompt (paste into a new chat)

> Implement the plan in `dev-docs/word-level-speaker-attribution.md`. The plan is **approved** —
> do not re-plan. Optionally recover extra context from Engram first:
> `mem_search "plan/word-level-speaker-attribution"` then `mem_get_observation` on the top hit;
> if Engram is unavailable, the `.md` file is self-contained and sufficient. Follow this
> project's `CLAUDE.md` conventions, implement the Approach steps, and verify with the
> Acceptance criteria before finishing.
