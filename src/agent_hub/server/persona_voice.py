"""Shared persona voice selection and explicit fallback reporting."""

from __future__ import annotations

from typing import Protocol

from loguru import logger

from agent_hub import spend
from agent_hub.registry.models import Persona
from agent_hub.server import session_state
from agent_hub.server.speech_text import for_speech, speech_pieces

VOICE_TEST_TEXT = "Hello! This is my persona voice. How can I help you today?"


class PcmSynthesizer(Protocol):
    """The provider operation shared by device and browser audio."""

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Return mono PCM and its sample rate."""
        ...


async def synthesize_persona(
    provider: PcmSynthesizer, text: str, persona: Persona, device_id: str = ""
) -> tuple[bytes, int]:
    """Use the persona voice, reporting any fallback to the provider default.

    The text is cleaned for speech first (markdown, symbols, leaked tool
    calls); if nothing speakable is left, no audio is returned. Long text is
    synthesized in pieces (``speech_pieces``) and the audio joined, since some
    engines fail on a long stretch without sentence punctuation.
    """
    state = session_state.get_state(device_id)
    state.voice_notice = ""
    if device_id:
        spend.bind_device(device_id)  # a cloud voice's cost is this agent's
    text = for_speech(text)
    if not text:
        return b"", 16000
    voice = persona.tts_voice
    audio = bytearray()
    rate = 16000
    for piece in speech_pieces(text):
        try:
            pcm, piece_rate = await provider.synthesize_pcm(piece, voice=voice)
        except ValueError:
            if not voice:
                raise
            voice = None
            state.voice_notice = (
                "Configured voice could not be used; speaking with the provider default. "
                "Check the persona voice setting."
            )
            logger.warning(f"Persona {persona.name!r}: {state.voice_notice}")
            pcm, piece_rate = await provider.synthesize_pcm(piece, voice=None)
        if audio and piece_rate != rate:
            raise RuntimeError(f"TTS changed sample rate mid-reply ({rate} → {piece_rate})")
        audio.extend(pcm)
        rate = piece_rate
    return bytes(audio), rate
