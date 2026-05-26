"""KittenTTS provider (local ONNX inference, no GPU required).

KittenTTS outputs numpy float32 arrays at 24 kHz. This provider converts
them to WAV bytes via soundfile so the rest of the pipeline sees standard
audio data.

Models:
  kitten-tts-mini-0.8   — 80M params, 80 MB, best quality
  kitten-tts-micro-0.8  — 40M params, 41 MB, balanced
  kitten-tts-nano-0.8   — 15M params, 56 MB, fastest
  kitten-tts-nano-0.8-int8 — 15M params, 25 MB, smallest (may have artifacts)

Voices: Bella, Jasper, Luna, Bruno, Rosie, Hugo, Kiki, Leo
"""

from __future__ import annotations

import asyncio
import io
from typing import AsyncIterator

import numpy as np
import soundfile as sf

from agent_hub.providers.tts import TTSProvider

_SAMPLE_RATE = 24000


class KittenTTSProvider(TTSProvider):
    """TTS via KittenML's local ONNX models.

    Model weights are downloaded from Hugging Face on first use and cached
    in the standard HF cache directory (~/.cache/huggingface).
    """

    def __init__(
        self,
        model: str = "KittenML/kitten-tts-nano-0.8",
        voice: str = "Luna",
        speed: float = 1.0,
    ) -> None:
        """Create a KittenTTSProvider.

        Model is loaded lazily on first call to synthesize().

        Args:
            model: Hugging Face model ID for KittenTTS.
            voice: Default voice name.
            speed: Playback speed multiplier (1.0 = normal).
        """
        self._model_id = model
        self._voice = voice
        self._speed = speed
        self._model: object | None = None  # lazy-loaded

    def _ensure_model(self) -> object:
        if self._model is None:
            from kittentts import KittenTTS  # type: ignore[import-untyped]

            self._model = KittenTTS(self._model_id)
        return self._model

    @property
    def available_voices(self) -> list[str]:
        """KittenTTS built-in voice names."""
        return ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]

    def _array_to_wav(self, audio: np.ndarray) -> bytes:
        buf = io.BytesIO()
        sf.write(buf, audio, _SAMPLE_RATE, format="WAV")
        return buf.getvalue()

    async def synthesize_pcm(
        self, text: str, voice: str | None = None
    ) -> tuple[bytes, int]:
        """Return PCM int16 bytes directly from KittenTTS numpy output.

        Skips the WAV roundtrip used by the default synthesize_pcm().

        Args:
            text: Text to synthesize.
            voice: Override voice.

        Returns:
            (pcm_bytes, 24000): mono int16 LE PCM at 24 kHz.
        """
        v = voice or self._voice
        model = self._ensure_model()
        audio: np.ndarray = await asyncio.to_thread(
            model.generate, text, voice=v, speed=self._speed  # type: ignore[union-attr]
        )
        pcm = (np.clip(audio.squeeze(), -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        return pcm, _SAMPLE_RATE

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to WAV bytes using KittenTTS ONNX inference.

        Runs the synchronous KittenTTS generate() call in a thread pool
        so it does not block the event loop.

        Args:
            text: Text to synthesize.
            voice: Override voice; uses instance default if None.

        Returns:
            WAV-encoded audio bytes at 24 kHz mono.
        """
        v = voice or self._voice
        model = self._ensure_model()
        audio: np.ndarray = await asyncio.to_thread(
            model.generate, text, voice=v, speed=self._speed  # type: ignore[union-attr]
        )
        return self._array_to_wav(audio)

    async def synthesize_stream(  # type: ignore[override]
        self, text: str, voice: str | None = None
    ) -> AsyncIterator[bytes]:
        """Stream WAV chunks using KittenTTS generate_stream().

        Args:
            text: Text to synthesize.
            voice: Override voice.

        Yields:
            WAV-encoded audio chunks at 24 kHz mono.
        """
        v = voice or self._voice
        model = self._ensure_model()

        # generate_stream is sync; run each chunk in a thread via an async wrapper
        loop = asyncio.get_event_loop()

        def _iter_chunks():  # type: ignore[return]
            return list(model.generate_stream(text=text, voice=v))  # type: ignore[union-attr]

        chunks = await loop.run_in_executor(None, _iter_chunks)
        for chunk in chunks:
            yield self._array_to_wav(chunk.squeeze())
