---
name: transcribe-8a
description: >
  Generates and runs the exact `audio_to_text_file.py` CLI command for this project from a
  plain-language transcription request — handles diarization on/off, language, alignment,
  custom voices dir, auto-enroll, and the non-interactive --accept requirement.
  Trigger: user types `/transcribe-8a <path/to/audio> <specifications>`.
license: Apache-2.0
metadata:
  author: Carlos Ochoa
  version: "1.0"
---

## When to Use

- User types `/transcribe-8a <path> <specs>` to transcribe audio in this project without hand-writing CLI flags.
- User describes what they want in plain language (language, diarization on/off, custom voices, auto-enroll) and needs the equivalent flag combo built and run.

## Critical Patterns

1. **Base command** (always), run from repo root `D:\Shane\Desktop\py-projects\whisper_transcript_with_gpu_cuda`:
   `py audio_to_text_file.py "<path>"`
2. **Windows console encoding**: always prefix with `$env:PYTHONUTF8=1;` (PowerShell) — the script prints `✓`/`✗`; PowerShell's default cp1252 encoding crashes on them.
3. **Non-interactive execution**: always append `--accept` when the skill itself runs the command. Without it, `main()` blocks on `input()` for a y/N confirmation — under Bash-tool/non-TTY execution this hangs forever, no timeout, no way to recover except killing the process.
4. **Diarization is ON by default** (`ENABLE_DIARIZATION=true` in `transcriber.py`). "Straightforward transcription" / "no speaker ID" / "don't identify speakers" / "just the transcript" → add `--no-diarize`. **Timestamps are always present regardless** — `[start - end] text` is the baseline output format, never a flag, never omit even with `--no-diarize`.
5. `--align` is unrelated to diarization quality — when diarization is ON, word-level speaker splitting already runs internally regardless of `--align`. `--align` only additionally exposes per-word timings in the JSON/API result, irrelevant to the `.txt` file. Only add it if the user explicitly asks for word-level timestamp data.
6. Language: omit `--language` for auto-detect (default). Map the spoken language to its ISO 639-1 code:

   | User says | Flag |
   |---|---|
   | Spanish / español | `--language es` |
   | English / inglés | `--language en` |
   | Portuguese | `--language pt` |
   | French | `--language fr` |
   | German | `--language de` |
   | Italian | `--language it` |

   Any other language → its ISO 639-1 code. Omit the flag if unspecified.
7. Optional flags — only add when explicitly requested, and never together with `--no-diarize` (nothing to match/enroll if diarization is off — warn the user if they ask for both):
   - Custom voices folder → `--voices "<path>"`
   - Auto-enroll unmatched speakers as new voices → `--enroll-unknown`

## Decision Tree

Parse `<specifications>` free text into flags:

```
"straightforward" / "no diarization" / "don't identify speakers" / "just the transcript"
    -> --no-diarize

language mentioned (name or code)
    -> --language <iso-code>

"word-level timestamps" / "word timings" / "align"
    -> --align

"voices from <path>" / "use voices folder <path>"
    -> --voices "<path>"

"auto-enroll" / "enroll unknown speakers"
    -> --enroll-unknown   (only if diarization stays ON)

nothing else specified
    -> diarization stays ON (default), language auto-detect
```

## Code Examples

Input: `/transcribe-8a "D:\Meetings\aug20" straightforward, in spanish`
```powershell
$env:PYTHONUTF8=1; py audio_to_text_file.py "D:\Meetings\aug20" --language es --no-diarize --accept
```

Input: `/transcribe-8a "D:\Meetings\aug20"` (no specs)
```powershell
$env:PYTHONUTF8=1; py audio_to_text_file.py "D:\Meetings\aug20" --accept
```
(diarization stays on by default, language auto-detect)

Input: `/transcribe-8a "D:\Meetings\aug20" english, auto-enroll new speakers`
```powershell
$env:PYTHONUTF8=1; py audio_to_text_file.py "D:\Meetings\aug20" --language en --enroll-unknown --accept
```

## Commands

Base pattern (PowerShell, run from repo root):
```powershell
$env:PYTHONUTF8=1; py audio_to_text_file.py "<path>" [--language <code>] [--no-diarize] [--align] [--voices "<path>"] [--enroll-unknown] --accept
```

Bash-tool equivalent (Git Bash, not PowerShell):
```bash
PYTHONUTF8=1 py audio_to_text_file.py "<path>" [flags] --accept
```

## Workflow

1. Parse `<path>` and `<specifications>` from the invocation.
2. Verify `<path>` exists before building the command — fail fast with a clear message rather than launching a doomed run.
3. Build the command per the Decision Tree above.
4. Show the generated command to the user.
5. Run it via the Bash tool. This is a long-running GPU job (can take minutes) — do not treat it as a quick command, do not add a short timeout.

## Not Yet Handled (deferred resilience, per project owner's note)

- Docker/`whisper-api` health checks — N/A for this skill: CLI mode runs whisperX locally, no Docker involved.
- Missing `HF_TOKEN` — already degrades gracefully in `transcriber.py` (diarization/speaker-ID skip, plain transcription still runs); no skill action needed.
- GPU unavailable — script auto-falls-back to CPU (slower, `int8` compute type); informational only, not an error.

## Resources

- CLI flags: `audio_to_text_file.py`
- Defaults/env resolution: `transcriber.py`
- Full behavior reference: `AGENTS.md` → "Running the Script", "Key behaviors"
