import os
import re
import unicodedata
from collections import Counter

import torch
import whisper

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}

_model = None


# Safer defaults for long-form audio; these reduce repetition loops on silence/noise.
BASE_TRANSCRIBE_OPTIONS = {
    "task": "transcribe",
    "temperature": 0.0,
    "condition_on_previous_text": False,
    "compression_ratio_threshold": 2.0,
    "logprob_threshold": -1.0,
    "no_speech_threshold": 0.45,
}

# Retry settings if the first pass looks like a hallucination loop.
RETRY_TRANSCRIBE_OPTIONS = {
    "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    "beam_size": 5,
    "best_of": 5,
    "suppress_tokens": "",
}

ACCENTED_VOWEL_TRANSLATION = str.maketrans("áéíóú", "aeiou")


def sanitize_filename(filename):
    """Sanitize a filename: lowercase, replace unknown characters with dashes, collapse dashes and transliterate accented vowels."""
    base_name = os.path.splitext(filename)[0]
    extension = os.path.splitext(filename)[1]

    # Normalize first so composed and decomposed accents are treated the same.
    sanitized = unicodedata.normalize("NFC", base_name.lower())
    sanitized = sanitized.translate(ACCENTED_VOWEL_TRANSLATION)
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


def _normalize_segment_text(text):
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    normalized = re.sub(r"[^\w]", "", normalized)
    return normalized


def _longest_consecutive_run(items):
    longest = 0
    current = 0
    previous = None

    for item in items:
        if item and item == previous:
            current += 1
        else:
            current = 1
            previous = item
        if current > longest:
            longest = current

    return longest


def _looks_like_hallucination_loop(segments):
    normalized = [_normalize_segment_text(s.get("text", "")) for s in segments]
    normalized = [text for text in normalized if text]

    if len(normalized) < 12:
        return False

    most_common_text, frequency = Counter(normalized).most_common(1)[0]
    dominant_ratio = frequency / len(normalized)
    longest_run = _longest_consecutive_run(normalized)

    # Typical failure signature: very short token (e.g. "y") repeated for most windows.
    if len(most_common_text) <= 3 and dominant_ratio >= 0.55 and len(normalized) >= 20:
        return True

    return longest_run >= 10


def _build_transcribe_options(language=None, retry=False):
    options = dict(BASE_TRANSCRIBE_OPTIONS)
    if retry:
        options.update(RETRY_TRANSCRIBE_OPTIONS)

    options["fp16"] = get_device() == "cuda"
    if language:
        options["language"] = language

    return options


def transcribe_audio(file_path, language=None):
    """Transcribe an audio file and return the Whisper result dict."""
    model = get_model()
    result = model.transcribe(file_path, **_build_transcribe_options(language=language, retry=False))

    if _looks_like_hallucination_loop(result.get("segments", [])):
        retry_result = model.transcribe(file_path, **_build_transcribe_options(language=language, retry=True))
        if _looks_like_hallucination_loop(retry_result.get("segments", [])):
            raise RuntimeError(
                "Whisper detected a repetition loop (hallucination). "
                "The audio likely has long silence/noise segments; try cleaner audio or split by voice activity."
            )
        return retry_result

    return result


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
