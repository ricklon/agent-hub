"""A cloud voice that falls back to a local one when the cloud refuses it.

Edge TTS is an unofficial Microsoft endpoint that answers 403 to some
datacentre addresses, and a cloud voice can fail for plain network reasons
too. Without a fallback the LLM answers and nobody hears it. This wraps a
cloud provider with a local one (Piper): a failed call is spoken by the
local voice instead, and for a short while the cloud voice isn't tried at
all, so each sentence doesn't pay for another failed handshake.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from loguru import logger

from agent_hub.providers.tts import TTSProvider

# After a cloud failure, speak locally for this long before trying the cloud again.
COOLDOWN_S = 60.0
_LOG_EVERY_S = 60.0


class FallbackTTS(TTSProvider):
    """Try ``primary``; on any failure but a bad voice, speak with ``fallback``."""

    def __init__(
        self, primary: TTSProvider, fallback: Callable[[], TTSProvider], name: str
    ) -> None:
        """Wrap a cloud provider.

        Args:
            primary: The persona's chosen provider.
            fallback: Builds a local provider that needs no network (Piper);
                called only the first time the primary fails.
            name: The primary's name, for log lines.
        """
        self._primary = primary
        self._make_fallback = fallback
        self._fallback: TTSProvider | None = None
        self._name = name
        self._skip_until = 0.0
        self._last_log = 0.0

    @property
    def primary(self) -> TTSProvider:
        """The wrapped cloud provider."""
        return self._primary

    @property
    def available_voices(self) -> list[str]:
        """The primary provider's voices."""
        return self._primary.available_voices

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        """Self-contained audio from the primary, or the fallback when it fails."""
        if time.monotonic() >= self._skip_until:
            try:
                return await self._primary.synthesize(text, voice=voice)
            except ValueError:
                raise
            except Exception as exc:
                self._failed(exc)
        return await self._local().synthesize(text, voice=None)

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """PCM from the primary, or from the fallback when the primary fails.

        A ``ValueError`` (the voice id is unusable) passes through so the
        caller's own "use the provider default" handling still applies.
        """
        if time.monotonic() >= self._skip_until:
            try:
                return await self._primary.synthesize_pcm(text, voice=voice)
            except ValueError:
                raise
            except Exception as exc:
                self._failed(exc)
        return await self._local().synthesize_pcm(text, voice=None)

    def _local(self) -> TTSProvider:
        if self._fallback is None:
            self._fallback = self._make_fallback()
        return self._fallback

    def _failed(self, exc: Exception) -> None:
        now = time.monotonic()
        self._skip_until = now + COOLDOWN_S
        if now - self._last_log >= _LOG_EVERY_S:
            self._last_log = now
            logger.warning(
                f"TTS {self._name!r} failed ({type(exc).__name__}: {str(exc)[:120]}); "
                f"speaking with the local voice for {int(COOLDOWN_S)}s"
            )
