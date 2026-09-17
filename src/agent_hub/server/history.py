"""Turning persisted history into messages a model will accept.

``conversation_history`` holds more than chat: photo captions (role
``image``), transcriber lines (role ``transcript``), and internal markers
appended to assistant replies. Models accept only ``user`` and ``assistant``
turns here, and OpenRouter rejects a request containing any other role with a
400, so every turn path passes history through :func:`history_for_llm` before
calling a model.
"""

from __future__ import annotations

import re

# Appended to an answer that used a tool whose result goes stale (the time, the
# weather). Such answers are dropped from context so the model asks again
# rather than repeating "it is 5:27 PM" an hour later.
VOLATILE_HISTORY_RE = re.compile(r"\n?\[volatile-tools:[^\]]+\]")
# Appended to an answer that captured a photo; a local path, meaningless to a model.
_IMAGE_MARKER_RE = re.compile(r"\n?\[image:[^\]]+\]")

_CHAT_ROLES = frozenset({"user", "assistant"})


def history_for_llm(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return the chat turns of ``history`` in the shape a model accepts.

    - only ``user`` and ``assistant`` messages
    - assistant answers built on volatile tools are left out
    - internal markers are stripped, and turns left empty are dropped
    - each message is exactly ``{"role", "content"}``: stored fields such as
      ``created_at`` are not sent
    """
    messages: list[dict[str, str]] = []
    for msg in history:
        role = msg.get("role")
        content = msg.get("content") or ""
        if role not in _CHAT_ROLES:
            continue
        if role == "assistant" and VOLATILE_HISTORY_RE.search(content):
            continue
        content = strip_history_markers(content)
        if content:
            messages.append({"role": role, "content": content})
    return messages


def strip_history_markers(content: str) -> str:
    """Remove internal metadata markers from persisted conversation text."""
    return _IMAGE_MARKER_RE.sub("", VOLATILE_HISTORY_RE.sub("", content)).strip()
