"""Listen mode: a device keeps hearing but stops answering.

While a builder debugs a robot, room chatter otherwise becomes LLM turns that
talk back and call servo tools. In listen mode each utterance is still
transcribed and logged, and the robot only says a short "Okay" so the builder
knows it heard; no LLM turn or device tool runs. It is toggled by
voice ("robot, go into listen mode" / "robot, interact again") or from the
dashboard, and is held in hub memory per device so it survives the device
reconnecting or rebooting, but not a hub restart.
"""

from __future__ import annotations

import re
from typing import Literal

Command = Literal["listen", "interact"]

LISTEN_CONFIRMATION = "Listen mode. I'll stay quiet until you say interact again."
INTERACT_CONFIRMATION = "Okay, I'm back."
# Spoken after each utterance heard in listen mode, in place of an answer.
LISTEN_ACK = "Okay."

# Phrases are matched on normalized text (lowercase, punctuation stripped), so
# ASR punctuation and casing don't matter. "robot" is not required: ASR often
# drops or mangles a leading wake word, and these phrases are specific enough
# that ambient speech rarely contains them.
_EXIT_LISTEN = re.compile(
    r"\b(?:exit|leave|stop|end|quit|cancel|turn off)\b.*\blisten(?:ing)? mode\b"
    r"|\blisten(?:ing)? mode (?:off|over|done)\b"
)
_ENTER_LISTEN = re.compile(r"\blisten(?:ing)? mode\b|\blisten only\b")
_INTERACT = re.compile(r"\binteract(?:ive)? (?:again|mode)\b|\bstart interacting\b")

_listen_only: set[str] = set()


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split())


def parse_command(transcript: str) -> Command | None:
    """Return the listen-mode command in ``transcript``, or None."""
    text = _normalize(transcript)
    if _EXIT_LISTEN.search(text) or _INTERACT.search(text):
        return "interact"
    if _ENTER_LISTEN.search(text):
        return "listen"
    return None


def set_listen_only(device_id: str, on: bool) -> None:
    if on:
        _listen_only.add(device_id)
    else:
        _listen_only.discard(device_id)


def is_listen_only(device_id: str) -> bool:
    return device_id in _listen_only
