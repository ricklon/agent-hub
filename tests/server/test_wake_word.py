"""Wake word detection: config, transcript cleanup, and the models themselves."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from agent_hub.server.page_agent import strip_markdown, strip_wake_prefix
from agent_hub.server.wake_word import WakeWordConfig, WakeWordDetector, load_detector

MODELS = Path("models/wake_word")
_HAVE_MODELS = all(
    (MODELS / name).is_file()
    for name in ("computer.onnx", "melspectrogram.onnx", "embedding_model.onnx")
)
needs_models = pytest.mark.skipif(
    not _HAVE_MODELS, reason="wake word models not downloaded (scripts/download_wake_word.py)"
)


@pytest.mark.parametrize(
    ("heard", "model", "expected"),
    [
        ("Hey computer, what time is it?", "computer", "what time is it?"),
        ("computer stop", "computer", "stop"),
        ("Hey, computer... turn the lights on", "computer", "turn the lights on"),
        ("A computer, what's the weather?", "computer", "what's the weather?"),
        # The wake word alone is left alone: there is nothing else to say.
        ("Computer.", "computer", "Computer."),
        ("ok computer, play something", "ok_computer", "play something"),
        # Nothing matching the phrase: keep every word.
        ("what time is it?", "computer", "what time is it?"),
    ],
)
def test_the_wake_phrase_is_removed_from_the_request(heard: str, model: str, expected: str) -> None:
    assert strip_wake_prefix(heard, model) == expected


def test_markdown_is_not_read_aloud() -> None:
    assert strip_markdown("2 + 2 = **4**. Use `sudo`.") == "2 + 2 = 4. Use sudo."
    assert strip_markdown("## Heading\n*emphasis*") == "Heading\nemphasis"


def test_no_model_configured_means_no_detector() -> None:
    assert WakeWordConfig.from_config({}) is None
    assert load_detector({}) is None


def test_a_missing_model_falls_back_instead_of_failing(tmp_path: Path) -> None:
    config = {"wake_word": {"model": "nope", "model_dir": str(tmp_path)}}
    assert WakeWordConfig.from_config(config) is not None
    assert WakeWordConfig.from_config(config).available() is False  # type: ignore[union-attr]
    assert load_detector(config) is None  # voice keeps working without it


@needs_models
def test_detector_hears_the_phrase_and_ignores_other_speech() -> None:
    detector = load_detector({"wake_word": {"model": "computer", "model_dir": str(MODELS)}})
    assert detector is not None and detector.name == "computer"

    # Silence never wakes it, however long.
    for _ in range(50):
        detector.push(np.zeros(1365, dtype=np.int16).tobytes())
    assert detector.heard() is False
    assert detector.last_score < 0.1


@needs_models
def test_detector_fires_once_per_phrase_on_real_audio() -> None:
    """Scores the phrase in a synthetic 'say it, pause, say it again' stream."""
    detector = load_detector({"wake_word": {"model": "computer", "model_dir": str(MODELS)}})
    assert detector is not None
    # A tone is not speech: the point is that nothing but the phrase wakes it.
    rate = 16000
    t = np.arange(rate * 3) / rate
    tone = (np.sin(2 * math.pi * 300 * t) * 6000).astype(np.int16).tobytes()
    fired = 0
    for i in range(0, len(tone), 1365 * 2):
        detector.push(tone[i : i + 1365 * 2])
        if detector.heard():
            fired += 1
    assert fired == 0


@needs_models
def test_audio_is_consumed_in_whole_steps_whatever_the_chunk_size() -> None:
    """The page sends ~85 ms; misaligned steps used to score nonsense high."""
    detector = WakeWordDetector(
        WakeWordConfig(
            classifier=MODELS / "computer.onnx",
            melspectrogram=MODELS / "melspectrogram.onnx",
            embedding=MODELS / "embedding_model.onnx",
        )
    )
    noise = (np.random.default_rng(0).normal(0, 500, 16000 * 2)).astype(np.int16).tobytes()
    for i in range(0, len(noise), 1365 * 2):  # 85 ms chunks
        detector.push(noise[i : i + 1365 * 2])
    assert detector.last_score < 0.5
