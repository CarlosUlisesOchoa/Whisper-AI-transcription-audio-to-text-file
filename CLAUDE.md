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

The entire tool lives in `audio_to_text_file.py`. Key behaviors:

- **Skip logic**: Before transcribing, the script checks if a `.txt` file with the sanitized name already exists in the same directory. If it does, the audio file is skipped.
- **Filename sanitization** (`sanitize_filename`): Converts to lowercase, replaces non-alphanumeric characters (except `-` and `_`) with dashes, collapses repeated dashes, strips leading/trailing dashes. Output `.txt` files are saved alongside the source audio using these sanitized names.
- **Model**: Hardcoded to `whisper.load_model("medium")`. Device is auto-selected (CUDA if available, else CPU).
- **Output format**: Each `.txt` file begins with a header block (`===...`, `filename:...`, `===...`), followed by timestamped segments in `[start - end] text` format.
- **Supported audio formats**: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`

## GPU / CUDA Notes

CUDA availability is detected at runtime via `torch.cuda.is_available()`. No configuration needed — if a CUDA-compatible GPU is present with the correct PyTorch CUDA build, it will be used automatically. The `ignore/` directory is gitignored and can be used for local test files.
