FROM nvidia/cuda:12.8.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# Install Python 3, pip, and FFmpeg (whisperX's load_audio shells out to the ffmpeg CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch with CUDA 12.8 support first (large layer, cached separately)
RUN pip3 install --no-cache-dir "torch~=2.8.0" "torchaudio~=2.8.0" --index-url https://download.pytorch.org/whl/cu128

# whisperX (CTranslate2 backend) requires cuDNN 9 + cuBLAS for CUDA 12 — install from pip wheels
RUN pip3 install --no-cache-dir "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*"

# Set LD_LIBRARY_PATH so CTranslate2 can find the cuDNN 9 libs
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.10/dist-packages/nvidia/cublas/lib:/usr/local/lib/python3.10/dist-packages/nvidia/cudnn/lib

# Install remaining Python dependencies
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy application code
COPY transcriber.py speaker_registry.py security.py api.py audio_to_text_file.py ./

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
