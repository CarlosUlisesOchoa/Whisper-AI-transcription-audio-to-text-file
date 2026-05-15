import os
import re
import unicodedata
from collections import Counter

import torch
from faster_whisper import WhisperModel

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}

_model = None


# Safer defaults for long-form audio; these reduce repetition loops on silence/noise.
# Note: faster-whisper uses log_prob_threshold (not logprob_threshold) and drops fp16
# (compute_type is set at model init instead).
BASE_TRANSCRIBE_OPTIONS = {
    "task": "transcribe",
    "temperature": 0.0,
    "condition_on_previous_text": False,
    "compression_ratio_threshold": 2.0,
    "log_prob_threshold": -1.0,
    "no_speech_threshold": 0.45,
    "vad_filter": True,
    "vad_parameters": {"min_silence_duration_ms": 500},
}

# Retry settings if the first pass looks like a hallucination loop.
RETRY_TRANSCRIBE_OPTIONS = {
    "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
    "beam_size": 5,
    "best_of": 5,
    "suppress_tokens": [-1],
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
    """Load and return the WhisperModel (singleton — loaded once, reused)."""
    global _model
    if _model is None:
        device = get_device()
        compute_type = "float16" if device == "cuda" else "int8"
        _model = WhisperModel("medium", device=device, compute_type=compute_type)
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

    if language:
        options["language"] = language

    return options


def _materialize_segments(segments_iter):
    """Materialize the lazy faster-whisper generator into a list of dicts."""
    return [{"start": s.start, "end": s.end, "text": s.text} for s in segments_iter]


def transcribe_audio(file_path, language=None):
    """Transcribe an audio file and return a result dict with segments and text."""
    model = get_model()
    segments_iter, info = model.transcribe(file_path, **_build_transcribe_options(language=language, retry=False))
    segments = _materialize_segments(segments_iter)
    result = {
        "segments": segments,
        "text": " ".join(s["text"].strip() for s in segments).strip(),
        "language": info.language,
    }

    if _looks_like_hallucination_loop(result["segments"]):
        retry_iter, retry_info = model.transcribe(file_path, **_build_transcribe_options(language=language, retry=True))
        retry_segments = _materialize_segments(retry_iter)
        if _looks_like_hallucination_loop(retry_segments):
            raise RuntimeError(
                "Whisper detected a repetition loop (hallucination). "
                "The audio likely has long silence/noise segments; try cleaner audio or split by voice activity."
            )
        return {
            "segments": retry_segments,
            "text": " ".join(s["text"].strip() for s in retry_segments).strip(),
            "language": retry_info.language,
        }

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
