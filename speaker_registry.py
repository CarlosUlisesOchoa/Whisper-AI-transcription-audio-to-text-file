import logging
import os
import re

import numpy as np
import soundfile as sf
import torch
import whisperx
from pyannote.audio import Inference, Model

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
SAMPLE_RATE = 16000

_UNKNOWN_NAME_PATTERN = re.compile(r"^unknown-(\d+)$")

_inference = None


def _get_inference(hf_token: str) -> Inference:
    """Lazily load the wespeaker embedding model (same one pyannote/speaker-diarization-3.1 uses)."""
    global _inference
    if _inference is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", token=hf_token)
        _inference = Inference(model, window="whole", device=torch.device(device))
    return _inference


def _embed(path: str, hf_token: str) -> np.ndarray:
    audio = whisperx.load_audio(path)
    inference = _get_inference(hf_token)
    embedding = inference({"waveform": torch.from_numpy(audio)[None, :], "sample_rate": 16000})
    return np.asarray(embedding).reshape(-1)


def load_registry(voices_dir: str) -> dict[str, np.ndarray]:
    """Scan voices_dir for reference audio samples and embed each into name -> vector.

    Name is the filename stem (verbatim, case preserved). Missing/empty folder
    or missing HF_TOKEN silently disables the feature (returns {}).
    """
    if not voices_dir or not os.path.isdir(voices_dir):
        return {}

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        logger.warning("Speaker registry skipped: HF_TOKEN not set.")
        return {}

    registry: dict[str, np.ndarray] = {}
    for filename in sorted(os.listdir(voices_dir)):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in AUDIO_EXTENSIONS:
            continue
        name = os.path.splitext(filename)[0]
        path = os.path.join(voices_dir, filename)
        try:
            registry[name] = _embed(path, hf_token)
        except Exception as e:
            logger.warning("Skipping voice sample %s: %s", filename, e)

    return registry


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def best_match(embedding, registry: dict[str, np.ndarray]) -> tuple[str | None, float]:
    """Return (best_name, best_score) for embedding against registry entries."""
    vec = np.asarray(embedding).reshape(-1)
    best_name, best_score = None, -1.0
    for name, ref_vec in registry.items():
        score = _cosine_similarity(vec, ref_vec)
        if score > best_score:
            best_name, best_score = name, score
    return best_name, best_score


def match_speakers(
    speaker_embeddings: dict[str, list],
    registry: dict[str, np.ndarray],
    threshold: float,
) -> dict[str, str]:
    """Map SPEAKER_xx labels to registry names by cosine similarity.

    Best match >= threshold wins; below threshold keeps the SPEAKER_xx label.
    Two diarized speakers may map to the same name (diarization sometimes
    splits one person into multiple clusters) — that is expected.
    """
    mapping: dict[str, str] = {}
    if not registry or not speaker_embeddings:
        return mapping

    for speaker_label, embedding in speaker_embeddings.items():
        best_name, best_score = best_match(embedding, registry)
        logger.info("Speaker %s best match: %s (score=%.3f)", speaker_label, best_name, best_score)
        if best_name is not None and best_score >= threshold:
            mapping[speaker_label] = best_name

    return mapping


def next_unknown_name(voices_dir: str) -> str:
    """Return the next unknown-NN placeholder name (zero-padded to 2 digits) for voices_dir."""
    max_n = 0
    if voices_dir and os.path.isdir(voices_dir):
        for filename in os.listdir(voices_dir):
            match = _UNKNOWN_NAME_PATTERN.match(os.path.splitext(filename)[0])
            if match:
                max_n = max(max_n, int(match.group(1)))
    return f"unknown-{max_n + 1:02d}"


def _turns_overlap_any(turn: tuple[float, float], other_turns: list[tuple[float, float]]) -> bool:
    start, end = turn
    return any(start < o_end and end > o_start for o_start, o_end in other_turns)


def extract_speaker_sample(
    audio: np.ndarray,
    turns: list[tuple[float, float]],
    other_turns: list[tuple[float, float]] | None = None,
    min_seconds: float = 10,
    max_seconds: float = 30,
) -> np.ndarray | None:
    """Splice a speaker's diarized turns into one voice sample for enrollment.

    `audio` is the 16kHz mono float32 array already loaded by transcribe_audio.
    `turns` are (start, end) tuples for one speaker; `other_turns` are every
    other speaker's turns, used to prefer overlap-free (cleaner) speech.

    Prefers turns that don't overlap other speakers' turns; falls back to all
    turns if the overlap-free subset is below min_seconds. Takes the longest
    turns first, up to max_seconds. Returns None if usable speech never
    reaches min_seconds. Continuity is not required — splice cuts are
    harmless since the clip only feeds a speaker-embedding model, never ASR.
    """
    if not turns:
        return None

    candidate_turns = turns
    if other_turns:
        non_overlapping = [t for t in turns if not _turns_overlap_any(t, other_turns)]
        if sum(end - start for start, end in non_overlapping) >= min_seconds:
            candidate_turns = non_overlapping

    ordered = sorted(candidate_turns, key=lambda t: t[1] - t[0], reverse=True)

    selected = []
    total = 0.0
    for turn in ordered:
        if total >= max_seconds:
            break
        selected.append(turn)
        total += turn[1] - turn[0]

    if total < min_seconds:
        return None

    selected.sort(key=lambda t: t[0])
    chunks = [audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)] for start, end in selected]
    sample = np.concatenate(chunks)
    return sample[: int(max_seconds * SAMPLE_RATE)]


def enroll_speaker(sample: np.ndarray, voices_dir: str, name: str, hf_token: str) -> np.ndarray:
    """Write sample as voices_dir/name.wav and return its freshly computed embedding.

    Re-embeds the written file (rather than reusing the diarization cluster
    embedding) so the in-memory cache value matches exactly what a future
    load_registry() run will compute for the same file.
    """
    os.makedirs(voices_dir, exist_ok=True)
    path = os.path.join(voices_dir, f"{name}.wav")
    sf.write(path, sample, SAMPLE_RATE)
    return _embed(path, hf_token)
