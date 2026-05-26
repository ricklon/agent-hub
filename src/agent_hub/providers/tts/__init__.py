"""TTS provider base class and registry."""

from __future__ import annotations

import abc
from typing import Any, AsyncIterator


class TTSProvider(abc.ABC):
    """Abstract base for text-to-speech providers.

    The pipeline-facing method is synthesize_pcm(), which returns raw PCM
    int16 bytes + sample rate. The higher-level synthesize() is preserved
    for callers that want a self-contained audio file.
    """

    @abc.abstractmethod
    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text and return a self-contained audio file (WAV or MP3).

        Args:
            text: Text to synthesize.
            voice: Provider voice name; uses provider default if None.

        Returns:
            Audio file bytes (format is provider-specific: WAV or MP3).
        """

    async def synthesize_pcm(
        self, text: str, voice: str | None = None
    ) -> tuple[bytes, int]:
        """Synthesize text and return raw PCM int16 bytes + sample rate.

        This is the method used by the audio pipeline to feed the Opus encoder.
        The default implementation assumes synthesize() returns WAV and reads it
        with soundfile. Providers that return other formats (e.g. MP3) must
        override this method.

        Args:
            text: Text to synthesize.
            voice: Provider voice name; uses provider default if None.

        Returns:
            (pcm_bytes, sample_rate): raw int16 LE mono PCM and its sample rate.
        """
        import io

        import soundfile as sf

        wav_bytes = await self.synthesize(text, voice)
        audio, sr = sf.read(io.BytesIO(wav_bytes), dtype="int16")
        return audio.tobytes(), int(sr)

    async def synthesize_stream(
        self, text: str, voice: str | None = None
    ) -> AsyncIterator[bytes]:
        """Yield audio chunks as they become available.

        Default calls synthesize() and yields one chunk. Override for
        providers with native streaming.

        Args:
            text: Text to synthesize.
            voice: Provider voice name.

        Yields:
            Audio byte chunks.
        """
        yield await self.synthesize(text, voice)

    @property
    def available_voices(self) -> list[str]:
        """Return provider's built-in voice names, or [] if dynamic."""
        return []


def get_provider(name: str, config: dict[str, Any]) -> TTSProvider:
    """Instantiate a TTS provider by name from config.

    Args:
        name: Provider key matching .config.yaml tts.<name>.
        config: Full raw config dict (tts section is extracted internally).

    Returns:
        Configured TTSProvider instance.

    Raises:
        ValueError: If the provider name is unknown.
    """
    tts_cfg: dict[str, Any] = config.get("tts", {})
    if name == "edge":
        from agent_hub.providers.tts.edge import EdgeTTSProvider

        cfg = tts_cfg.get("edge", {})
        return EdgeTTSProvider(
            voice=cfg.get("voice", "en-US-AriaNeural"),
            rate=cfg.get("rate", "+0%"),
            volume=cfg.get("volume", "+0%"),
        )
    if name == "kitten":
        from agent_hub.providers.tts.kitten import KittenTTSProvider

        cfg = tts_cfg.get("kitten", {})
        return KittenTTSProvider(
            model=cfg.get("model", "KittenML/kitten-tts-nano-0.8"),
            voice=cfg.get("voice", "Luna"),
            speed=float(cfg.get("speed", 1.0)),
        )
    raise ValueError(f"Unknown TTS provider: {name!r}")
