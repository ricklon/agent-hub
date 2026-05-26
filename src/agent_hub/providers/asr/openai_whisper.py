"""OpenAI Whisper ASR provider."""

from __future__ import annotations

from openai import AsyncOpenAI

from agent_hub.providers.asr import ASRProvider, Transcript


class OpenAIWhisperASRProvider(ASRProvider):
    """Transcribes audio via the OpenAI Whisper API."""

    def __init__(
        self,
        api_key: str,
        model: str = "whisper-1",
        language: str | None = None,
    ) -> None:
        """Create an OpenAIWhisperASRProvider.

        Args:
            api_key: OpenAI API key.
            model: Whisper model name (currently only 'whisper-1' is available).
            language: BCP-47 language hint; None enables auto-detection.
        """
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._language = language

    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> Transcript:
        lang = language or self._language
        kwargs: dict = {"model": self._model, "file": ("audio.wav", audio_bytes, "audio/wav")}
        if lang:
            kwargs["language"] = lang

        result = await self._client.audio.transcriptions.create(**kwargs)
        return Transcript(text=result.text.strip())
