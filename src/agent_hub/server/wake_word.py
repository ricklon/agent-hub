"""Wake word detection on a live 16 kHz PCM stream (openWakeWord models).

Transcribing everything and matching the wake word as text does not work: a
small ASR hears "toaster" as "Tombster", "Towster" or "Toast stir", so the
agent stays asleep. A wake word model listens for the sound of the phrase
instead, and only then does anything else run.

Three ONNX models, run in sequence on each 80 ms of audio:

    audio → melspectrogram → speech embedding → per-phrase classifier → score

They are openWakeWord's (https://github.com/dscripka/openWakeWord); this is a
small streaming implementation of its feature pipeline so the hub needs only
onnxruntime and numpy, which it already has, rather than the package's scipy,
scikit-learn and tflite-runtime.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger

_TAG = "wake_word"

# The feature pipeline's fixed shapes, from openWakeWord.
_SAMPLE_RATE = 16000
_STEP_SAMPLES = 1280  # 80 ms: one embedding per step
_MEL_FRAMES_PER_STEP = 8
_MEL_WINDOW = 76  # mel frames per embedding
_MEL_BINS = 32
_EMBEDDINGS_FOR_SCORE = 16  # what a classifier head looks at (~1.4 s)
_MEL_CONTEXT_SAMPLES = 480  # overlap kept so streamed mel frames match a whole-clip one
_FEATURE_HISTORY = 120

DEFAULT_THRESHOLD = 0.5
# After a detection, ignore further ones for this long: one phrase otherwise
# scores above the threshold on several consecutive frames.
DEFAULT_REFRACTORY_S = 2.0


@dataclass(frozen=True)
class WakeWordConfig:
    """Where the models are and how sure the detector must be."""

    classifier: Path
    melspectrogram: Path
    embedding: Path
    threshold: float = DEFAULT_THRESHOLD

    @classmethod
    def from_config(
        cls, config: dict[str, Any], classifier: str | None = None
    ) -> WakeWordConfig | None:
        """Read ``wake_word`` settings, or None when no classifier is configured."""
        section = config.get("wake_word") or {}
        model_dir = Path(str(section.get("model_dir") or "models/wake_word"))
        name = classifier or str(section.get("model") or "")
        if not name:
            return None
        path = Path(name) if "/" in name else model_dir / f"{name}.onnx"
        return cls(
            classifier=path,
            melspectrogram=model_dir / "melspectrogram.onnx",
            embedding=model_dir / "embedding_model.onnx",
            threshold=float(section.get("threshold", DEFAULT_THRESHOLD)),
        )

    def available(self) -> bool:
        """True when every model file needed is present."""
        return all(p.is_file() for p in (self.classifier, self.melspectrogram, self.embedding))


class WakeWordDetector:
    """Scores a stream of PCM for one wake phrase.

    Feed audio with :meth:`push` as it arrives (any chunk size); it returns the
    score of the most recent 80 ms step, and True from :meth:`heard` when that
    crosses the threshold outside the refractory window.
    """

    def __init__(self, config: WakeWordConfig, refractory_s: float = DEFAULT_REFRACTORY_S) -> None:
        """Load the models. Raises if a file is missing or unreadable."""
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        providers = ["CPUExecutionProvider"]
        self._mel = ort.InferenceSession(str(config.melspectrogram), options, providers=providers)
        self._embed = ort.InferenceSession(str(config.embedding), options, providers=providers)
        self._head = ort.InferenceSession(str(config.classifier), options, providers=providers)
        self._head_input = self._head.get_inputs()[0].name
        self.name = config.classifier.stem
        self.threshold = config.threshold
        self._refractory_samples = int(refractory_s * _SAMPLE_RATE)
        self._reset()

    def _reset(self) -> None:
        self._raw: deque[int] = deque(maxlen=_SAMPLE_RATE * 10)
        self._mel_buffer = np.ones((_MEL_WINDOW, _MEL_BINS), dtype=np.float32)
        self._features = np.zeros((_EMBEDDINGS_FOR_SCORE, 96), dtype=np.float32)
        self._step_buffer = np.zeros(0, dtype=np.int16)
        self._samples_seen = 0
        self._last_detection_at = -(10**9)
        self.last_score = 0.0
        # Prime with silence, as openWakeWord does: the classifier reads the
        # last 16 embeddings, and starting from zeros instead of embedded
        # silence makes the first ~1.3 s of any stream score too high.
        silence = np.zeros(_STEP_SAMPLES, dtype=np.int16)
        for _ in range(_EMBEDDINGS_FOR_SCORE + _MEL_WINDOW // _MEL_FRAMES_PER_STEP):
            self._consume_step(silence)
        self._raw.clear()
        self._samples_seen = 0
        self.last_score = 0.0

    def reset(self) -> None:
        """Forget the stream so far (a new listening session)."""
        self._reset()

    def push(self, pcm: bytes) -> float:
        """Feed raw int16 LE PCM at 16 kHz; returns the newest score (0..1).

        Any chunk size is accepted, but audio is consumed in exact 80 ms steps:
        the mel frames and the embeddings advance together, and a partial step
        waits for the next push. Letting a remainder through (the page sends
        ~85 ms) drifts that alignment and eventually scores nonsense high.
        """
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
        if samples.size == 0:
            return self.last_score
        self._samples_seen += samples.size
        self._step_buffer = np.concatenate((self._step_buffer, samples))
        while self._step_buffer.size >= _STEP_SAMPLES:
            step, self._step_buffer = (
                self._step_buffer[:_STEP_SAMPLES],
                self._step_buffer[_STEP_SAMPLES:],
            )
            self._consume_step(step)
        return self.last_score

    def _consume_step(self, step: np.ndarray[Any, Any]) -> None:
        """One 80 ms step: mel frames, one embedding, one score."""
        self._raw.extend(step.tolist())
        window = np.array(
            list(self._raw)[-(_STEP_SAMPLES + _MEL_CONTEXT_SAMPLES) :], dtype=np.float32
        )
        mel = self._mel.run(None, {"input": window[None, :]})[0].squeeze()
        # The ONNX melspectrogram needs this shift to match the model's training.
        mel = np.atleast_2d(mel / 10.0 + 2.0).astype(np.float32)[-_MEL_FRAMES_PER_STEP:]
        self._mel_buffer = np.vstack((self._mel_buffer, mel))[-_FEATURE_HISTORY * 8 :]

        frames = self._mel_buffer[-_MEL_WINDOW:]
        if frames.shape[0] != _MEL_WINDOW:
            return
        embedding = self._embed.run(None, {"input_1": frames[None, :, :, None]})[0].squeeze()
        self._features = np.vstack((self._features, embedding))[-_EMBEDDINGS_FOR_SCORE:]
        self.last_score = float(
            self._head.run(None, {self._head_input: self._features[None, :, :]})[0][0][0]
        )

    def heard(self) -> bool:
        """True when the last push crossed the threshold and we're not still in one."""
        if self.last_score < self.threshold:
            return False
        if self._samples_seen - self._last_detection_at < self._refractory_samples:
            return False
        self._last_detection_at = self._samples_seen
        return True


def load_detector(config: dict[str, Any], classifier: str | None = None) -> WakeWordDetector | None:
    """Build a detector from config, or None when it isn't configured or installed.

    Never raises: a missing or broken model leaves the agent on its old
    behaviour (wake word matched in the transcript) rather than killing voice.
    """
    settings = WakeWordConfig.from_config(config, classifier)
    if settings is None:
        return None
    if not settings.available():
        logger.bind(tag=_TAG).warning(
            f"Wake word models not found ({settings.classifier}); "
            "falling back to matching the transcript"
        )
        return None
    try:
        detector = WakeWordDetector(settings)
    except Exception as exc:  # noqa: BLE001 - detection is optional, voice is not
        logger.bind(tag=_TAG).warning(f"Wake word detector unavailable: {exc}")
        return None
    logger.bind(tag=_TAG).info(
        f"Wake word detector ready: {detector.name} (threshold {detector.threshold})"
    )
    return detector
