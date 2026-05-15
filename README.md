# Whisper Audio Transcription Tool 🎙️

A Python transcription tool powered by [`faster-whisper`](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 backend) — up to **4× faster** than `openai-whisper` on GPU, lower VRAM, and INT8 acceleration on CPU. Bundles its own audio decoding via PyAV, so **no system FFmpeg install is required**.

Two run modes:

- **CLI** — batch-transcribe a folder of audio files (`audio_to_text_file.py`).
- **API** — FastAPI HTTP service with async job queue, exposed only over a WireGuard tunnel (`api.py`).

## 🌟 Features

- Batch audio file transcription (CLI) and async HTTP API
- GPU acceleration with CUDA 12 (float16) + automatic CPU fallback (int8)
- Built-in Silero VAD filter — strips silence/noise to reduce hallucination loops
- Automatic hallucination-loop detection with retry on higher-temperature settings
- Timestamp-based transcription output with sanitized filenames
- Multi-language support (auto-detect by default)
- Skip already-transcribed files
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

- Python 3.8+
- CUDA-compatible GPU (optional, for faster processing) with **CUDA 12** drivers
- For GPU on host (non-Docker): **cuDNN 9** + **cuBLAS for CUDA 12** must be available to CTranslate2 (see Installation step 2)
- Required Python packages (see `requirements.txt`)

> No system FFmpeg install is required — `faster-whisper` ships PyAV which handles audio decoding.

## 🚀 Installation

1. Clone the repository:

```bash
git clone https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file.git
```

2. Install the required Python packages:

```bash
pip install -r requirements.txt
```

> **GPU users:** `requirements.txt` already pulls `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*` so CTranslate2 can find cuDNN 9 + cuBLAS at runtime. On Linux you may also need `LD_LIBRARY_PATH` pointing at the wheel install dirs (the Dockerfile does this automatically).

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

## 🐳 Docker / API Mode

The project includes a **FastAPI HTTP server** that exposes Whisper as a remote transcription service. It is fully Dockerized with GPU support and **exposed exclusively through a [WireGuard](https://www.wireguard.com/) tunnel** — nothing binds to LAN or a public interface.

Access model: the PC runs a WireGuard **client** sidecar that dials a VPS WireGuard server. Only devices on that WG network (VPS, laptop, phone) can reach the API. The `X-API-Key` header adds a second layer of defense.

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with the [Docker Compose plugin](https://docs.docker.com/compose/install/)
- [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) (for GPU acceleration)
- A WireGuard server (e.g. [wg-easy](https://github.com/wg-easy/wg-easy) on a VPS) with a client config for this machine

### 1. Copy the example config and write your real wg0.conf

```bash
cp wireguard/wg0.conf.example wireguard/wg0.conf
```

Edit `wireguard/wg0.conf` with the values issued by your VPS WG server:

```ini
[Interface]
PrivateKey = <PC_PRIVATE_KEY>
Address    = 10.0.0.5/32

[Peer]
PublicKey            = <VPS_PUBLIC_KEY>
Endpoint             = vps.example.com:51820
AllowedIPs           = 10.0.0.0/24
PersistentKeepalive  = 25
```

> `wireguard/wg0.conf` is git-ignored — it contains your private key. Never commit it.

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set your API key:

```env
API_KEY=change-me-to-a-random-secret
```

> **Note:** `/health` is public and does not require the API key.

### 3. Build and start

```bash
docker compose up --build -d
```

Two containers start: `whisper-wireguard` (dials the VPS over WireGuard) and `whisper-api` (shares its network namespace). Allow ~60–120 seconds on first run for the Whisper model to load.

Confirm the tunnel is up:

```bash
docker compose logs wireguard
# look for: wg-quick: [#] wg setconf wg0 ...

docker compose exec wireguard wg show
# expect: latest handshake populated within ~30s
```

### 4. Verify it's running

From **any device on the WG network** (e.g. the VPS):

```bash
curl http://10.0.0.5:8000/health
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

> `"device": "cuda"` confirms GPU passthrough is working. If you see `"cpu"`, check your NVIDIA Container Toolkit / WSL2 GPU passthrough setup.

### 5. Submit a transcription job

```bash
curl -X POST http://10.0.0.5:8000/transcribe \
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

### 6. Poll for the result

```bash
curl http://10.0.0.5:8000/jobs/3f8a1c2d-... \
  -H "X-API-Key: your-api-key"
```

Job status values: `queued` → `processing` → `completed` / `failed`

Completed response includes the full timestamped transcription in `result.formatted` and the plain text in `result.text`. Completed and failed jobs are purged from memory after `JOB_TTL_SECONDS` (default: 1 hour).

### 7. Stop the service

```bash
docker compose down
```

The Whisper model cache is stored in a Docker volume (`whisper-cache`) so it survives container restarts.

### Troubleshooting: WireGuard kernel module on Windows Docker Desktop (WSL2)

If `docker compose logs wireguard` shows `Unable to find module 'wireguard'`, the WG kernel module is missing. Enable userspace mode by adding `- USE_BORINGTUN=true` to the `wireguard` service `environment:` in `docker-compose.yml`. Slight throughput hit, no other change.

---

## �🔑 License

- [GPL-3.0 license](https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file/blob/main/LICENSE)

## About developer

Visit my web [Carlos Ochoa](https://carlos8a.com)

---

**Note:** If you encounter any issues with the project, please report them [here](https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file/issues). Contributions are welcome!
