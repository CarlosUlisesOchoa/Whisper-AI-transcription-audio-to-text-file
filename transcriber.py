import logging
import os
import re
import unicodedata
from collections import Counter

import torch
import whisperx
from dotenv import load_dotenv
from whisperx.diarize import DiarizationPipeline

import speaker_registry

load_dotenv()

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac'}

# --- Configuration from environment ---
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
WHISPER_BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", 16))
HF_TOKEN = os.environ.get("HF_TOKEN", "")
ENABLE_DIARIZATION = os.environ.get("ENABLE_DIARIZATION", "true").lower() == "true"
SPEAKER_MATCH_THRESHOLD = float(os.environ.get("SPEAKER_MATCH_THRESHOLD", 0.5))

# --- Model singletons ---
_model = None
_retry_model = None
_align_cache: dict[str, tuple] = {}   # language_code -> (model_a, metadata)
_diarize_pipeline = None
_speaker_registry_cache: dict[str, dict] = {}   # voices_dir -> {name: embedding}

# --- ASR options (passed at load_model time, not per-transcribe) ---
ASR_OPTIONS = {
    "temperatures": [0.0],
    "compression_ratio_threshold": 2.0,
    "log_prob_threshold": -1.0,
    "no_speech_threshold": 0.45,
    "condition_on_previous_text": False,
    "suppress_tokens": [-1],
}

# Retry-tuned options merged on top of ASR_OPTIONS for the retry model singleton.
RETRY_ASR_OPTIONS = {
    "temperatures": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "beam_size": 5,
    "best_of": 5,
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
    """Load and return the whisperX model (singleton — loaded once, reused)."""
    global _model
    if _model is None:
        device = get_device()
        compute_type = "float16" if device == "cuda" else "int8"
        _model = whisperx.load_model(
            WHISPER_MODEL,
            device=device,
            compute_type=compute_type,
            asr_options=ASR_OPTIONS,
            vad_method="pyannote",
            vad_options={"min_duration_off": 0.5},
        )
    return _model


def _get_retry_model():
    """Load and return a retry-tuned whisperX model (singleton)."""
    global _retry_model
    if _retry_model is None:
        device = get_device()
        compute_type = "float16" if device == "cuda" else "int8"
        retry_asr_options = {**ASR_OPTIONS, **RETRY_ASR_OPTIONS}
        _retry_model = whisperx.load_model(
            WHISPER_MODEL,
            device=device,
            compute_type=compute_type,
            asr_options=retry_asr_options,
            vad_method="pyannote",
            vad_options={"min_duration_off": 0.5},
        )
    return _retry_model


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


def _get_align_model(language_code: str):
    """Lazily load and cache the wav2vec2 alignment model for a given language."""
    if language_code not in _align_cache:
        device = get_device()
        _align_cache[language_code] = whisperx.load_align_model(
            language_code=language_code, device=device
        )
    return _align_cache[language_code]


def _get_diarize_pipeline():
    """Lazily load the pyannote diarization pipeline (requires HF_TOKEN)."""
    global _diarize_pipeline
    if _diarize_pipeline is None:
        device = get_device()
        _diarize_pipeline = DiarizationPipeline(
            model_name="pyannote/speaker-diarization-3.1", token=HF_TOKEN, device=device
        )
    return _diarize_pipeline


def _get_speaker_registry(voices_dir: str) -> dict:
    """Lazily load and cache the voice registry for a given voices_dir."""
    if voices_dir not in _speaker_registry_cache:
        _speaker_registry_cache[voices_dir] = speaker_registry.load_registry(voices_dir)
    return _speaker_registry_cache[voices_dir]


def _materialize_segments(whisperx_result: dict) -> list[dict]:
    """Extract {start, end, text} dicts from a whisperX result."""
    return [
        {"start": s["start"], "end": s["end"], "text": s["text"]}
        for s in whisperx_result.get("segments", [])
    ]


def transcribe_audio(file_path, language=None, align=False, diarize=None, voices_dir=None):
    """Transcribe an audio file and return a result dict with segments and text.

    Args:
        file_path: Path to the audio file.
        language: Optional language code (e.g. 'en', 'es'). None = auto-detect.
        align: If True, run word-level alignment and add 'words' to each segment.
        diarize: If True/False, override ENABLE_DIARIZATION env default. None = use env.
        voices_dir: Optional directory of enrolled reference voices for named speaker
            identification. None = feature disabled (SPEAKER_xx labels only).
    """
    if diarize is None:
        diarize = ENABLE_DIARIZATION

    model = get_model()
    audio = whisperx.load_audio(file_path)
    raw = model.transcribe(audio, batch_size=WHISPER_BATCH_SIZE, language=language)
    segments = _materialize_segments(raw)
    detected_language = raw.get("language", language or "unknown")

    if _looks_like_hallucination_loop(segments):
        retry_model = _get_retry_model()
        retry_raw = retry_model.transcribe(audio, batch_size=WHISPER_BATCH_SIZE, language=language)
        retry_segments = _materialize_segments(retry_raw)
        if _looks_like_hallucination_loop(retry_segments):
            raise RuntimeError(
                "Whisper detected a repetition loop (hallucination). "
                "The audio likely has long silence/noise segments; try cleaner audio or split by voice activity."
            )
        segments = retry_segments
        detected_language = retry_raw.get("language", detected_language)

    result: dict = {
        "segments": segments,
        "language": detected_language,
    }

    # Optional: word-level alignment
    if align:
        try:
            model_a, metadata = _get_align_model(detected_language)
            align_result = whisperx.align(
                result["segments"], model_a, metadata, audio, get_device(),
                return_char_alignments=False,
            )
            for i, seg in enumerate(result["segments"]):
                aligned_seg = align_result["segments"][i] if i < len(align_result["segments"]) else {}
                if "words" in aligned_seg:
                    seg["words"] = aligned_seg["words"]
        except Exception as e:
            logger.warning("Word alignment skipped: %s", e)

    # Optional: speaker diarization
    if diarize:
        if not HF_TOKEN:
            logger.warning("Diarization skipped: HF_TOKEN not set.")
        else:
            try:
                pipeline = _get_diarize_pipeline()
                diarize_segments, speaker_embeddings = pipeline(audio, return_embeddings=True)
                diarized = whisperx.assign_word_speakers(diarize_segments, result)
                for i, seg in enumerate(result["segments"]):
                    diarized_seg = diarized["segments"][i] if i < len(diarized["segments"]) else {}
                    if "speaker" in diarized_seg:
                        seg["speaker"] = diarized_seg["speaker"]

                if voices_dir and speaker_embeddings:
                    registry = _get_speaker_registry(voices_dir)
                    if registry:
                        name_map = speaker_registry.match_speakers(
                            speaker_embeddings, registry, SPEAKER_MATCH_THRESHOLD
                        )
                        if name_map:
                            for seg in result["segments"]:
                                spk = seg.get("speaker")
                                if spk in name_map:
                                    seg["speaker"] = name_map[spk]
            except Exception as e:
                logger.warning("Diarization skipped: %s", e)

    result["text"] = " ".join(s["text"].strip() for s in result["segments"]).strip()
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
        speaker = segment.get("speaker")
        if speaker:
            lines.append(f"[{start:.2f}s - {end:.2f}s] {speaker}: {text}")
        else:
            lines.append(f"[{start:.2f}s - {end:.2f}s] {text}")
    return '\n'.join(lines) + '\n'
