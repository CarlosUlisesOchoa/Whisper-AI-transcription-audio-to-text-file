import os
import re
import torch
import whisper

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}

_model = None


def sanitize_filename(filename):
    """Sanitize a filename: lowercase, replace non-alnum with dashes, collapse dashes."""
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]

    sanitized = base_name.lower()
    sanitized = re.sub(r'[^a-z0-9-_]', '-', sanitized)
    sanitized = re.sub(r'-+', '-', sanitized)
    sanitized = sanitized.strip('-')

    return sanitized + extension


def get_device():
    """Return 'cuda' if available, else 'cpu'."""
    return "cuda" if torch.cuda.is_available() else "cpu"


def get_model():
    """Load and return the Whisper model (singleton — loaded once, reused)."""
    global _model
    if _model is None:
        device = get_device()
        _model = whisper.load_model("medium", device=device)
    return _model


def transcribe_audio(file_path, language=None):
    """Transcribe an audio file and return the Whisper result dict."""
    model = get_model()
    return model.transcribe(file_path, language=language)


def format_transcription(filename, segments):
    """Format transcription segments into the standard output string."""
    lines = []
    lines.append('=' * 50)
    lines.append(f"filename:{filename}")
    lines.append('=' * 50)
    for segment in segments:
        start = segment["start"]
        end = segment["end"]
        text = segment["text"]
        lines.append(f"[{start:.2f}s - {end:.2f}s] {text}")
    return '\n'.join(lines) + '\n'
