"""Shared persona voice selection and explicit fallback reporting."""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from agent_hub.registry.models import Persona
from agent_hub.server import session_state

VOICE_TEST_TEXT = "Hello! This is my persona voice. How can I help you today?"


class PcmSynthesizer(Protocol):
    """The provider operation shared by device and browser audio."""

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Return mono PCM and its sample rate."""
        ...


async def synthesize_persona(
    provider: PcmSynthesizer, text: str, persona: Persona, device_id: str = ""
) -> tuple[bytes, int]:
    """Use the persona voice, reporting any fallback to the provider default."""
    state = session_state.get_state(device_id)
    state.voice_notice = ""
    try:
        return await provider.synthesize_pcm(text, voice=persona.tts_voice)
    except ValueError:
        if not persona.tts_voice:
            raise
        state.voice_notice = (
            "Configured voice could not be used; speaking with the provider default. "
            "Check the persona voice setting."
        )
        logger.warning(f"Persona {persona.name!r}: {state.voice_notice}")
        return await provider.synthesize_pcm(text, voice=None)
