"""Model output is cleaned before any voice reads it."""

from __future__ import annotations

import pytest

from agent_hub.registry.models import Persona
from agent_hub.server.persona_voice import synthesize_persona
from agent_hub.server.speech_text import for_speech


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("**Sure!** Here's the `get_weather` result.", "Sure! Here's the get weather result."),
        ("## Today\n- Sunny\n- 72 degrees", "Today\nSunny\n72 degrees"),
        ("1. Preheat\n2) Bake", "Preheat\nBake"),
        (
            "It costs $3.99, or $1 on sale, or $0.50 used.",
            "It costs 3 dollars and 99 cents, or 1 dollar on sale, or 50 cents used.",
        ),
        ("Battery at 15% and 99.5 %.", "Battery at 15 percent and 99.5 percent."),
        ("You came 1st, then 3rd, then 21st.", "You came first, then third, then 21st."),
        (
            "The timer ends at 14:32, lunch at 12:00.",
            "The timer ends at 2 32 PM, lunch at 12 PM.",
        ),
        ("See [the docs](https://example.com) for more.", "See the docs for more."),
        ("<think>user wants time</think>It is noon.", "It is noon."),
        ('<tool_call>{"name": "x"}</tool_call>Done.', "Done."),
        ('[TOOL_CALLS][{"name": "get_current_time", "arguments": {}}] One moment.', "One moment."),
        ("Here:\n```python\nprint(1)\n```\nThat prints one.", "Here:\n\nThat prints one."),
        ("2 * 3 is 6", "2 times 3 is 6"),
        ("I like C# and version 2:1:3", "I like C# and version 2:1:3"),
        ("", ""),
    ],
)
def test_for_speech(raw: str, spoken: str) -> None:
    assert for_speech(raw) == spoken


class _RecordingTTS:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def synthesize_pcm(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        self.texts.append(text)
        return b"\x01\x00", 24000


async def test_every_voice_path_hears_cleaned_text() -> None:
    tts = _RecordingTTS()
    persona = Persona(name="p", tts_provider="edge")
    assert await synthesize_persona(tts, "**Hi** at 50%", persona) == (b"\x01\x00", 24000)
    assert tts.texts == ["Hi at 50 percent"]


async def test_nothing_speakable_synthesizes_nothing() -> None:
    tts = _RecordingTTS()
    persona = Persona(name="p", tts_provider="edge")
    assert await synthesize_persona(tts, "```\ncode only\n```", persona) == (b"", 16000)
    assert tts.texts == []
