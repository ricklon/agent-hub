"""SQLAlchemy ORM models for the agent registry."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class AgentKind(str, enum.Enum):
    """Kinds of agents that can register with the hub."""

    XIAOZHI = "xiaozhi"  # physical ESP32 running xiaozhi firmware
    VOICE = "voice"      # software voice agent (e.g. Talkbot)
    MCP = "mcp"          # agent that exposes MCP tools
    AG2 = "ag2"          # AutoGen2 agent


class AgentStatus(str, enum.Enum):
    """Lifecycle states for a registered agent.

    Transitions: DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE
    """

    DISCOVERED = "discovered"  # seen on check-in, no persona claimed by a user
    CLAIMED = "claimed"        # a user has assigned a persona
    ACTIVE = "active"          # currently in a voice session
    IDLE = "idle"              # connected but not speaking
    OFFLINE = "offline"        # has not checked in recently


class Base(DeclarativeBase):
    pass


class Persona(Base):
    """A reusable configuration template (LLM + TTS + ASR + system prompt)."""

    __tablename__ = "personas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    llm_provider: Mapped[str] = mapped_column(String(64))
    llm_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tts_provider: Mapped[str] = mapped_column(String(64))
    tts_voice: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    asr_provider: Mapped[str] = mapped_column(String(64))
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    agents: Mapped[list[Agent]] = relationship(back_populates="persona")


class Agent(Base):
    """A registered agent — ESP32 device, voice agent, or custom agent."""

    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # AgentKind value
    device_id: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=AgentStatus.DISCOVERED.value)
    persona_id: Mapped[int] = mapped_column(ForeignKey("personas.id"))
    ip_address: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    firmware_version: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_seen: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    persona: Mapped[Persona] = relationship(back_populates="agents")
