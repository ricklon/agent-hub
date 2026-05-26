"""Emotion display utilities for xiaozhi device LCD face.

Two signal sources:
  1. User emotion (from SenseVoice ASR tags) — empathetic reactive display
  2. Reply emotion (emoji embedded in LLM text) — expressive display

Both produce {"type": "llm", "text": "<emoji>", "emotion": "<name>", "session_id": "..."}
messages sent over the WebSocket before TTS starts.
"""

from __future__ import annotations

import re

# Upstream-compatible emoji → device emotion name map.
_EMOJI_MAP: dict[str, str] = {
    "😂": "funny",
    "😆": "laughing",
    "😭": "crying",
    "😳": "embarrassed",
    "😠": "angry",
    "😉": "winking",
    "😔": "sad",
    "😎": "cool",
    "😍": "loving",
    "🤤": "delicious",
    "😲": "surprised",
    "😘": "kissy",
    "😱": "shocked",
    "😏": "confident",
    "🤔": "thinking",
    "🙂": "happy",
    "😌": "relaxed",
    "🙄": "confused",
    "😴": "sleepy",
    "😶": "neutral",
    "😜": "silly",
}

# SenseVoice emotion → (device emotion name, representative emoji)
_USER_EMOTION_MAP: dict[str, tuple[str, str]] = {
    "HAPPY": ("happy", "🙂"),
    "SAD": ("sad", "😔"),
    "ANGRY": ("angry", "😠"),
    "FEARFUL": ("shocked", "😱"),
    "DISGUSTED": ("confused", "🙄"),
    "SURPRISED": ("surprised", "😲"),
}

_EMOJI_RE = re.compile("|".join(re.escape(e) for e in _EMOJI_MAP))


def user_emotion_face(sense_voice_emotion: str) -> tuple[str, str] | None:
    """Map a SenseVoice emotion tag to (emoji, device_emotion_name).

    Returns None for NEUTRAL or unknown tags.
    """
    return _USER_EMOTION_MAP.get(sense_voice_emotion)


def extract_reply_emotion(text: str) -> tuple[str, str] | None:
    """Find the first mapped emoji in LLM reply text.

    Returns (emoji, device_emotion_name) or None if no mapped emoji found.
    """
    m = _EMOJI_RE.search(text)
    if m:
        emoji = m.group()
        return emoji, _EMOJI_MAP[emoji]
    return None


def strip_emoji(text: str) -> str:
    """Remove all mapped emoji from text, collapsing extra whitespace."""
    return re.sub(r" {2,}", " ", _EMOJI_RE.sub("", text)).strip()
