"""SQLAlchemy ORM models for the agent registry."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class AgentKind(StrEnum):
    """Kinds of agents that can register with the hub."""

    XIAOZHI = "xiaozhi"  # physical ESP32 running xiaozhi firmware
    VOICE = "voice"  # software voice agent (e.g. Talkbot)
    MCP = "mcp"  # agent that exposes MCP tools
    AG2 = "ag2"  # AutoGen2 agent


class AgentStatus(StrEnum):
    """Lifecycle states for a registered agent.

    Transitions: DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE
    """

    DISCOVERED = "discovered"  # seen on check-in, no persona claimed by a user
    CLAIMED = "claimed"  # a user has assigned a persona
    ACTIVE = "active"  # currently in a voice session
    IDLE = "idle"  # connected but not speaking
    OFFLINE = "offline"  # has not checked in recently


class Base(DeclarativeBase):
    pass


class Persona(Base):
    """A reusable configuration template (LLM + TTS + ASR + system prompt)."""

    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    llm_provider: Mapped[str] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tts_provider: Mapped[str] = mapped_column(String(64))
    tts_voice: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asr_provider: Mapped[str] = mapped_column(String(64))
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    # JSON-encoded list of enabled skill names; NULL means all skills enabled
    server_skills: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON-encoded list of allowed device MCP tool names; NULL means all allowed
    mcp_tools_allowlist: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Max conversation turns kept in LLM context
    memory_window: Mapped[int] = mapped_column(Integer, default=20)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    agents: Mapped[list[Agent]] = relationship(back_populates="persona")

    @property
    def server_skills_list(self) -> list[str] | None:
        """Decoded server_skills, or None (all skills enabled)."""
        return json.loads(self.server_skills) if self.server_skills else None

    @property
    def mcp_tools_allowlist_list(self) -> list[str] | None:
        """Decoded mcp_tools_allowlist, or None (all tools allowed)."""
        return json.loads(self.mcp_tools_allowlist) if self.mcp_tools_allowlist else None


class ConversationTurn(Base):
    """One message in a device's persisted conversation history."""

    __tablename__ = "conversation_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # 'user' or 'assistant'
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Agent(Base):
    """A registered agent — ESP32 device, voice agent, or custom agent."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # AgentKind value
    device_id: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=AgentStatus.DISCOVERED.value)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"))
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    persona: Mapped[Persona] = relationship(back_populates="agents")
