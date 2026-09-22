"""Ending idle conversations, and naming and summarizing finished ones.

A sweep runs every few minutes. It ends chat conversations whose silence has
passed their agent's idle gap (so titles appear without waiting for the next
turn), then wraps up ended conversations: one call to the persona's own model
returns a short title and summary together. The summary is what later
conversations remember.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from agent_hub import spend
from agent_hub.conversations import effective_settings
from agent_hub.providers.llm import get_provider
from agent_hub.registry.models import Conversation, ConversationKind, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server.history import strip_history_markers

_TAG = "wrap_up"

# Fewer user turns than this aren't worth a model call ("what time is it?").
MIN_TURNS_FOR_MODEL = 2
# A failed call is retried by the next sweep, then left for a manual "Title this".
MAX_ATTEMPTS = 2
SWEEP_INTERVAL_S = 300
# The transcript sent for wrap-up is capped; long conversations keep their end.
_MAX_TRANSCRIPT_CHARS = 12_000
_MAX_TITLE_WORDS = 6

_CHAT_INSTRUCTIONS = (
    "You name and summarize a finished conversation between a person and a voice "
    "assistant. Reply with only a JSON object: "
    '{"title": "at most 6 words, no quotes or trailing period", '
    '"summary": "at most 3 sentences about what the person wanted, what was decided or '
    'learned, and anything worth remembering next time"}.'
)
_TRANSCRIPT_INSTRUCTIONS = (
    "You name and summarize a transcript recorded by a room microphone, with photo "
    "captions. Reply with only a JSON object: "
    '{"title": "at most 6 words, no quotes or trailing period", '
    '"summary": "at most 3 sentences on the topics discussed and any decisions or actions"}.'
)


@dataclass(frozen=True)
class WrapUpResult:
    """What happened to one conversation."""

    status: str  # "model", "fallback", "skipped", or "failed"
    title: str | None = None
    summary: str | None = None
    error: str | None = None


def _transcript(messages: list[dict[str, str]], kind: str) -> str:
    labels = {"user": "Person", "assistant": "Assistant", "transcript": "Heard", "image": "Photo"}
    lines = []
    for message in messages:
        label = labels.get(message.get("role", ""))
        text = strip_history_markers(message.get("content") or "")
        if label and text and (kind == ConversationKind.TRANSCRIPT.value or label != "Photo"):
            lines.append(f"{label}: {text}")
    text = "\n".join(lines)
    return text[-_MAX_TRANSCRIPT_CHARS:]


def _parse(reply: str) -> tuple[str | None, str | None]:
    """Title and summary from the model's reply, tolerating fences and chatter."""
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if not match:
        return None, None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    title = str(data.get("title") or "").strip().strip('"').rstrip(".").strip()
    summary = str(data.get("summary") or "").strip()
    if title:
        words = title.split()
        title = " ".join(words[:_MAX_TITLE_WORDS])
    return title or None, summary or None


def _fallback_title(messages: list[dict[str, str]], max_words: int = 8) -> str | None:
    for roles in ({"user", "transcript"}, {"assistant"}):
        for message in messages:
            text = strip_history_markers(message.get("content") or "")
            if message.get("role") in roles and text:
                words = text.split()
                title = " ".join(words[:max_words])
                return title + "…" if len(words) > max_words else title
    return None


async def wrap_up_conversation(
    store: RegistryStore, config: dict[str, Any], conversation: Conversation
) -> WrapUpResult:
    """Title and summarize one ended conversation, as its settings allow."""
    agent = await store.get_agent(conversation.device_id)
    persona: Persona | None = None
    if conversation.persona_id is not None:
        persona = await store.get_persona_by_id(conversation.persona_id)
    if persona is None:
        persona = await store.get_persona_for_device(conversation.device_id)
    settings = effective_settings(persona, agent)
    messages = await store.conversation_messages(conversation.id)
    fallback = conversation.title or _fallback_title(messages)
    wants_title = settings.auto_title and conversation.title_source != "manual"
    wants_summary = settings.summarize

    turns = conversation.turn_count or 0
    if persona is None or not (wants_title or wants_summary) or turns < MIN_TURNS_FOR_MODEL:
        await store.save_wrap_up(
            conversation.id,
            title=None if conversation.title else fallback,
            title_source="fallback",
            summary=None,
            done=True,
            max_attempts=MAX_ATTEMPTS,
        )
        return WrapUpResult(status="skipped", title=fallback)

    instructions = (
        _TRANSCRIPT_INSTRUCTIONS
        if conversation.kind == ConversationKind.TRANSCRIPT.value
        else _CHAT_INSTRUCTIONS
    )
    try:
        llm = get_provider(persona.llm_provider, config, model_override=persona.llm_model or None)
        spend.bind_device(conversation.device_id)
        reply = await llm.complete(
            [{"role": "user", "content": _transcript(messages, conversation.kind)}],
            system_prompt=instructions,
        )
    except Exception as exc:  # noqa: BLE001 - any failure falls back and retries
        logger.bind(tag=_TAG).warning(f"Wrap-up of {conversation.public_id} failed: {exc}")
        await store.save_wrap_up(
            conversation.id,
            title=None if conversation.title else fallback,
            title_source="fallback",
            summary=None,
            done=False,
            max_attempts=MAX_ATTEMPTS,
        )
        return WrapUpResult(status="failed", title=fallback, error=str(exc)[:200])

    title, summary = _parse(reply)
    if not title and not summary:
        logger.bind(tag=_TAG).warning(
            f"Wrap-up of {conversation.public_id}: unreadable reply {reply[:120]!r}"
        )
        await store.save_wrap_up(
            conversation.id,
            title=None if conversation.title else fallback,
            title_source="fallback",
            summary=None,
            done=False,
            max_attempts=MAX_ATTEMPTS,
        )
        return WrapUpResult(status="failed", title=fallback, error="unreadable reply")

    final_title = title if (wants_title and title) else (None if conversation.title else fallback)
    await store.save_wrap_up(
        conversation.id,
        title=final_title,
        title_source="auto" if (wants_title and title) else "fallback",
        summary=summary if wants_summary else None,
        done=True,
        max_attempts=MAX_ATTEMPTS,
    )
    logger.bind(tag=_TAG).info(f"Wrapped up {conversation.public_id}: {final_title!r}")
    return WrapUpResult(status="model", title=final_title, summary=summary)


async def end_idle_conversations(store: RegistryStore, now: datetime | None = None) -> int:
    """End chat conversations whose silence has passed their agent's idle gap."""
    reference = (now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
    ended = 0
    for conversation in await store.open_chat_conversations():
        last = conversation.last_turn_at or conversation.started_at
        if last is None:
            continue
        agent = await store.get_agent(conversation.device_id)
        persona = await store.get_persona_for_device(conversation.device_id)
        idle = timedelta(minutes=effective_settings(persona, agent).idle_minutes)
        if reference - last.replace(tzinfo=None) > idle:
            await store.end_conversation(conversation.id)
            ended += 1
    return ended


async def sweep(store: RegistryStore, config: dict[str, Any]) -> dict[str, int]:
    """One pass: end idle conversations, then wrap up the ended ones."""
    counts = {"ended": await end_idle_conversations(store)}
    for conversation in await store.conversations_to_wrap_up(max_attempts=MAX_ATTEMPTS):
        result = await wrap_up_conversation(store, config, conversation)
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts
