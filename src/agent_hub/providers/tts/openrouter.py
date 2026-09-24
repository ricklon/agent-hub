"""OpenRouter TTS provider (cloud voices through OpenRouter's speech endpoint).

``POST {base_url}/audio/speech`` takes the OpenAI Audio Speech request shape
(``model``, ``input``, ``voice``, ``response_format``, ``speed``) and routes it
to the chosen model (OpenAI, Gemini, Kokoro, Grok, ...). Asking for ``pcm``
returns raw int16 mono audio with its rate in the Content-Type
(``audio/pcm;rate=24000;channels=1``), so no MP3 decode is needed.

Speech is billed per input character, so each call passes the hub's spend
guard first and is recorded in the spend ledger afterwards.
"""

from __future__ import annotations

import re

import httpx
from loguru import logger

from agent_hub import spend
from agent_hub.providers.tts import TTSProvider

_TAG = "tts.openrouter"
_DEFAULT_RATE = 24000
_RATE_RE = re.compile(r"rate=(\d+)")


class OpenRouterTTSProvider(TTSProvider):
    """TTS via OpenRouter's OpenAI-compatible ``/audio/speech`` endpoint.

    Voices depend on the model (``alloy`` for OpenAI models, for example), so
    the voice list is not fixed; a voice the model rejects falls back to the
    configured default with a visible notice.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str,
        base_url: str = "https://openrouter.ai/api/v1",
        speed: float | None = None,
        price_per_million_chars: float = 0.0,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create an OpenRouterTTSProvider.

        Args:
            api_key: OpenRouter API key.
            model: OpenRouter TTS model id (e.g. ``openai/gpt-4o-mini-tts-2025-12-15``).
            voice: Default voice for that model.
            base_url: API base; the endpoint is ``{base_url}/audio/speech``.
            speed: Playback speed; honoured only by models that support it.
            price_per_million_chars: USD per 1M input characters, for the spend
                ledger. 0 records the call without a cost (marked estimated).
            timeout: Seconds to wait for one sentence of audio.
            transport: HTTP transport override, for tests.
        """
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._url = base_url.rstrip("/") + "/audio/speech"
        self._speed = speed
        self._price = price_per_million_chars
        self._timeout = timeout
        self._transport = transport

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Return PCM int16 bytes and their sample rate.

        Args:
            text: Text to synthesize.
            voice: Override voice; uses the configured default if None.

        Returns:
            (pcm_bytes, sample_rate) as reported by the endpoint.

        Raises:
            ValueError: The model rejected the requested voice (the caller
                retries with the default voice).
            RuntimeError: No API key, or the endpoint returned an error.
        """
        if not self._api_key:
            raise RuntimeError(
                "OpenRouter voice needs an API key: set tts.openrouter.api_key "
                "(or AGENT_HUB_TTS_OPENROUTER_API_KEY)."
            )
        await spend.guard()
        body: dict[str, object] = {
            "model": self._model,
            "input": text,
            "voice": voice or self._voice,
            "response_format": "pcm",
        }
        if self._speed is not None:
            body["speed"] = self._speed
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            resp = await client.post(
                self._url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Title": "agent-hub",
                },
            )
        if resp.status_code != 200:
            message = _error_message(resp)
            if voice and resp.status_code in (400, 422) and "voice" in message.lower():
                raise ValueError(message)
            raise RuntimeError(f"OpenRouter TTS {resp.status_code}: {message}")
        match = _RATE_RE.search(resp.headers.get("content-type", ""))
        rate = int(match.group(1)) if match else _DEFAULT_RATE
        await spend.record(
            self._model, 0, 0, len(text) * self._price / 1_000_000 if self._price else None
        )
        logger.bind(tag=_TAG).debug(
            f"{self._model} {len(text)} chars → {len(resp.content)} bytes @ {rate}Hz "
            f"({resp.headers.get('x-generation-id', 'no generation id')})"
        )
        return resp.content, rate

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Synthesize text to a WAV file.

        Args:
            text: Text to synthesize.
            voice: Override voice; uses the configured default if None.

        Returns:
            WAV-encoded audio bytes.
        """
        from agent_hub.server.audio import pcm_to_wav

        pcm, rate = await self.synthesize_pcm(text, voice)
        return pcm_to_wav(pcm, rate)


def _error_message(resp: httpx.Response) -> str:
    """The error text from a JSON error body, or the raw body."""
    try:
        payload = resp.json()
    except ValueError:
        return resp.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:200]
    return str(error or payload)[:200]
