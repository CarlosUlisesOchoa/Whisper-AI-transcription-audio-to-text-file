# Whisper Audio Transcription Tool 🎙️

A powerful Python-based transcription tool that leverages OpenAI's Whisper model to transcribe audio files with GPU acceleration support.

## 🌟 Features

- Batch audio file transcription
- GPU acceleration with CUDA support
- Timestamp-based transcription output
- Multi-language support
- Easy-to-use command line interface
- Automatic file status checking
- Skip already transcribed files
- Detailed processing summary

## 🔧 Usage Examples

Run the script with various arguments:

```bash
# Basic usage with a directory containing audio files
py audio_to_text_file.py "path/to/audio/folder"

# Specify a different language (default is auto-detect)
py audio_to_text_file.py "path/to/audio/folder" --language es

# Auto-accept file list without confirmation
py audio_to_text_file.py "path/to/audio/folder" --accept
```

Available arguments:

- Directory path: First positional argument (required)
- `--language`: Input language (optional, defaults to auto-detection)
- `--accept`: Auto-accept file list without confirmation prompt (optional)

## 🔧 Requirements

- Python 3.7+
- FFmpeg
- CUDA-compatible GPU (optional, for faster processing)
- Required Python packages (see `requirements.txt`)

## 🚀 Installation

1. Clone the repository:

```bash
git clone https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file.git
```

2. Install the required Python packages:

```bash
pip install -r requirements.txt
```

3. Run the script:

```bash
py audio_to_text_file.py "D:\files\audio_folder" --language en
```

The script will:

1. Scan the directory for audio files (.mp3, .wav, .m4a, .ogg, .flac)
2. Skip files that already have transcriptions
3. Show a summary of files to be processed
4. Ask for confirmation (unless --accept is used)
5. Process each file and save transcriptions with timestamps
6. Display a detailed completion summary

Output files will be saved in the same folder as the input files, with sanitized filenames and .txt extension.

## � Docker / API Mode

The project includes a **FastAPI HTTP server** that exposes Whisper as a remote transcription service. It is fully Dockerized with GPU support via the NVIDIA Container Toolkit.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with the [Docker Compose plugin](https://docs.docker.com/compose/install/)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (for GPU acceleration)

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set your values:

```env
# Required: shared secret that callers must send in X-API-Key header
API_KEY=change-me-to-a-random-secret

# Optional: comma-separated IPs allowed to call the API (leave empty to allow all)
ALLOWED_IPS=

# Optional overrides (defaults shown)
# JOB_TTL_SECONDS=3600
# MAX_UPLOAD_SIZE_MB=500
```

> **Note:** `/health` is public and does not require the API key.

### 2. Build and start

```bash
docker compose up --build -d
```

The container pre-loads the Whisper model on startup — allow ~60–120 seconds on first run.

### 3. Verify it's running

```bash
curl http://localhost:8000/health
```

Example response:

```json
{
  "status": "ok",
  "device": "cuda",
  "gpu_name": "NVIDIA GeForce RTX 3080",
  "queue_depth": 0,
  "jobs_total": 0
}
```

### 4. Submit a transcription job

```bash
curl -X POST http://localhost:8000/transcribe \
  -H "X-API-Key: your-api-key" \
  -F "file=@recording.mp3" \
  -F "language=en"
```

Supported formats: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`

`language` is optional — omit it for automatic language detection.

Response:

```json
{ "job_id": "3f8a1c2d-..." }
```

### 5. Poll for the result

```bash
curl http://localhost:8000/jobs/3f8a1c2d-... \
  -H "X-API-Key: your-api-key"
```

Job status values: `queued` → `processing` → `completed` / `failed`

Completed response includes the full timestamped transcription in `result.formatted` and the plain text in `result.text`. Completed and failed jobs are purged from memory after `JOB_TTL_SECONDS` (default: 1 hour).

### 6. Stop the service

```bash
docker compose down
```

The Whisper model cache is stored in a Docker volume (`whisper-cache`) so it survives container restarts.

---

## �🔑 License

- [GPL-3.0 license](https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file/blob/main/LICENSE)

## About developer

Visit my web [Carlos Ochoa](https://carlos8a.com)

---

**Note:** If you encounter any issues with the project, please report them [here](https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file/issues). Contributions are welcome!
