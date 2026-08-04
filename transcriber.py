import logging
import os
import re
from collections import Counter

import numpy as np
import torch
import whisperx
from dotenv import load_dotenv
from whisperx.diarize import DiarizationPipeline

import speaker_registry
from naming import AUDIO_EXTENSIONS, ACCENTED_VOWEL_TRANSLATION, sanitize_filename

load_dotenv()

logger = logging.getLogger(__name__)

# --- Configuration from environment ---
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
WHISPER_BATCH_SIZE = int(os.environ.get("WHISPER_BATCH_SIZE", 16))
HF_TOKEN = os.environ.get("HF_TOKEN", "")
ENABLE_DIARIZATION = os.environ.get("ENABLE_DIARIZATION", "true").lower() == "true"
SPEAKER_MATCH_THRESHOLD = float(os.environ.get("SPEAKER_MATCH_THRESHOLD", 0.5))
AUTO_ENROLL_UNKNOWN = os.environ.get("AUTO_ENROLL_UNKNOWN", "false").lower() == "true"
ENROLL_MIN_SECONDS = float(os.environ.get("ENROLL_MIN_SECONDS", 10))
ENROLL_MAX_SECONDS = float(os.environ.get("ENROLL_MAX_SECONDS", 30))

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


def _align_segments(segments: list[dict], audio, language_code: str) -> None:
    """Run wav2vec2 alignment and attach a 'words' array to each segment in place. Raises on failure.

    whisperx.align() re-segments internally by sentence, so its output segment list is not
    1:1 with our input segments (different count/order) — words must be bucketed back onto
    the original segments by timestamp via align_result['word_segments'], not by list index.
    Each word's aligned start/end is mathematically bounded within its source segment's own
    [start, end] window (alignment runs on that segment's audio slice), so a chronological
    walk against segment boundaries is exact, not a heuristic.
    """
    model_a, metadata = _get_align_model(language_code)
    align_result = whisperx.align(
        segments, model_a, metadata, audio, get_device(),
        return_char_alignments=False,
    )
    for seg in segments:
        seg["words"] = []

    seg_idx = 0
    for word in align_result.get("word_segments", []):
        word_start = word.get("start")
        if word_start is not None:
            while seg_idx + 1 < len(segments) and word_start >= segments[seg_idx + 1]["start"]:
                seg_idx += 1
        segments[seg_idx]["words"].append(word)


def _split_segments_by_word_speaker(segments: list[dict]) -> list[dict]:
    """Split segments at word-speaker-change boundaries so short interjections get their own line."""
    output = []
    for seg in segments:
        words = seg.get("words")
        if not words:
            output.append(seg)
            continue

        distinct_speakers = {w["speaker"] for w in words if w.get("speaker")}
        if len(distinct_speakers) <= 1:
            output.append(seg)
            continue

        groups: list[dict] = []
        for word in words:
            speaker = word.get("speaker")
            if speaker is None or "start" not in word:
                # Unlabeled/unaligned words never open a new group.
                if groups:
                    groups[-1]["words"].append(word)
                else:
                    groups.append({"speaker": None, "words": [word]})
                continue

            if groups and groups[-1]["speaker"] in (speaker, None):
                groups[-1]["speaker"] = speaker
                groups[-1]["words"].append(word)
            else:
                groups.append({"speaker": speaker, "words": [word]})

        for gi, group in enumerate(groups):
            group_words = group["words"]
            if gi == 0:
                start = seg["start"]
            else:
                start = next((w["start"] for w in group_words if "start" in w), seg["start"])
            if gi == len(groups) - 1:
                end = seg["end"]
            else:
                end = next((w["end"] for w in reversed(group_words) if "end" in w), seg["end"])

            output.append({
                "start": start,
                "end": end,
                "text": " ".join(w["word"] for w in group_words).strip(),
                "speaker": group["speaker"],
                "words": group_words,
            })

    return output


def transcribe_audio(file_path, language=None, align=False, diarize=None, voices_dir=None, enroll_unknown=None):
    """Transcribe an audio file and return a result dict with segments and text.

    Args:
        file_path: Path to the audio file.
        language: Optional language code (e.g. 'en', 'es'). None = auto-detect.
        align: If True, run word-level alignment and add 'words' to each segment.
        diarize: If True/False, override ENABLE_DIARIZATION env default. None = use env.
        voices_dir: Optional directory of enrolled reference voices for named speaker
            identification. None = feature disabled (SPEAKER_xx labels only).
        enroll_unknown: If True/False, override AUTO_ENROLL_UNKNOWN env default. None = use env.
            Auto-captures a voice sample for any diarized speaker unmatched in voices_dir.
    """
    if diarize is None:
        diarize = ENABLE_DIARIZATION
    if enroll_unknown is None:
        enroll_unknown = AUTO_ENROLL_UNKNOWN

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
        "enrolled_speakers": [],
    }

    words_requested = align
    aligned = False

    # Optional: word-level alignment
    if align:
        try:
            _align_segments(result["segments"], audio, detected_language)
            aligned = True
        except Exception as e:
            logger.warning("Word alignment skipped: %s", e)

    # Optional: speaker diarization
    if diarize:
        if not HF_TOKEN:
            logger.warning("Diarization skipped: HF_TOKEN not set.")
        else:
            try:
                if not aligned:
                    # Word-level attribution needs per-word timings even if the caller
                    # didn't ask for --align; fall back to segment-level attribution on failure.
                    try:
                        _align_segments(result["segments"], audio, detected_language)
                        aligned = True
                    except Exception as e:
                        logger.warning("Word-level alignment for speaker attribution skipped: %s", e)

                pipeline = _get_diarize_pipeline()
                diarize_segments, speaker_embeddings = pipeline(audio, return_embeddings=True)
                whisperx.assign_word_speakers(diarize_segments, result)

                result["segments"] = _split_segments_by_word_speaker(result["segments"])
                if not words_requested:
                    for seg in result["segments"]:
                        seg.pop("words", None)

                if voices_dir and speaker_embeddings:
                    registry = _get_speaker_registry(voices_dir)
                    name_map = (
                        speaker_registry.match_speakers(speaker_embeddings, registry, SPEAKER_MATCH_THRESHOLD)
                        if registry else {}
                    )

                    if enroll_unknown:
                        unmatched = sorted(label for label in speaker_embeddings if label not in name_map)
                        if unmatched:
                            os.makedirs(voices_dir, exist_ok=True)
                        newly_enrolled: dict = {}
                        for label in unmatched:
                            try:
                                vec = np.asarray(speaker_embeddings[label]).reshape(-1)
                                if not np.isfinite(vec).all():
                                    logger.warning("Enrollment skipped for %s: non-finite embedding.", label)
                                    continue

                                # Within-run dedup: diarization sometimes splits one person
                                # into multiple clusters — don't enroll the same voice twice.
                                if newly_enrolled:
                                    dup_name, dup_score = speaker_registry.best_match(vec, newly_enrolled)
                                    if dup_name is not None and dup_score >= SPEAKER_MATCH_THRESHOLD:
                                        name_map[label] = dup_name
                                        continue

                                speaker_turns = diarize_segments.loc[
                                    diarize_segments["speaker"] == label, ["start", "end"]
                                ].values.tolist()
                                other_turns = diarize_segments.loc[
                                    diarize_segments["speaker"] != label, ["start", "end"]
                                ].values.tolist()
                                sample = speaker_registry.extract_speaker_sample(
                                    audio, speaker_turns, other_turns,
                                    min_seconds=ENROLL_MIN_SECONDS, max_seconds=ENROLL_MAX_SECONDS,
                                )
                                if sample is None:
                                    logger.info(
                                        "Enrollment skipped for %s: less than %.0fs usable speech.",
                                        label, ENROLL_MIN_SECONDS,
                                    )
                                    continue

                                new_name = speaker_registry.next_unknown_name(voices_dir)
                                new_vec = speaker_registry.enroll_speaker(sample, voices_dir, new_name, HF_TOKEN)
                                registry[new_name] = new_vec
                                newly_enrolled[new_name] = new_vec
                                name_map[label] = new_name
                                result["enrolled_speakers"].append(new_name)
                                logger.info("Enrolled new speaker %s as %s", label, new_name)
                            except Exception as e:
                                logger.warning("Enrollment skipped for %s: %s", label, e)

                    if name_map:
                        for seg in result["segments"]:
                            spk = seg.get("speaker")
                            if spk in name_map:
                                seg["speaker"] = name_map[spk]
                            if words_requested:
                                for w in seg.get("words", []):
                                    wspk = w.get("speaker")
                                    if wspk in name_map:
                                        w["speaker"] = name_map[wspk]
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
