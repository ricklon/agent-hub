"""Conversation settings, and the one place turn paths resolve a conversation.

A persona carries the defaults (how long a silence ends a conversation, how
many recent turns the model sees, titles, summaries, remembered conversations)
and an agent can override any of them. Every turn path, whether device voice,
page voice, or dashboard/page text, asks :func:`conversation_for_turn` which
conversation it is in, so the boundary rules live in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_hub.registry.models import Agent, Conversation, ConversationKind, Persona
from agent_hub.registry.store import RegistryStore

# Used when a persona row predates a setting (or no persona is assigned).
DEFAULTS: dict[str, Any] = {
    "conversation_idle_minutes": 30,
    "memory_window": 20,
    "auto_title": True,
    "summarize_conversations": True,
    "remember_conversations": 3,
}


@dataclass(frozen=True)
class ConversationSettings:
    """The settings in force for one agent, and where each one came from."""

    idle_minutes: int
    memory_window: int
    auto_title: bool
    summarize: bool
    remember: int
    # Setting name → "agent" (overridden on the agent) or "persona".
    sources: dict[str, str] = field(default_factory=dict)


def effective_settings(persona: Persona | None, agent: Agent | None = None) -> ConversationSettings:
    """Resolve each setting: the agent's override if set, else the persona's."""
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, default in DEFAULTS.items():
        override = getattr(agent, name, None) if agent is not None else None
        if override is not None:
            values[name], sources[name] = override, "agent"
            continue
        own = getattr(persona, name, None) if persona is not None else None
        values[name], sources[name] = (default if own is None else own), "persona"
    return ConversationSettings(
        idle_minutes=max(1, int(values["conversation_idle_minutes"])),
        memory_window=max(1, int(values["memory_window"])),
        auto_title=bool(values["auto_title"]),
        summarize=bool(values["summarize_conversations"]),
        remember=max(0, int(values["remember_conversations"])),
        sources=sources,
    )


async def settings_for_device(store: RegistryStore, device_id: str) -> ConversationSettings:
    """Effective settings for a device, from its agent row and persona."""
    agent = await store.get_agent(device_id)
    persona = await store.get_persona_for_device(device_id)
    return effective_settings(persona, agent)


async def conversation_for_turn(
    store: RegistryStore,
    device_id: str,
    persona: Persona | None,
    settings: ConversationSettings,
) -> Conversation:
    """The chat conversation a new turn belongs to, starting one at a boundary.

    Boundaries: no open conversation, silence longer than the idle gap, or a
    different persona than the open conversation ran under.
    """
    conversation = await store.open_conversation(
        device_id,
        persona=persona,
        idle_minutes=settings.idle_minutes,
        kind=ConversationKind.CHAT,
        create=True,
    )
    assert conversation is not None  # create=True always returns one
    return conversation
