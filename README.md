# Whisper Audio Transcription Tool 🎙️

A Python transcription tool powered by [`whisperX`](https://github.com/m-bain/whisperX) (CTranslate2 backend under the hood, same as `faster-whisper`) — adds **speaker diarization** (pyannote) and **named speaker identification** on top of fast GPU transcription.

Two run modes:

- **CLI** — batch-transcribe a folder of audio files (`audio_to_text_file.py`).
- **API** — FastAPI HTTP service with async job queue, exposed only over a WireGuard tunnel (`api.py`).

## 🌟 Features

- Batch audio file transcription (CLI) and async HTTP API
- GPU acceleration with CUDA 12.8 (float16) + automatic CPU fallback (int8)
- **Speaker diarization** — labels each segment `SPEAKER_00`, `SPEAKER_01`, etc. (pyannote 3.1)
- **Named speaker identification** — enroll reference voices in `voices/` and get real names instead of `SPEAKER_xx`
- Optional word-level alignment (`--align`, wav2vec2)
- Built-in VAD filter — strips silence/noise to reduce hallucination loops
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

# Word-level alignment (adds precise word timings, opt-in)
py audio_to_text_file.py "path/to/audio/folder" --align

# Disable speaker diarization for this run
py audio_to_text_file.py "path/to/audio/folder" --no-diarize

# Named speaker identification from a custom voices folder
py audio_to_text_file.py "path/to/audio/folder" --voices "D:\my-voices"
```

Available arguments:

- Directory path: First positional argument (required)
- `--language`: Input language (optional, defaults to auto-detection)
- `--accept`: Auto-accept file list without confirmation prompt (optional)
- `--align`: Run word-level alignment (wav2vec2), adds per-word timings (optional, off by default)
- `--no-diarize`: Disable speaker diarization for this run (optional; diarization is on by default when `HF_TOKEN` is set)
- `--voices <dir>`: Directory of enrolled reference voices for named speaker ID (optional; default: env `VOICES_DIR`, else `voices/` next to the script if present, else disabled)

## 🔧 Requirements

- Python 3.8+
- CUDA-compatible GPU (optional, for faster processing) with **CUDA 12.8** drivers (NVIDIA driver **≥ 550.x**)
- For GPU on host (non-Docker): **cuDNN 9** + **cuBLAS for CUDA 12** must be available to CTranslate2 (see Installation step 2)
- Required Python packages (see `requirements.txt`)
- A [HuggingFace](https://huggingface.co) account + read token if you want speaker diarization / named ID (see Installation step 4)

## 🚀 Installation

1. Clone the repository:

```bash
git clone https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file.git
```

2. Install PyTorch with CUDA 12.8 support **first**, from the PyTorch cu128 index (never install torch from plain PyPI — it silently installs the CPU build and breaks GPU acceleration):

```bash
pip install "torch~=2.8.0" "torchaudio~=2.8.0" --index-url https://download.pytorch.org/whl/cu128
```

3. Install the rest of the requirements:

```bash
pip install -r requirements.txt
```

> **GPU users:** `requirements.txt` already pulls `nvidia-cublas-cu12` and `nvidia-cudnn-cu12==9.*` so CTranslate2 can find cuDNN 9 + cuBLAS at runtime. On Linux you may also need `LD_LIBRARY_PATH` pointing at the wheel install dirs (the Dockerfile does this automatically).

4. (Optional, required for diarization/named speaker ID) Set up a HuggingFace token:

   1. Create an account at [huggingface.co](https://huggingface.co)
   2. Accept the model licenses at **all three** of:
      - [`pyannote/speaker-diarization-3.1`](https://hf.co/pyannote/speaker-diarization-3.1)
      - [`pyannote/segmentation-3.0`](https://hf.co/pyannote/segmentation-3.0)
      - [`pyannote/speaker-diarization-community-1`](https://hf.co/pyannote/speaker-diarization-community-1) — not actually used for transcription, but `pyannote-audio` 4.x unconditionally fetches a calibration file from this repo when loading the 3.1 pipeline, and 403s without it. Harmless to accept.
   3. Generate a read token at [hf.co/settings/tokens](https://hf.co/settings/tokens)
   4. Copy `.env.example` to `.env` and set `HF_TOKEN=<your token>` — this is picked up automatically (`python-dotenv`) for CLI/host runs; Docker uses its own `env_file:` mechanism instead.

   Without a token, diarization/named ID gracefully skip — plain transcription still works.

5. Run the script:

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

## 🗣️ Speaker Diarization & Named Identification

With `HF_TOKEN` set (see Installation step 4), diarization is **on by default**: each segment is prefixed with a speaker label.

```
[12.34s - 15.78s] SPEAKER_01: hola, ¿cómo estás?
```

### Enroll real names

Drop one clean 10–30 s reference sample per person into a `voices/` folder (next to the script, or point `--voices`/`VOICES_DIR` elsewhere). The filename (without extension) becomes the name used in the output — capitalization is up to you:

```
voices/
  carlos.wav
  Maria.mp3
```

Supported formats: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac` (same set as the main transcription path).

With voices enrolled, matching segments show the real name instead of `SPEAKER_xx`:

```
[12.34s - 15.78s] carlos: hola, ¿cómo estás?
[16.02s - 18.40s] SPEAKER_02: bien, ¿y vos?
```

`SPEAKER_02` above stayed anonymous because no matching voice was enrolled — that's expected, not a bug.

**How it works:** each enrolled sample is embedded with the same voice-embedding model the diarization pipeline already uses internally (`pyannote/wespeaker-voxceleb-resnet34-LM` — no extra downloads). At transcription time, each diarized speaker's embedding is compared against the registry by cosine similarity; the best match above `SPEAKER_MATCH_THRESHOLD` (default `0.5`, tune via `.env`) wins. Below threshold, the anonymous `SPEAKER_xx` label is kept. Two diarized clusters can legitimately map to the same name (diarization sometimes splits one person into multiple clusters).

**Reference sample quality matters**: 10–30 s, one speaker only, minimal background noise. Noisy or short samples lead to missed or false matches — tune `SPEAKER_MATCH_THRESHOLD` based on the per-speaker similarity scores logged at match time.

`voices/` is git-ignored — personal voice samples should never be committed.

**Known limitation**: diarization quality degrades on heavy overlapping speech/crosstalk (a whisperX/pyannote limitation, not specific to this project).

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

Edit `.env` and set your API key (and `HF_TOKEN` if you want diarization):

```env
API_KEY=change-me-to-a-random-secret
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
```

> **Note:** `/health` is public and does not require the API key.

For named speaker ID in Docker, drop reference samples into `./voices` on the host — `docker-compose.yml` mounts it to `/app/voices` and sets `VOICES_DIR=/app/voices` automatically. The registry loads once at container startup; there is no per-request upload of voice samples. (Diarization and named ID are verified end-to-end via both the CLI and the API itself — a live `POST /transcribe` returned name-labeled segments. The Docker/WireGuard container stack wraps that same API but hasn't been separately re-verified since these features landed.)

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
  "jobs_total": 0,
  "diarization_enabled": true,
  "speaker_id_enabled": true
}
```

> `"device": "cuda"` confirms GPU passthrough is working. If you see `"cpu"`, check your NVIDIA Container Toolkit / WSL2 GPU passthrough setup.

### 5. Submit a transcription job

```bash
curl -X POST http://10.0.0.5:8000/transcribe \
  -H "X-API-Key: your-api-key" \
  -F "file=@recording.mp3" \
  -F "language=en" \
  -F "align=false" \
  -F "diarize=true"
```

Supported formats: `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`

`language` is optional — omit it for automatic language detection. `align` and `diarize` are optional booleans (default: no alignment; diarization follows the server's `ENABLE_DIARIZATION` env default). Named speaker ID has no per-request field — it's driven by the `voices/` folder mounted at startup (see below).

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

## 🔑 License

- [GPL-3.0 license](https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file/blob/main/LICENSE)

## About developer

Visit my web [Carlos Ochoa](https://carlos8a.com?ref=gh)

---

**Note:** If you encounter any issues with the project, please report them [here](https://github.com/CarlosUlisesOchoa/Whisper-AI-transcription-audio-to-text-file/issues). Contributions are welcome!
