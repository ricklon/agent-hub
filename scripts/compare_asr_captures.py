"""Compare ASR providers on real captured device audio.

`scripts/bench_asr.py` measures WER against LibriSpeech, which is clean read
speech. Field accuracy is worse, and the useful question is *why*: a weak model,
or audio that was already degraded before the model saw it.

This replays audio captured by `server.debug_audio_dir` through one or more
providers and prints them side by side, with signal statistics for each clip.
Captures have no ground truth — you are the judge — so it prints, it does not
score.

Usage:
    uv run --extra full python scripts/compare_asr_captures.py data/asr-captures
    PROVIDERS=moonshine,funasr_onnx uv run --extra full python \\
        scripts/compare_asr_captures.py data/asr-captures

Reading the signal stats:
    peak near 1.0 with clipped% above ~0.1   input gain too high; distortion
    rms below ~0.01                          too quiet or too far from the mic
    duration far longer than the words       VAD is holding silence around speech
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np

from agent_hub.providers.asr import get_provider, is_available

PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "moonshine").split(",") if p.strip()]

CONFIG = {
    "asr": {
        "moonshine": {"language": "en", "model_arch": "tiny", "cache_root": "models/moonshine"},
        "funasr_onnx": {"model_dir": "models/SenseVoiceSmall-onnx"},
    }
}


def signal_stats(wav_path: Path) -> dict[str, float]:
    """Duration, level and clipping for one capture."""
    with wave.open(str(wav_path), "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if samples.size == 0:
        return {"seconds": 0.0, "rate": rate, "peak": 0.0, "rms": 0.0, "clipped_pct": 0.0}
    peak = float(np.max(np.abs(samples)))
    return {
        "seconds": samples.size / rate,
        "rate": float(rate),
        "peak": peak,
        "rms": float(np.sqrt(np.mean(samples**2))),
        "clipped_pct": 100.0 * float(np.mean(np.abs(samples) > 0.99)),
    }


async def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "data/asr-captures")
    wavs = sorted(directory.glob("*.wav"))
    if not wavs:
        print(f"No captures in {directory}. Set server.debug_audio_dir and speak to a device.")
        return

    usable = [p for p in PROVIDERS if is_available(p)]
    for missing in [p for p in PROVIDERS if p not in usable]:
        print(f"note: provider {missing!r} is not installed in this environment — skipping")
    if not usable:
        print("No requested providers are installed.")
        return

    print(f"{len(wavs)} captures from {directory}, providers: {', '.join(usable)}\n")
    for wav in wavs:
        stats = signal_stats(wav)
        sidecar = wav.with_suffix(".json")
        recorded = ""
        if sidecar.exists():
            recorded = json.loads(sidecar.read_text()).get("transcript", "")

        print(f"── {wav.name}")
        print(
            f"   {stats['seconds']:.1f}s @ {stats['rate']:.0f}Hz  "
            f"peak={stats['peak']:.2f}  rms={stats['rms']:.3f}  "
            f"clipped={stats['clipped_pct']:.2f}%"
        )
        if recorded:
            print(f"   {'as recorded':<14} {recorded!r}")
        for name in usable:
            provider = get_provider(name, CONFIG)
            result = await provider.transcribe(wav.read_bytes())
            print(f"   {name:<14} {result.text!r}")
        print()


asyncio.run(main())
