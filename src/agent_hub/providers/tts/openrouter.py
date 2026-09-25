"""OpenRouter TTS provider (cloud voices through OpenRouter's speech endpoint).

``POST {base_url}/audio/speech`` takes the OpenAI Audio Speech request shape
(``model``, ``input``, ``voice``, ``response_format``, ``speed``) and routes it
to the chosen model (Gemini 3.8 Flash Lite TTS by default). Asking for ``pcm``
returns raw int16 mono audio with its rate in the Content-Type
(``audio/pcm;rate=24000;channels=1``), so no MP3 decode is needed.

Speech is billed per token: the input text, and far more for the audio out
(Gemini TTS makes about 32 audio tokens per second of speech). The endpoint
reports no cost, so each call passes the hub's spend guard first, is recorded
at once with an estimate from the catalogue's per-token prices, and is then
settled in the background with the billed cost from
``{base_url}/generation?id=…`` (available some seconds after the call).
"""

from __future__ import annotations

import asyncio
import math
import re

import httpx
from loguru import logger

from agent_hub import spend
from agent_hub.providers.openrouter_prices import token_prices
from agent_hub.providers.tts import TTSProvider

_TAG = "tts.openrouter"
_DEFAULT_RATE = 24000
_RATE_RE = re.compile(r"rate=(\d+)")
# Gemini TTS audio tokens per second of speech (200 tokens for 6.24 s, measured).
AUDIO_TOKENS_PER_SECOND = 32
# Seconds to wait before each billed-cost lookup; the generation record
# appears some seconds after the audio does.
_SETTLE_DELAYS = (4.0, 4.0, 6.0, 10.0, 20.0)
# Settle tasks run after the request returns; hold them so they are not collected.
_settling: set[asyncio.Task[None]] = set()


def estimate_tokens(text: str, pcm_bytes: int, rate: int) -> tuple[int, int]:
    """(input, output) tokens for a speech call: ~4 characters a token in, audio out."""
    seconds = pcm_bytes / 2 / rate if rate else 0.0
    return math.ceil(len(text) / 4), round(seconds * AUDIO_TOKENS_PER_SECOND)


class OpenRouterTTSProvider(TTSProvider):
    """TTS via OpenRouter's OpenAI-compatible ``/audio/speech`` endpoint.

    Voices depend on the model (``Kore``, ``Puck``, ... for Gemini TTS), so
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
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        settle_delays: tuple[float, ...] = _SETTLE_DELAYS,
    ) -> None:
        """Create an OpenRouterTTSProvider.

        Args:
            api_key: OpenRouter API key.
            model: OpenRouter TTS model id (e.g. ``google/gemini-3.8-flash-lite-tts``).
            voice: Default voice for that model.
            base_url: API base; the endpoint is ``{base_url}/audio/speech``.
            speed: Playback speed; honoured only by models that support it.
            timeout: Seconds to wait for one sentence of audio.
            transport: HTTP transport override, for tests.
            settle_delays: Waits before each billed-cost lookup (tests shorten them).
        """
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._base_url = base_url.rstrip("/")
        self._url = self._base_url + "/audio/speech"
        self._speed = speed
        self._timeout = timeout
        self._transport = transport
        self._settle_delays = settle_delays

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Return PCM int16 bytes and their sample rate.

        Args:
            text: Text to synthesize.
            voice: Override voice; uses the configured default if None.

        Returns:
            (pcm_bytes, sample_rate) as reported by the endpoint.

        Raises:
            ValueError: A request with an explicit voice was refused (400/422);
                the caller retries with the default voice.
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
            # OpenRouter reports a voice the model does not have as a bare
            # "Provider returned 400", so any 400/422 on an explicit voice is
            # treated as a rejected voice; the default-voice retry surfaces a
            # different cause as a RuntimeError.
            if voice and resp.status_code in (400, 422):
                raise ValueError(message)
            raise RuntimeError(f"OpenRouter TTS {resp.status_code}: {message}")
        match = _RATE_RE.search(resp.headers.get("content-type", ""))
        rate = int(match.group(1)) if match else _DEFAULT_RATE
        tokens_in, tokens_out = estimate_tokens(text, len(resp.content), rate)
        prices = await token_prices(self._model, self._base_url, self._transport)
        cost = tokens_in * prices[0] + tokens_out * prices[1] if prices else None
        row_id = await spend.record(self._model, tokens_in, tokens_out, cost, estimated=True)
        generation = resp.headers.get("x-generation-id", "")
        if row_id is not None and generation:
            task = asyncio.create_task(self._settle(row_id, generation))
            _settling.add(task)
            task.add_done_callback(_settling.discard)
        logger.bind(tag=_TAG).debug(
            f"{self._model} {len(text)} chars → {len(resp.content)} bytes @ {rate}Hz, "
            f"est. ${cost or 0:.6f} ({generation or 'no generation id'})"
        )
        return resp.content, rate

    async def _settle(self, row_id: int, generation: str) -> None:
        """Replace the estimate with the billed cost once OpenRouter has it."""
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
            for delay in self._settle_delays:
                await asyncio.sleep(delay)
                try:
                    resp = await client.get(
                        self._base_url + "/generation",
                        params={"id": generation},
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                except httpx.HTTPError:
                    continue
                if resp.status_code == 404:
                    continue  # not recorded yet
                if resp.status_code != 200:
                    break
                data = resp.json().get("data") or {}
                if data.get("total_cost") is None:
                    continue
                await spend.settle(
                    row_id,
                    float(data["total_cost"]),
                    int(data.get("native_tokens_prompt") or data.get("tokens_prompt") or 0),
                    int(data.get("native_tokens_completion") or data.get("tokens_completion") or 0),
                )
                return
        logger.bind(tag=_TAG).warning(
            f"No billed cost for {generation}; the ledger keeps the estimate"
        )

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
