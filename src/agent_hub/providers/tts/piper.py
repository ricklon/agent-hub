"""Piper TTS provider (local ONNX inference, no GPU required).

Each Piper voice is its own small ONNX model plus a JSON config, named
``<language>_<REGION>-<name>-<quality>`` (``en_US-lessac-medium``). A voice
missing from the data directory is downloaded from the rhasspy/piper-voices
collection on Hugging Face the first time a persona speaks with it, so any
published voice id works; the dashboard offers an English shortlist.

Output is int16 PCM at the voice's own rate (22050 Hz for most medium
voices); the voice pipeline resamples it.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.error import HTTPError

import soundfile as sf
from loguru import logger

from agent_hub.providers.tts import TTSProvider

if TYPE_CHECKING:
    from piper import PiperVoice

DEFAULT_VOICE = "en_US-lessac-medium"


def voice_id_problem(voice: str) -> str | None:
    """Why ``voice`` is not a Piper voice id, or None if it has the right shape.

    Args:
        voice: Candidate voice id.

    Returns:
        A one-line reason, or None.
    """
    from piper.download_voices import VOICE_PATTERN

    if VOICE_PATTERN.match(voice):
        return None
    return (
        f"{voice!r} is not a Piper voice id. Piper voices look like "
        "'en_US-lessac-medium': language_REGION-name-quality."
    )


class PiperTTSProvider(TTSProvider):
    """TTS via Piper's local ONNX voices, downloaded on first use."""

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        data_dir: str | Path = "models/piper",
        speed: float = 1.0,
    ) -> None:
        """Create a PiperTTSProvider.

        Voices are loaded lazily, on the first sentence each one speaks.

        Args:
            voice: Default voice id.
            data_dir: Where voice models are stored and downloaded to.
            speed: Speaking speed multiplier (1.0 = the voice's own pace).
        """
        self._voice = voice
        self._data_dir = Path(data_dir)
        self._length_scale = 1.0 / speed if speed > 0 else 1.0
        self._voices: dict[str, PiperVoice] = {}
        self._lock = asyncio.Lock()

    def _download(self, voice: str) -> None:
        """Fetch one voice into the data directory, all or nothing."""
        from piper.download_voices import download_voice

        self._data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Downloading Piper voice {voice!r} into {self._data_dir}")
        # Download beside the target and move into place, so an interrupted
        # download never leaves a truncated model that later looks present.
        with tempfile.TemporaryDirectory(dir=self._data_dir) as tmp:
            try:
                download_voice(voice, Path(tmp))
            except HTTPError as exc:
                if exc.code == 404:
                    raise ValueError(f"No published Piper voice called {voice!r}") from exc
                raise
            for name in (f"{voice}.onnx.json", f"{voice}.onnx"):
                shutil.move(Path(tmp) / name, self._data_dir / name)

    def _load(self, voice: str) -> PiperVoice:
        from piper import PiperVoice

        model_path = self._data_dir / f"{voice}.onnx"
        if not model_path.exists():
            self._download(voice)
        return PiperVoice.load(model_path)

    async def _get_voice(self, voice: str | None) -> PiperVoice:
        name = (voice or self._voice).strip()
        if problem := voice_id_problem(name):
            raise ValueError(problem)
        async with self._lock:
            if name not in self._voices:
                self._voices[name] = await asyncio.to_thread(self._load, name)
            return self._voices[name]

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Return mono int16 PCM at the voice's sample rate.

        Args:
            text: Text to synthesize.
            voice: Override voice id; uses the instance default if None.

        Returns:
            (pcm_bytes, sample_rate).

        Raises:
            ValueError: If the voice id is malformed or not published.
        """
        from piper import SynthesisConfig

        model = await self._get_voice(voice)
        config = SynthesisConfig(length_scale=self._length_scale)

        def _run() -> bytes:
            return b"".join(chunk.audio_int16_bytes for chunk in model.synthesize(text, config))

        pcm = await asyncio.to_thread(_run)
        return pcm, int(model.config.sample_rate)

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to WAV bytes.

        Args:
            text: Text to synthesize.
            voice: Override voice id; uses the instance default if None.

        Returns:
            WAV-encoded mono audio at the voice's sample rate.
        """
        import numpy as np

        pcm, rate = await self.synthesize_pcm(text, voice)
        buf = io.BytesIO()
        sf.write(buf, np.frombuffer(pcm, dtype=np.int16), rate, format="WAV")
        return buf.getvalue()
