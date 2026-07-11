# Próxima migración: diarización de hablantes (whisperX)

> Guía de **resume**. Cuando quieras volver a la diarización (etiquetas `SPEAKER_00`,
> `SPEAKER_01`, quién habla cuándo), seguí esto paso a paso.
> El plan técnico profundo ya está escrito en [`feat-whisper-x.md`](./feat-whisper-x.md) —
> este doc es el "cómo lo retomo sin romper nada".

---

## Estado actual (2026-07-07)

- **Motor activo**: `faster-whisper 1.2.1` (CTranslate2). Transcribe texto, corre en GPU. Funciona.
- **Código whisperX**: YA está implementado, pero **guardado en `git stash`** (no borrado).
  - Rama: `feat/whisper-x`
  - Stash: `stash@{0}` → `"whisperx diarization migration WIP"`
  - Toca: `transcriber.py`, `api.py`, `audio_to_text_file.py`, `requirements.txt`, `Dockerfile`, `.env.example`
- **Venv**: `.venv` con `torch 2.5.1+cu121` (GPU OK). **NO tiene whisperx todavía.**

---

## Por qué se pausó

whisperX 3.8.5 exige `torch ~=2.8.0` (CUDA 12.8). El venv tiene el `torch 2.5.1+cu121`
que **funciona en GPU hoy**. Correr `pip install -r requirements.txt` a lo bruto baja
torch CPU de PyPI y **mata la GPU** (ya pasó antes en este proyecto). Por eso la instalación
hay que hacerla en orden y con el index-url correcto. Ver abajo.

---

## Pasos para retomar

### 1. Recuperar el código whisperX del stash

```bash
git status                 # confirmá que estás en feat/whisper-x y limpio
git stash list             # deberías ver: stash@{0} whisperx diarization migration WIP
git stash pop              # trae de vuelta transcriber.py + api.py + ... con whisperx
```

Si `pop` da conflicto (porque tocaste esos archivos), usá `git stash apply stash@{0}` y resolvé a mano.

### 2. Instalar torch CUDA 12.8 PRIMERO (con index-url — clave para no romper GPU)

```bash
# Dentro del venv. NO usar el PyPI default para torch.
.venv/Scripts/python.exe -m pip install "torch~=2.8.0" "torchaudio~=2.8.0" \
  --index-url https://download.pytorch.org/whl/cu128
```

Requiere driver NVIDIA que soporte CUDA 12.8 (**≥ 550.x**). Verificá con `nvidia-smi` antes.

### 3. Instalar el resto (whisperx + wheels), sin volver a tocar torch

```bash
.venv/Scripts/python.exe -m pip install whisperx==3.8.5 \
  "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"
```

> whisperx arrastra `pyannote-audio`, `transformers`, `pandas`, `numpy>=2.1.0`, `ctranslate2`.
> Si el resolver quiere bajar torch a CPU, frenalo con `--no-deps` en torch o fijá torch antes.

### 4. Token de HuggingFace (obligatorio para diarización)

La diarización usa `pyannote/speaker-diarization-3.1`, que pide licencia + token:

1. Crear cuenta en https://huggingface.co
2. Aceptar licencia en:
   - https://hf.co/pyannote/speaker-diarization-3.1
   - https://hf.co/pyannote/segmentation-3.0
3. Generar token de lectura en https://hf.co/settings/tokens
4. Ponerlo en `.env`:
   ```
   HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
   ENABLE_DIARIZATION=true
   WHISPER_MODEL=medium
   WHISPER_BATCH_SIZE=16
   ```

> Primera corrida descarga ~1 GB de modelos (whisper medium + wav2vec2 align + pyannote).
> Se cachean en `~/.cache/huggingface`. En Docker, montá un volumen para no re-descargar.

### 5. Verificar

```bash
# GPU + imports
.venv/Scripts/python.exe -c "import torch, whisperx; print('CUDA:', torch.cuda.is_available())"
# → CUDA: True

# CLI con diarización (SPEAKER_xx en cada segmento)
$env:PYTHONUTF8=1
.venv/Scripts/python.exe audio_to_text_file.py "temp" --language es --accept
```

Esperado en el `.txt`: `[12.34s - 15.78s] SPEAKER_01: texto...`

---

## Gotchas conocidos (no te olvides)

1. **Consola Windows + Unicode**: el script imprime `✓` (`✓`) y `cp1252` explota con
   `UnicodeEncodeError`. **Siempre** exportá `PYTHONUTF8=1` (o `PYTHONIOENCODING=utf-8`) antes
   de correr en PowerShell. Esto aplica también a la versión faster-whisper actual.
2. **torch CPU trap**: nunca instalar torch sin `--index-url .../cu128`. Rompe la GPU.
3. **VAD en segundos, no ms**: whisperX usa `min_duration_off=0.5` (seg), no `min_silence_duration_ms=500`.
   Semántica levemente distinta — tunear tras la primera corrida real.
4. **Alignment opcional**: `--align` es opt-in. Idiomas fuera de la lista soportada se saltan
   el alignment silenciosamente (solo warning, no crash).
5. **Sin HF_TOKEN**: el server igual bootea, `/health` reporta `diarization_enabled: false`,
   y la transcripción sigue funcionando (skip elegante, no crash).

---

## Docker (después de que el CLI ande)

El `Dockerfile` stasheado ya apunta a `nvidia/cuda:12.8.0-runtime-ubuntu22.04` + torch cu128.
Recordá: el `docker-compose.yml` es **API detrás de WireGuard** (no publica puerto local).
Sirve para transcribir remoto desde el VPS, NO para carpetas locales. Para local, seguí con el CLI.

---

## Checklist de merge (de feat-whisper-x.md)

- [ ] `git stash pop` sin conflictos
- [ ] torch cu128 instalado, `torch.cuda.is_available()` → True
- [ ] whisperx + pyannote + wheels instalados sin bajar torch a CPU
- [ ] `HF_TOKEN` en `.env` + licencias pyannote aceptadas
- [ ] CLI sin diarización: output estructuralmente igual al de hoy
- [ ] CLI con diarización: prefijos `SPEAKER_00:` / `SPEAKER_01:` visibles
- [ ] Retry de alucinación sigue funcionando
- [ ] `/health` reporta `diarization_enabled: true`
- [ ] Actualizar `README.md` + `CLAUDE.md` (sección arquitectura)
