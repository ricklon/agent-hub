"""Run browser voice regressions when the optional Node runtime is available."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_browser_voice_playback_and_cancellation() -> None:
    """Execute the served JavaScript with deterministic media and socket doubles."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the browser JavaScript regression harness")
    root = Path(__file__).resolve().parents[2]
    subprocess.run(
        [node, str(root / "tests/browser/page_voice.cjs")],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_browser_persona_voice_speaks_sentence_by_sentence() -> None:
    """The persona voice starts after the first sentence, not after the whole reply."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is needed for the browser JavaScript regression harness")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, str(root / "tests/browser/page_speech.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
