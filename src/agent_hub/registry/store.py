"""SQLite-backed registry store for agents and personas."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_hub.registry.models import Agent, AgentKind, AgentStatus, Base, Persona

_DEFAULT_PERSONA_NAME = "hub-default"
_DEFAULT_SYSTEM_PROMPT = (
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
        self._engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}", echo=False
        )
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)

    async def initialize(self) -> None:
        """Create tables and seed the hub-default persona if missing."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self._sessions() as session:
            await self._ensure_default_persona(session)
        logger.info("Registry store initialized")

    async def _ensure_default_persona(self, session: AsyncSession) -> None:
        result = await session.execute(
            select(Persona).where(Persona.name == _DEFAULT_PERSONA_NAME)
        )
        if result.scalar_one_or_none() is None:
            session.add(
                Persona(
                    name=_DEFAULT_PERSONA_NAME,
                    llm_provider="openai",
                    tts_provider="edge",
                    asr_provider="funasr",
                    system_prompt=_DEFAULT_SYSTEM_PROMPT,
                )
            )
            await session.commit()
            logger.info(f"Seeded persona '{_DEFAULT_PERSONA_NAME}'")

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
            result = await session.execute(
                select(Agent).where(Agent.device_id == device_id)
            )
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
            result = await session.execute(
                select(Agent).where(Agent.device_id == device_id)
            )
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
            result = await session.execute(
                select(Persona).where(Persona.name == persona_name)
            )
            persona = result.scalar_one_or_none()
            if persona is None:
                return False
            persona.llm_model = model
            await session.commit()
            return True

    async def get_agent(self, device_id: str) -> Agent | None:
        """Return the agent row for device_id, or None if not found.

        Args:
            device_id: The device/agent to look up.

        Returns:
            Agent row or None.
        """
        async with self._sessions() as session:
            result = await session.execute(
                select(Agent).where(Agent.device_id == device_id)
            )
            return result.scalar_one_or_none()
