"""Download the wake word models into models/wake_word.

Two feature models shared by every phrase (openWakeWord's mel-spectrogram and
speech embedding), plus one small classifier per phrase. The classifiers come
from the community collection, which is MIT-licensed; the feature models come
from openWakeWord's own release.

Run it with a phrase name to add another:  python scripts/download_wake_word.py ok_computer
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

DEST = Path("models/wake_word")
# openWakeWord's shared feature extractors.
FEATURES = {
    "melspectrogram.onnx": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx",
    "embedding_model.onnx": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx",
}
# Phrase classifiers (MIT) from fwartner/home-assistant-wakewords-collection.
_COLLECTION = (
    "https://raw.githubusercontent.com/fwartner/home-assistant-wakewords-collection/main/en"
)
PHRASES = {
    "computer": f"{_COLLECTION}/computer/computer_v2.onnx",
    "ok_computer": f"{_COLLECTION}/ok_computer/ok_computer.onnx",
    "jarvis": f"{_COLLECTION}/jarvis/jarvis.onnx",
}


def fetch(url: str, path: Path) -> None:
    if path.is_file():
        print(f"{path} already there")
        return
    print(f"Downloading {path.name} …")
    path.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, path.open("wb") as out:  # noqa: S310
        out.write(response.read())
    print(f"  {path} ({path.stat().st_size // 1024} KB)")


def main(phrases: list[str]) -> int:
    for name, url in FEATURES.items():
        fetch(url, DEST / name)
    for phrase in phrases or ["computer"]:
        url = PHRASES.get(phrase)
        if url is None:
            print(f"Unknown phrase {phrase!r}; known: {', '.join(sorted(PHRASES))}")
            return 1
        fetch(url, DEST / f"{phrase}.onnx")
    print(f"Wake word models ready in {DEST.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
