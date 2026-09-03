"""SQLAlchemy ORM models for the agent registry."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class AgentKind(StrEnum):
    """Kinds of agents that can register with the hub."""

    XIAOZHI = "xiaozhi"  # physical ESP32 running xiaozhi firmware
    VOICE = "voice"  # software voice agent (e.g. Talkbot)
    MCP = "mcp"  # agent that exposes MCP tools
    AG2 = "ag2"  # AutoGen2 agent
    PAGE = "page"  # browser page agent (talking + seeing) acting as an MCP server


class AgentStatus(StrEnum):
    """Lifecycle states for a registered agent.

    Transitions: DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE
    """

    DISCOVERED = "discovered"  # seen on check-in, no persona claimed by a user
    CLAIMED = "claimed"  # a user has assigned a persona
    ACTIVE = "active"  # currently in a voice session
    IDLE = "idle"  # connected but not speaking
    OFFLINE = "offline"  # has not checked in recently


class OperatorRole(StrEnum):
    """Dashboard authorization levels for authenticated human operators."""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


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
    # JSON-encoded list of other agent (device) ids this persona may borrow
    # non-destructive MCP tools from; NULL/[] means none.
    linked_agents: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Max conversation turns kept in LLM context
    memory_window: Mapped[int] = mapped_column(Integer, default=20)
    # When true this is a transcriber, not an assistant: the device streams
    # audio continuously and the hub logs each utterance via ASR with no LLM
    # or TTS. TTS/prompt/skills/linked-agent settings are ignored.
    transcription: Mapped[bool] = mapped_column(Boolean, default=False)
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

    @property
    def linked_agents_list(self) -> list[str]:
        """Decoded linked_agents, or [] when none are linked."""
        if not self.linked_agents:
            return []
        parsed = json.loads(self.linked_agents)
        return [a for a in parsed if isinstance(a, str)] if isinstance(parsed, list) else []


class ConversationTurn(Base):
    """One message in a device's persisted conversation history."""

    __tablename__ = "conversation_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))  # 'user' or 'assistant'
    content: Mapped[str] = mapped_column(Text)
    # Groups the turns of one transcription session (one start→stop of a
    # transcriber device). NULL for ordinary assistant turns.
    session_id: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LLMSpend(Base):
    """One billed LLM call — the ledger behind the spend metrics and limits."""

    __tablename__ = "llm_spend"

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL for calls with no device behind them (page agent, image explain).
    device_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    # True when cost came from the local price table rather than the provider.
    # An estimate is only as good as the configured prices, so the dashboard
    # says so rather than presenting a guess as billing truth.
    cost_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class DashboardOperator(Base):
    """A Cloudflare Access identity authorized to use the dashboard."""

    __tablename__ = "dashboard_operators"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(16), default=OperatorRole.VIEWER.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AuditEvent(Base):
    """Privacy-minimal record of one authenticated dashboard mutation."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    operator_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    operator_email: Mapped[str] = mapped_column(String(320), index=True)
    operator_role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(128), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


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
    websocket_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(nullable=True)
    health_fault: Mapped[str | None] = mapped_column(Text, nullable=True)
    reported_activity: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reported_mcp_tools: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A long-term agent: never counted as stale, never pruned. Set from the
    # dashboard for the boards and pages that are part of the furniture.
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    persona: Mapped[Persona] = relationship(back_populates="agents")

    @property
    def reported_mcp_tools_list(self) -> list[str]:
        """Return MCP capability names from the latest authenticated heartbeat."""
        if not self.reported_mcp_tools:
            return []
        try:
            parsed = json.loads(self.reported_mcp_tools)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        return [name for name in parsed if isinstance(name, str)]
