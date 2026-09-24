"""Turn model output into text a TTS voice reads naturally.

Models write for screens: markdown, ``snake_case`` tool names, ``$3.99``,
``14:32``, ``15%``. Voices read those literally ("asterisk asterisk",
"underscore"). Every speech path calls ``for_speech`` through
``persona_voice.synthesize_persona``, so devices, page agents and the
dashboard preview all hear the same cleanup.

Ported from ricklon/talkbot ``text_utils.normalize_for_tts``, where it was
measured with a TTS-friction score across benchmark conversations.
"""

from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>", re.IGNORECASE)
# Tool calls a model wrote as text instead of calling the tool; never speak them.
_TOOL_CALL_RE = re.compile(r"<tool_call>.*?</tool_call>|\[TOOL_CALLS\]\s*\[.*?\]", re.DOTALL)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_EMPHASIS_RE = re.compile(r"(\*{1,3}|_{2})([^*_\n]+)\1")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_RULE_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_TIMES_RE = re.compile(r"(\d)\s*[*×]\s*(\d)")
_STRAY_MARKS_RE = re.compile(r"[*`]+")
_SNAKE_RE = re.compile(r"\b([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)\b")
_PERCENT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*%")
_CURRENCY_RE = re.compile(r"\$(\d[\d,]*(?:\.\d+)?)")
_ORDINAL_RE = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b(?![\d/:])")
_SPACES_RE = re.compile(r"[ \t]+")

_ORDINALS = {
    1: "first",
    2: "second",
    3: "third",
    4: "fourth",
    5: "fifth",
    6: "sixth",
    7: "seventh",
    8: "eighth",
    9: "ninth",
    10: "tenth",
    11: "eleventh",
    12: "twelfth",
    13: "thirteenth",
    14: "fourteenth",
    15: "fifteenth",
    16: "sixteenth",
    17: "seventeenth",
    18: "eighteenth",
    19: "nineteenth",
    20: "twentieth",
}


def _currency(match: re.Match[str]) -> str:
    raw = match.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return match.group(0)
    dollars = int(value)
    cents = round((value - dollars) * 100)
    if dollars == 0:
        return f"{cents} cents"
    unit = "dollar" if dollars == 1 else "dollars"
    return f"{dollars} {unit}" if cents == 0 else f"{dollars} {unit} and {cents} cents"


def _ordinal(match: re.Match[str]) -> str:
    return _ORDINALS.get(int(match.group(1)), match.group(0))


def _time(match: re.Match[str]) -> str:
    hour, minute = int(match.group(1)), int(match.group(2))
    period = "AM" if hour < 12 else "PM"
    spoken_minute = "" if minute == 0 else f" {minute:02d}"
    return f"{hour % 12 or 12}{spoken_minute} {period}"


def for_speech(text: str) -> str:
    """Return ``text`` as a voice should read it.

    Drops reasoning blocks, leaked tool-call syntax, code blocks and markdown;
    reads ``snake_case`` as words; spells out percentages, dollar amounts,
    small ordinals and 24-hour times. An empty result means there is nothing
    worth saying (a reply that was only a code block, say).

    Args:
        text: A reply, or one sentence of a streamed reply.
    """
    if not text:
        return ""
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_TAG_RE.sub("", text)
    text = _TOOL_CALL_RE.sub("", text)
    text = _CODE_FENCE_RE.sub("", text)
    text = _CODE_SPAN_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = _HEADER_RE.sub("", text)
    text = _RULE_RE.sub("", text)
    text = _BULLET_RE.sub("", text)
    text = _NUMBERED_RE.sub("", text)
    text = _TIMES_RE.sub(r"\1 times \2", text)
    text = _STRAY_MARKS_RE.sub("", text)
    while (spaced := _SNAKE_RE.sub(r"\1 \2", text)) != text:
        text = spaced
    text = _PERCENT_RE.sub(r"\1 percent", text)
    text = _CURRENCY_RE.sub(_currency, text)
    text = _ORDINAL_RE.sub(_ordinal, text)
    text = _TIME_RE.sub(_time, text)
    return _SPACES_RE.sub(" ", text).strip()
