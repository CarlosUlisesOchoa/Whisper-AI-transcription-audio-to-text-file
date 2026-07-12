import logging
import os

import numpy as np
import torch
import whisperx
from pyannote.audio import Inference, Model

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac"}

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
        vec = np.asarray(embedding).reshape(-1)
        best_name, best_score = None, -1.0
        for name, ref_vec in registry.items():
            score = _cosine_similarity(vec, ref_vec)
            if score > best_score:
                best_name, best_score = name, score
        logger.info("Speaker %s best match: %s (score=%.3f)", speaker_label, best_name, best_score)
        if best_name is not None and best_score >= threshold:
            mapping[speaker_label] = best_name

    return mapping
