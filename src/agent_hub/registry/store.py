"""SQLite-backed registry store for agents and personas."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_hub.registry.models import Agent, AgentKind, AgentStatus, Base, ConversationTurn, Persona

_DEFAULT_PERSONA_NAME = "hub-default"
_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. "
    "Keep responses concise and conversational — two sentences or fewer. "
    "For anything that can change, including date, time, weather, and live facts, "
    "always call the matching tool and answer only from the fresh tool result. "
    "Never reuse changing values from earlier conversation history, and never claim "
    "you used a tool unless a tool was actually called in the current turn."
)
_LEGACY_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. "
    "Keep responses concise and conversational — two sentences or fewer."
)


class RegistryStore:
    """SQLite-backed registry for agents and personas.

    Thread-safe via SQLAlchemy's async session factory. Call initialize()
    once at startup before any other method.
    """

    def __init__(self, db_path: str | Path = "data/registry.db") -> None:
        """Create the store.

        Args:
            db_path: Path to the SQLite database file. Parent dirs are
                created automatically.
        """
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        """Create tables and seed the hub-default persona if missing."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await self._migrate()
        async with self._sessions() as session:
            await self._ensure_default_persona(session)
        logger.info("Registry store initialized")

    async def _migrate(self) -> None:
        """Add columns introduced after the initial schema without Alembic."""
        new_columns = [
            "ALTER TABLE personas ADD COLUMN server_skills TEXT",
            "ALTER TABLE personas ADD COLUMN mcp_tools_allowlist TEXT",
            "ALTER TABLE personas ADD COLUMN memory_window INTEGER DEFAULT 20 NOT NULL",
        ]
        async with self._engine.begin() as conn:
            for stmt in new_columns:
                with suppress(Exception):
                    await conn.execute(text(stmt))

    async def _ensure_default_persona(self, session: AsyncSession) -> None:
        result = await session.execute(select(Persona).where(Persona.name == _DEFAULT_PERSONA_NAME))
        persona = result.scalar_one_or_none()
        if persona is None:
            session.add(
                Persona(
                    name=_DEFAULT_PERSONA_NAME,
                    llm_provider="openai",
                    tts_provider="edge",
                    asr_provider="funasr_onnx",
                    system_prompt=_DEFAULT_SYSTEM_PROMPT,
                )
            )
            await session.commit()
            logger.info(f"Seeded persona '{_DEFAULT_PERSONA_NAME}'")
        elif persona.system_prompt == _LEGACY_DEFAULT_SYSTEM_PROMPT:
            persona.system_prompt = _DEFAULT_SYSTEM_PROMPT
            await session.commit()
            logger.info(f"Updated persona '{_DEFAULT_PERSONA_NAME}' prompt")

    async def get_or_create_agent(
        self,
        device_id: str,
        kind: AgentKind = AgentKind.XIAOZHI,
        ip_address: str | None = None,
        firmware_version: str | None = None,
    ) -> Agent:
        """Return the agent row for device_id, creating it on first contact.

        New agents are auto-assigned the hub-default persona so they work
        immediately without any activation step.

        Args:
            device_id: MAC address or UUID identifying the device.
            kind: Agent kind; defaults to XIAOZHI.
            ip_address: Reported IP address from the check-in request.
            firmware_version: Reported firmware version string.

        Returns:
            The Agent row, newly created or with last_seen updated.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()

            if agent is None:
                persona_result = await session.execute(
                    select(Persona).where(Persona.name == _DEFAULT_PERSONA_NAME)
                )
                default_persona = persona_result.scalar_one()
                agent = Agent(
                    kind=kind.value,
                    device_id=device_id,
                    persona_id=default_persona.id,
                    ip_address=ip_address,
                    firmware_version=firmware_version,
                    status=AgentStatus.DISCOVERED.value,
                    last_seen=datetime.now(UTC),
                )
                session.add(agent)
                logger.info(f"Registered new agent {device_id!r} → '{_DEFAULT_PERSONA_NAME}'")
            else:
                agent.last_seen = datetime.now(UTC)
                if ip_address:
                    agent.ip_address = ip_address
                if firmware_version:
                    agent.firmware_version = firmware_version

            await session.commit()
            return agent

    async def set_agent_status(self, device_id: str, status: AgentStatus) -> None:
        """Update the lifecycle status of an agent.

        Args:
            device_id: The agent to update.
            status: New status value.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent:
                agent.status = status.value
                agent.last_seen = datetime.now(UTC)
                await session.commit()

    async def list_agents_with_personas(self) -> list[tuple[Agent, Persona | None]]:
        """Return all agents with their assigned persona, ordered by last_seen desc."""
        async with self._sessions() as session:
            result = await session.execute(
                select(Agent, Persona)
                .outerjoin(Persona, Agent.persona_id == Persona.id)
                .order_by(Agent.last_seen.desc())
            )
            return [(row[0], row[1]) for row in result.all()]

    async def list_agents(self) -> list[Agent]:
        """Return all registered agents ordered by last_seen descending.

        Returns:
            List of Agent rows (persona relationship not eagerly loaded).
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent))
            return list(result.scalars().all())

    async def get_persona_for_device(self, device_id: str) -> Persona | None:
        """Return the Persona assigned to device_id, or None.

        Args:
            device_id: The device to look up.

        Returns:
            Persona row or None if the device is not registered.
        """
        async with self._sessions() as session:
            result = await session.execute(
                select(Persona)
                .join(Agent, Agent.persona_id == Persona.id)
                .where(Agent.device_id == device_id)
            )
            return result.scalar_one_or_none()

    async def list_personas(self) -> list[Persona]:
        """Return all personas ordered by name."""
        async with self._sessions() as session:
            result = await session.execute(select(Persona).order_by(Persona.name))
            return list(result.scalars().all())

    async def update_persona_model(self, persona_name: str, model: str) -> bool:
        """Set the llm_model field on a persona. Returns True if found and updated."""
        async with self._sessions() as session:
            result = await session.execute(select(Persona).where(Persona.name == persona_name))
            persona = result.scalar_one_or_none()
            if persona is None:
                return False
            persona.llm_model = model
            await session.commit()
            return True

    async def get_persona_by_name(self, name: str) -> Persona | None:
        """Return a persona by name, or None."""
        async with self._sessions() as session:
            result = await session.execute(select(Persona).where(Persona.name == name))
            return result.scalar_one_or_none()

    async def find_best_persona_for_tools(self, tool_names: list[str]) -> Persona | None:
        """Return the most specific persona whose mcp_tools_allowlist is satisfied
        by tool_names, or None if no persona has a matching non-empty allowlist.

        "Most specific" means the longest allowlist that is still a subset of
        the device's available tools — so a camera-aware persona beats a generic one
        when both would otherwise qualify.
        """
        tool_set = set(tool_names)
        async with self._sessions() as session:
            result = await session.execute(
                select(Persona).where(Persona.mcp_tools_allowlist.isnot(None))
            )
            candidates = list(result.scalars().all())

        best: Persona | None = None
        best_score = -1
        for p in candidates:
            required = p.mcp_tools_allowlist_list
            if not required:
                continue
            if set(required).issubset(tool_set):
                score = len(required)
                if score > best_score:
                    best_score = score
                    best = p
        return best

    async def create_persona(
        self,
        name: str,
        *,
        system_prompt: str = "",
        llm_provider: str = "openai",
        llm_model: str | None = None,
        tts_provider: str = "edge",
        tts_voice: str | None = None,
        asr_provider: str = "funasr_onnx",
    ) -> Persona | None:
        """Create a new persona. Returns None if the name is already taken."""
        async with self._sessions() as session:
            existing = await session.execute(select(Persona).where(Persona.name == name))
            if existing.scalar_one_or_none() is not None:
                return None
            persona = Persona(
                name=name,
                system_prompt=system_prompt,
                llm_provider=llm_provider,
                llm_model=llm_model,
                tts_provider=tts_provider,
                tts_voice=tts_voice,
                asr_provider=asr_provider,
            )
            session.add(persona)
            await session.commit()
            await session.refresh(persona)
            logger.info(f"Created persona '{name}'")
            return persona

    async def assign_persona(self, device_id: str, persona_name: str) -> bool:
        """Assign a persona to a device by name. Returns False if either not found."""
        async with self._sessions() as session:
            agent_result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = agent_result.scalar_one_or_none()
            if agent is None:
                return False
            persona_result = await session.execute(
                select(Persona).where(Persona.name == persona_name)
            )
            persona = persona_result.scalar_one_or_none()
            if persona is None:
                return False
            agent.persona_id = persona.id
            agent.status = AgentStatus.CLAIMED.value
            await session.commit()
            logger.info(f"Assigned persona '{persona_name}' to agent '{device_id}'")
            return True

    async def update_persona(
        self,
        persona_name: str,
        *,
        system_prompt: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        tts_provider: str | None = None,
        tts_voice: str | None = None,
        asr_provider: str | None = None,
        server_skills: str | None = None,
        mcp_tools_allowlist: str | None = None,
        memory_window: int | None = None,
    ) -> bool:
        """Update editable fields on a persona. Returns False if not found."""
        async with self._sessions() as session:
            result = await session.execute(select(Persona).where(Persona.name == persona_name))
            persona = result.scalar_one_or_none()
            if persona is None:
                return False
            if system_prompt is not None:
                persona.system_prompt = system_prompt
            if llm_provider is not None:
                persona.llm_provider = llm_provider
            if llm_model is not None:
                persona.llm_model = llm_model or None
            if tts_provider is not None:
                persona.tts_provider = tts_provider
            if tts_voice is not None:
                persona.tts_voice = tts_voice or None
            if asr_provider is not None:
                persona.asr_provider = asr_provider
            if server_skills is not None:
                persona.server_skills = server_skills or None
            if mcp_tools_allowlist is not None:
                persona.mcp_tools_allowlist = mcp_tools_allowlist or None
            if memory_window is not None:
                persona.memory_window = memory_window
            await session.commit()
            return True

    async def load_history(self, device_id: str, limit: int = 40) -> list[dict[str, str]]:
        """Return the most recent messages for device_id, oldest first.

        Args:
            device_id: The device to load history for.
            limit: Maximum number of messages (not turns) to return.

        Returns:
            List of {role, content} dicts ready for LLM context.
        """
        async with self._sessions() as session:
            result = await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.device_id == device_id)
                .order_by(ConversationTurn.id.desc())
                .limit(limit)
            )
            rows = list(result.scalars().all())
        rows.reverse()
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    async def append_history(self, device_id: str, role: str, content: str) -> None:
        """Append one message to the persisted conversation history."""
        async with self._sessions() as session:
            session.add(ConversationTurn(device_id=device_id, role=role, content=content))
            await session.commit()

    async def clear_history(self, device_id: str) -> None:
        """Delete all conversation history for a device."""
        from sqlalchemy import delete

        async with self._sessions() as session:
            await session.execute(
                delete(ConversationTurn).where(ConversationTurn.device_id == device_id)
            )
            await session.commit()
        logger.info(f"Cleared conversation history for {device_id!r}")

    async def get_agent(self, device_id: str) -> Agent | None:
        """Return the agent row for device_id, or None if not found.

        Args:
            device_id: The device/agent to look up.

        Returns:
            Agent row or None.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            return result.scalar_one_or_none()
