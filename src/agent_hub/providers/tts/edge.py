"""Edge TTS provider (Microsoft Edge neural voices, free, online)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import edge_tts

from agent_hub.providers.tts import TTSProvider

_TTS_SAMPLE_RATE = 24000


class EdgeTTSProvider(TTSProvider):
    """TTS via Microsoft Edge's neural voices.

    Requires internet access. No API key needed. Voices are identified by
    locale-name strings such as 'en-US-AriaNeural'.
    """

    def __init__(
        self,
        voice: str = "en-US-AriaNeural",
        rate: str = "+0%",
        volume: str = "+0%",
    ) -> None:
        """Create an EdgeTTSProvider.

        Args:
            voice: Default Edge voice name (e.g. 'en-US-AriaNeural').
            rate: SSML rate offset string (e.g. '+10%', '-5%').
            volume: SSML volume offset string.
        """
        self._voice = voice
        self._rate = rate
        self._volume = volume

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to MP3 bytes via the Edge TTS service.

        Args:
            text: Text to synthesize.
            voice: Override voice; uses instance default if None.

        Returns:
            MP3-encoded audio bytes (Edge TTS native format).
        """
        v = voice or self._voice
        communicate = edge_tts.Communicate(text, v, rate=self._rate, volume=self._volume)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Return PCM int16 bytes at 24 kHz by decoding Edge TTS MP3 via ffmpeg.

        Args:
            text: Text to synthesize.
            voice: Override voice.

        Returns:
            (pcm_bytes, 24000): mono int16 LE PCM at 24 kHz.
        """
        from agent_hub.server.audio import mp3_to_pcm

        mp3_bytes = await self.synthesize(text, voice)
        pcm = await mp3_to_pcm(mp3_bytes, sample_rate=_TTS_SAMPLE_RATE)
        return pcm, _TTS_SAMPLE_RATE

    async def synthesize_stream(self, text: str, voice: str | None = None) -> AsyncIterator[bytes]:
        """Stream MP3 audio chunks from Edge TTS as they arrive.

        Args:
            text: Text to synthesize.
            voice: Override voice.

        Yields:
            MP3 audio byte chunks.
        """
        v = voice or self._voice
        communicate = edge_tts.Communicate(text, v, rate=self._rate, volume=self._volume)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                yield chunk["data"]
