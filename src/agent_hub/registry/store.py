"""SQLite-backed registry store for agents and personas."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_hub.providers.asr import first_available as asr_first_available
from agent_hub.providers.asr import is_available as asr_is_available
from agent_hub.registry.models import (
    Agent,
    AgentKind,
    AgentStatus,
    AuditEvent,
    Base,
    Conversation,
    ConversationKind,
    ConversationTurn,
    DashboardOperator,
    LLMSpend,
    OperatorRole,
    Persona,
)
from agent_hub.registry.page_identity import is_named_page_agent

_DEFAULT_PERSONA_NAME = "hub-default"
# Idle gap used to split history written before conversations existed.
_BACKFILL_IDLE_MINUTES = 30
# Turns that count toward a conversation's turn_count.
_COUNTED_ROLES = frozenset({"user", "transcript"})


def _utcnow() -> datetime:
    """Naive UTC, matching what SQLite's CURRENT_TIMESTAMP stores."""
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


_TRANSCRIBER_PERSONA_NAME = "transcriber"
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

    def __init__(
        self,
        db_path: str | Path = "data/registry.db",
        default_asr_provider: str = "funasr_onnx",
    ) -> None:
        """Create the store.

        Args:
            db_path: Path to the SQLite database file. Parent dirs are
                created automatically.
            default_asr_provider: ASR provider for the seeded default persona,
                and the fallback when a persona names one this build cannot
                run. Should match asr.default_provider in the config.
        """
        self._default_asr_provider = default_asr_provider
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._init_lock = asyncio.Lock()
        self._operator_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """Create tables and seed the hub-default persona if missing.

        Safe to call concurrently and repeatedly: the server binds one app per
        port and every app's startup hook calls this against the shared store.
        Without the lock, a fresh database lets all of them pass create_all's
        existence check together and the losers fail with "table already exists".
        """
        async with self._init_lock:
            if self._initialized:
                return
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await self._migrate()
            async with self._sessions() as session:
                await self._ensure_default_persona(session)
                await self._ensure_transcriber_persona(session)
            await self._backfill_conversations()
            self._initialized = True
            logger.info("Registry store initialized")

    async def _migrate(self) -> None:
        """Add columns introduced after the initial schema without Alembic."""
        new_columns = [
            "ALTER TABLE personas ADD COLUMN server_skills TEXT",
            "ALTER TABLE personas ADD COLUMN mcp_tools_allowlist TEXT",
            "ALTER TABLE personas ADD COLUMN linked_agents TEXT",
            "ALTER TABLE personas ADD COLUMN memory_window INTEGER DEFAULT 20 NOT NULL",
            "ALTER TABLE personas ADD COLUMN transcription BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE conversation_history ADD COLUMN session_id VARCHAR(48)",
            "ALTER TABLE agents ADD COLUMN websocket_token VARCHAR(128)",
            "ALTER TABLE agents ADD COLUMN last_heartbeat DATETIME",
            "ALTER TABLE agents ADD COLUMN health_fault TEXT",
            "ALTER TABLE agents ADD COLUMN reported_activity VARCHAR(32)",
            "ALTER TABLE agents ADD COLUMN reported_mcp_tools TEXT",
            "ALTER TABLE agents ADD COLUMN pinned BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE agents ADD COLUMN owner VARCHAR(64)",
            "ALTER TABLE agents ADD COLUMN owner_subject VARCHAR(255)",
            # Conversations and memory.
            "ALTER TABLE conversation_history ADD COLUMN conversation_id INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_conversation_history_conversation_id "
            "ON conversation_history (conversation_id)",
            "ALTER TABLE personas ADD COLUMN conversation_idle_minutes INTEGER DEFAULT 30 NOT NULL",
            "ALTER TABLE personas ADD COLUMN auto_title BOOLEAN DEFAULT 1 NOT NULL",
            "ALTER TABLE personas ADD COLUMN summarize_conversations BOOLEAN DEFAULT 1 NOT NULL",
            "ALTER TABLE personas ADD COLUMN remember_conversations INTEGER DEFAULT 3 NOT NULL",
            "ALTER TABLE agents ADD COLUMN conversation_idle_minutes INTEGER",
            "ALTER TABLE agents ADD COLUMN memory_window INTEGER",
            "ALTER TABLE agents ADD COLUMN auto_title BOOLEAN",
            "ALTER TABLE agents ADD COLUMN summarize_conversations BOOLEAN",
            "ALTER TABLE agents ADD COLUMN remember_conversations INTEGER",
            "ALTER TABLE conversations ADD COLUMN wrap_up_attempts INTEGER DEFAULT 0 NOT NULL",
            "ALTER TABLE dashboard_operators ADD COLUMN paid_models BOOLEAN DEFAULT 0 NOT NULL",
            "ALTER TABLE personas ADD COLUMN tts_model VARCHAR(128)",
        ]
        async with self._engine.begin() as conn:
            for stmt in new_columns:
                with suppress(Exception):
                    await conn.execute(text(stmt))

    async def _backfill_conversations(self) -> None:
        """Group history written before conversations existed into conversations.

        Idempotent: only rows with no ``conversation_id`` are touched, so it
        costs one indexed count on every start after the first. Transcript rows
        keep their listen session as the conversation (its id becomes the
        public id). Chat rows are split per device by a 30-minute idle gap.
        Every backfilled conversation is ended except each device's latest chat
        one, which the normal idle-gap rule then continues or closes. No model
        is called; titles come from the first words and are marked fallback.
        """
        async with self._sessions() as session:
            pending = int(
                await session.scalar(
                    select(func.count(ConversationTurn.id)).where(
                        ConversationTurn.conversation_id.is_(None)
                    )
                )
                or 0
            )
            if not pending:
                return
            rows = (
                (
                    await session.execute(
                        select(ConversationTurn)
                        .where(ConversationTurn.conversation_id.is_(None))
                        .order_by(ConversationTurn.device_id, ConversationTurn.id)
                    )
                )
                .scalars()
                .all()
            )
            personas = {
                a.device_id: p
                for a, p in (
                    await session.execute(
                        select(Agent, Persona).outerjoin(Persona, Agent.persona_id == Persona.id)
                    )
                ).all()
            }

            groups: list[tuple[str, str, str | None, list[ConversationTurn]]] = []
            open_chat: dict[str, list[ConversationTurn]] = {}
            by_session: dict[tuple[str, str], list[ConversationTurn]] = {}
            gap = timedelta(minutes=_BACKFILL_IDLE_MINUTES)
            for row in rows:
                if row.session_id:
                    key = (row.device_id, row.session_id)
                    if key not in by_session:
                        by_session[key] = []
                        groups.append(
                            (
                                row.device_id,
                                ConversationKind.TRANSCRIPT.value,
                                row.session_id,
                                by_session[key],
                            )
                        )
                    by_session[key].append(row)
                    continue
                current = open_chat.get(row.device_id)
                if current is None or row.created_at - current[-1].created_at > gap:
                    current = []
                    open_chat[row.device_id] = current
                    groups.append((row.device_id, ConversationKind.CHAT.value, None, current))
                current.append(row)
            latest_chat = {device_id: turns[0].id for device_id, turns in open_chat.items()}

            for device_id, kind, session_id, turns in groups:
                persona = personas.get(device_id)
                first = turns[0]
                still_open = (
                    kind == ConversationKind.CHAT.value and latest_chat.get(device_id) == first.id
                )
                conversation = Conversation(
                    public_id=session_id or secrets.token_hex(8),
                    device_id=device_id,
                    kind=kind,
                    persona_id=persona.id if persona else None,
                    persona_name=persona.name if persona else None,
                    title=_fallback_title(turns),
                    title_source="fallback",
                    started_at=first.created_at,
                    last_turn_at=turns[-1].created_at,
                    ended_at=None if still_open else turns[-1].created_at,
                    turn_count=sum(1 for t in turns if t.role in _COUNTED_ROLES),
                )
                session.add(conversation)
                await session.flush()
                for turn in turns:
                    turn.conversation_id = conversation.id
            await session.commit()
        logger.info(f"Backfilled {len(groups)} conversation(s) from {pending} message(s)")

    async def _ensure_default_persona(self, session: AsyncSession) -> None:
        result = await session.execute(select(Persona).where(Persona.name == _DEFAULT_PERSONA_NAME))
        persona = result.scalar_one_or_none()
        if persona is None:
            # Seed with a provider this build can actually run: the configured
            # default if installed, otherwise any installed one.
            asr_provider = (
                asr_first_available(self._default_asr_provider) or self._default_asr_provider
            )
            session.add(
                Persona(
                    name=_DEFAULT_PERSONA_NAME,
                    llm_provider="openai",
                    tts_provider="edge",
                    asr_provider=asr_provider,
                    system_prompt=_DEFAULT_SYSTEM_PROMPT,
                )
            )
            await session.commit()
            logger.info(f"Seeded persona '{_DEFAULT_PERSONA_NAME}' (asr={asr_provider})")
        else:
            updates: list[str] = []
            if persona.system_prompt == _LEGACY_DEFAULT_SYSTEM_PROMPT:
                persona.system_prompt = _DEFAULT_SYSTEM_PROMPT
                updates.append("prompt")
            # Early Agent Hub databases seeded the default persona with the
            # native FunASR provider. Container deployments package the ONNX
            # model, so retaining that legacy value leaves a connected device
            # listening while every transcription silently fails.
            if persona.asr_provider in {"funasr", "fun_local"}:
                persona.asr_provider = "funasr_onnx"
                updates.append("ASR provider")
            # Same failure one generation on: the slim container images ship
            # only one ASR provider, so a persona naming an absent one leaves
            # the microphone live while every transcription silently returns
            # nothing. Fall back to a provider this build can actually run.
            if self._repair_persona_asr(persona):
                updates.append("ASR provider (not installed)")
            if updates:
                await session.commit()
                logger.info(f"Updated persona '{_DEFAULT_PERSONA_NAME}' {', '.join(updates)}")

    def _repair_persona_asr(self, persona: Persona) -> bool:
        """Point a persona at an installed ASR provider if its own is absent.

        Returns True if it was rewritten. The slim container images ship only
        one ASR provider, so a persona naming an absent one leaves the
        microphone live while every transcription silently returns nothing.
        Prefers the configured default, then any provider this build can run —
        so a stale ``funasr_onnx`` survives a switch to a Moonshine-only image.
        """
        if asr_is_available(persona.asr_provider):
            return False
        target = asr_first_available(self._default_asr_provider)
        if target is None or target == persona.asr_provider:
            return False
        logger.warning(
            f"Persona '{persona.name}' uses ASR provider "
            f"{persona.asr_provider!r}, which is not installed in this build — "
            f"falling back to {target!r}."
        )
        persona.asr_provider = target
        return True

    async def _ensure_transcriber_persona(self, session: AsyncSession) -> None:
        """Seed the built-in ``transcriber`` persona if missing.

        Assigning it to a device switches that device to transcription mode:
        the hub logs each utterance via ASR with no LLM or TTS. The LLM and
        TTS providers are set only to satisfy the non-null schema; they are
        never used.
        """
        result = await session.execute(
            select(Persona).where(Persona.name == _TRANSCRIBER_PERSONA_NAME)
        )
        persona = result.scalar_one_or_none()
        if persona is not None:
            if self._repair_persona_asr(persona):
                await session.commit()
                logger.info(f"Updated persona '{_TRANSCRIBER_PERSONA_NAME}' ASR provider")
            return
        # Inherit hub-default's ASR provider — it was just ensured and repaired
        # to something this build can run, which the raw configured default may
        # not be (e.g. a Moonshine-only image with the funasr_onnx default).
        hub_default = await session.scalar(
            select(Persona).where(Persona.name == _DEFAULT_PERSONA_NAME)
        )
        asr_provider = (
            hub_default.asr_provider
            if hub_default and asr_is_available(hub_default.asr_provider)
            else (asr_first_available(self._default_asr_provider) or self._default_asr_provider)
        )
        session.add(
            Persona(
                name=_TRANSCRIBER_PERSONA_NAME,
                llm_provider="openai",
                tts_provider="edge",
                asr_provider=asr_provider,
                system_prompt="",
                server_skills="[]",
                transcription=True,
            )
        )
        await session.commit()
        logger.info(f"Seeded persona '{_TRANSCRIBER_PERSONA_NAME}' (asr={asr_provider})")

    async def get_or_create_agent(
        self,
        device_id: str,
        kind: AgentKind = AgentKind.XIAOZHI,
        label: str | None = None,
        ip_address: str | None = None,
        firmware_version: str | None = None,
        owner: str | None = None,
        owner_subject: str | None = None,
    ) -> Agent:
        """Return the agent row for device_id, creating it on first contact.

        New agents are auto-assigned the hub-default persona so they work
        immediately without any activation step.

        Args:
            device_id: MAC address or UUID identifying the device.
            kind: Agent kind; defaults to XIAOZHI.
            label: Human-readable device name reported by the firmware.
            ip_address: Reported IP address from the check-in request.
            firmware_version: Reported firmware version string.
            owner: Owner label to record when the row is created.
            owner_subject: Verified Access subject to record when the row is
                created. Neither owner field changes on an existing row, so a
                later registration cannot move an agent between people.

        Returns:
            The Agent row, newly created or with last_seen updated.
        """
        async with self._sessions() as session:
            now = datetime.now(UTC)
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
                    label=label,
                    persona_id=default_persona.id,
                    ip_address=ip_address,
                    firmware_version=firmware_version,
                    status=AgentStatus.DISCOVERED.value,
                    last_heartbeat=now,
                    reported_activity="idle",
                    last_seen=now,
                    owner=(owner or "").strip()[:64] or None,
                    owner_subject=owner_subject or None,
                )
                session.add(agent)
                logger.info(f"Registered new agent {device_id!r} → '{_DEFAULT_PERSONA_NAME}'")
            else:
                # A valid check-in is positive proof that the device is alive,
                # even before its periodic heartbeat loop starts.
                agent.last_heartbeat = now
                agent.reported_activity = "idle"
                agent.last_seen = now
                if label:
                    agent.label = label
                if ip_address:
                    agent.ip_address = ip_address
                if firmware_version:
                    agent.firmware_version = firmware_version

            await session.commit()
            return agent

    async def get_or_create_dashboard_operator(
        self,
        subject: str,
        email: str,
        admin_emails: set[str],
        default_role: str = OperatorRole.VIEWER.value,
    ) -> DashboardOperator:
        """Resolve a verified Access identity to its dashboard authorization row.

        New identities get ``default_role`` unless their normalized email is
        in the configured bootstrap-admin set. A configured bootstrap admin is
        also promoted on a later login, which makes initial deployment
        recoverable without granting elevated access to the first arbitrary
        visitor.

        Args:
            subject: Stable Cloudflare Access subject identifier.
            email: Verified email claim from the Access assertion.
            admin_emails: Normalized emails explicitly configured as admins.
            default_role: Role for an identity the hub has never seen.

        Returns:
            Persisted operator row with current email and last-seen time.
        """
        async with self._operator_lock:
            return await self._get_or_create_dashboard_operator(
                subject, email, admin_emails, default_role
            )

    async def _get_or_create_dashboard_operator(
        self,
        subject: str,
        email: str,
        admin_emails: set[str],
        default_role: str = OperatorRole.VIEWER.value,
    ) -> DashboardOperator:
        """Provision one operator while the process-local identity lock is held."""
        normalized_email = email.strip().lower()
        async with self._sessions() as session:
            result = await session.execute(
                select(DashboardOperator).where(DashboardOperator.subject == subject)
            )
            operator = result.scalar_one_or_none()
            now = datetime.now(UTC)
            if operator is None:
                role_value = (
                    OperatorRole.ADMIN.value if normalized_email in admin_emails else default_role
                )
                operator = DashboardOperator(
                    subject=subject,
                    email=normalized_email,
                    role=role_value,
                    enabled=True,
                    last_seen_at=now,
                )
                session.add(operator)
                logger.info(f"Provisioned dashboard operator {normalized_email!r} as {role_value}")
                changed = True
            else:
                changed = operator.email != normalized_email
                operator.email = normalized_email
                previous_seen = operator.last_seen_at
                if previous_seen.tzinfo is None:
                    previous_seen = previous_seen.replace(tzinfo=UTC)
                if now - previous_seen >= timedelta(minutes=1):
                    operator.last_seen_at = now
                    changed = True
                if normalized_email in admin_emails and operator.role != OperatorRole.ADMIN.value:
                    operator.role = OperatorRole.ADMIN.value
                    changed = True
                    logger.info(f"Promoted bootstrap dashboard admin {normalized_email!r}")
            if changed:
                await session.commit()
                await session.refresh(operator)
            return operator

    async def list_dashboard_operators(self) -> list[DashboardOperator]:
        """Return dashboard operators ordered by email."""
        async with self._sessions() as session:
            result = await session.execute(
                select(DashboardOperator).order_by(DashboardOperator.email)
            )
            return list(result.scalars().all())

    async def record_audit_event(
        self,
        *,
        operator_subject: str | None,
        operator_email: str,
        operator_role: str,
        action: str,
        target_type: str | None,
        target_id: str | None,
        outcome: str,
        status_code: int,
    ) -> None:
        """Persist privacy-minimal metadata for one dashboard mutation.

        Request bodies and arbitrary details are deliberately absent from this
        API so prompts, transcripts, tokens, and form values cannot enter the
        audit ledger accidentally.
        """
        async with self._sessions() as session:
            session.add(
                AuditEvent(
                    operator_subject=operator_subject,
                    operator_email=operator_email,
                    operator_role=operator_role,
                    action=action,
                    target_type=target_type,
                    target_id=target_id,
                    outcome=outcome,
                    status_code=status_code,
                )
            )
            await session.commit()

    async def list_audit_events(self, limit: int = 200) -> list[AuditEvent]:
        """Return the newest dashboard audit events first.

        Args:
            limit: Maximum rows to return, clamped to 1–1000.
        """
        safe_limit = min(1000, max(1, limit))
        async with self._sessions() as session:
            result = await session.execute(
                select(AuditEvent).order_by(AuditEvent.id.desc()).limit(safe_limit)
            )
            return list(result.scalars().all())

    async def update_dashboard_operator(
        self,
        subject: str,
        role: OperatorRole,
        *,
        enabled: bool,
        paid_models: bool | None = None,
    ) -> bool:
        """Update an operator while preserving at least one enabled admin.

        Args:
            subject: Stable Cloudflare Access subject identifier.
            role: New authorization role.
            enabled: Whether this identity may use the dashboard.
            paid_models: In free mode, whether their agents may run paid
                models; None leaves it unchanged.

        Returns:
            True when updated. False means the row was missing or the change
            would remove the final enabled administrator.
        """
        async with self._operator_lock:
            return await self._update_dashboard_operator(
                subject, role, enabled=enabled, paid_models=paid_models
            )

    async def get_dashboard_operator(self, subject: str) -> DashboardOperator | None:
        """Return the operator row for a Cloudflare Access subject, if any."""
        async with self._sessions() as session:
            result = await session.execute(
                select(DashboardOperator).where(DashboardOperator.subject == subject)
            )
            return result.scalar_one_or_none()

    async def _update_dashboard_operator(
        self,
        subject: str,
        role: OperatorRole,
        *,
        enabled: bool,
        paid_models: bool | None = None,
    ) -> bool:
        """Apply an operator update while the process-local role lock is held."""
        async with self._sessions() as session:
            result = await session.execute(
                select(DashboardOperator).where(DashboardOperator.subject == subject)
            )
            operator = result.scalar_one_or_none()
            if operator is None:
                return False
            removes_admin = (
                operator.enabled
                and operator.role == OperatorRole.ADMIN.value
                and (not enabled or role != OperatorRole.ADMIN)
            )
            if removes_admin:
                count = await session.scalar(
                    select(func.count(DashboardOperator.id)).where(
                        DashboardOperator.enabled.is_(True),
                        DashboardOperator.role == OperatorRole.ADMIN.value,
                    )
                )
                if int(count or 0) <= 1:
                    return False
            operator.role = role.value
            operator.enabled = enabled
            if paid_models is not None:
                operator.paid_models = paid_models
            await session.commit()
            return True

    async def record_authenticated_heartbeat(
        self,
        device_id: str,
        token: str,
        fault: str | None,
        activity: str,
        mcp_tools: list[str],
    ) -> bool:
        """Record an authenticated liveness heartbeat and optional fault.

        Args:
            device_id: Device sending the heartbeat.
            token: Per-device token issued during check-in.
            fault: Current device fault, or None when healthy.
            activity: Current device activity reported independently of health.
            mcp_tools: Device capability names available when its control channel is open.

        Returns:
            True when the device and token are valid; otherwise False.
        """
        if not token:
            return False
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None or not agent.websocket_token:
                return False
            if not secrets.compare_digest(agent.websocket_token, token):
                return False
            now = datetime.now(UTC)
            agent.last_heartbeat = now
            agent.last_seen = now
            agent.health_fault = fault
            agent.reported_activity = activity
            agent.reported_mcp_tools = json.dumps(mcp_tools)
            await session.commit()
            return True

    async def issue_websocket_token(self, device_id: str) -> str:
        """Create or replace the WebSocket bearer token for device_id.

        Args:
            device_id: Registered device receiving the token.

        Returns:
            Newly issued opaque token, or an empty string when the device is unknown.
        """
        token = secrets.token_urlsafe(32)
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None:
                return ""
            agent.websocket_token = token
            agent.last_seen = datetime.now(UTC)
            await session.commit()
            return token

    async def validate_websocket_token(self, device_id: str, token: str) -> bool:
        """Return True when token matches the device's current WebSocket token.

        Args:
            device_id: Device attempting to open a WebSocket session.
            token: Bearer token supplied by the device.

        Returns:
            True if the token matches the current registry row.
        """
        if not token:
            return False
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None or not agent.websocket_token:
                return False
            return secrets.compare_digest(agent.websocket_token, token)

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

    async def mark_agent_offline(self, device_id: str, token: str) -> bool:
        """Record that an agent announced it is going away.

        Used by page agents on tab close. Clears the heartbeat so health
        reads offline immediately instead of after the heartbeat timeout.

        Args:
            device_id: The departing agent.
            token: Its current token; the call is ignored when it does not match.

        Returns:
            True when the agent existed and the token matched.
        """
        if not token:
            return False
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None or not agent.websocket_token:
                return False
            if not secrets.compare_digest(agent.websocket_token, token):
                return False
            agent.status = AgentStatus.OFFLINE.value
            agent.last_heartbeat = None
            agent.reported_activity = "idle"
            agent.last_seen = datetime.now(UTC)
            await session.commit()
            return True

    async def delete_agent(self, device_id: str, *, keep_history: bool = False) -> bool:
        """Remove an agent and, unless kept, its conversation history.

        Spend rows are always kept. Kept history is keyed by device id, so an
        agent that comes back with the same id (a named page agent reopened,
        a board checking in again) picks it up where it left off.

        Args:
            device_id: The agent to remove.
            keep_history: Leave the agent's conversation history in place.

        Returns:
            True when a row was deleted.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None:
                return False
            if not keep_history:
                await session.execute(
                    delete(ConversationTurn).where(ConversationTurn.device_id == device_id)
                )
                await session.execute(
                    delete(Conversation).where(Conversation.device_id == device_id)
                )
            await session.delete(agent)
            await session.commit()
            kept = ", history kept" if keep_history else ""
            logger.info(f"Removed agent {device_id!r} ({agent.kind}{kept})")
            return True

    async def list_stale_agents(
        self,
        *,
        device_after: timedelta,
        page_after: timedelta,
        now: datetime | None = None,
    ) -> list[Agent]:
        """Agents not seen for longer than the threshold for their kind.

        Per-tab page agents are ephemeral so they go stale much sooner than a
        board that is merely powered off for the week. A named page agent is
        meant to be reopened, so it gets the device threshold.

        Args:
            device_after: Staleness threshold for devices, robots, and named
                page agents.
            page_after: Staleness threshold for per-tab page agents.
            now: Reference time; defaults to now (UTC).

        Returns:
            Stale agents, oldest first.
        """
        reference = now or datetime.now(UTC)
        async with self._sessions() as session:
            rows = (await session.execute(select(Agent))).scalars().all()
        stale: list[Agent] = []
        for agent in rows:
            if agent.pinned:
                continue
            seen = agent.last_seen or agent.created_at
            if seen is None:
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=UTC)
            ephemeral = agent.kind == AgentKind.PAGE.value and not is_named_page_agent(agent)
            limit = page_after if ephemeral else device_after
            if reference - seen > limit:
                stale.append(agent)
        stale.sort(key=lambda a: a.last_seen or a.created_at or reference)
        return stale

    async def set_agent_owner(self, device_id: str, owner: str | None) -> bool:
        """Set (or clear, with None/empty) the builder this agent belongs to.

        Returns:
            True when the agent exists.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None:
                return False
            agent.owner = (owner or "").strip()[:64] or None
            await session.commit()
            return True

    async def claim_agent(self, device_id: str, subject: str, label: str) -> bool:
        """Record that a verified operator owns this agent.

        Args:
            device_id: The agent being claimed.
            subject: Verified Access subject of the claimant.
            label: Display name to show as the owner (usually their email).

        Returns:
            True when the agent exists.
        """
        if not subject:
            return False
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None:
                return False
            agent.owner_subject = subject
            agent.owner = (label or "").strip()[:64] or None
            await session.commit()
            logger.info(f"Agent {device_id!r} claimed by {label!r}")
            return True

    async def release_agent(self, device_id: str) -> bool:
        """Drop both the verified claim and the owner label.

        Returns:
            True when the agent exists.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None:
                return False
            agent.owner_subject = None
            agent.owner = None
            await session.commit()
            return True

    async def list_owners(self) -> list[str]:
        """Distinct owner names currently in use, alphabetically."""
        async with self._sessions() as session:
            rows = (await session.execute(select(Agent.owner).distinct())).scalars().all()
        return sorted({r for r in rows if r})

    async def set_agent_pinned(self, device_id: str, pinned: bool) -> bool:
        """Mark an agent as long-term (exempt from staleness) or not.

        Returns:
            True when the agent exists.
        """
        async with self._sessions() as session:
            result = await session.execute(select(Agent).where(Agent.device_id == device_id))
            agent = result.scalar_one_or_none()
            if agent is None:
                return False
            agent.pinned = pinned
            await session.commit()
            return True

    async def llm_spend_by_device(self, since: datetime | None = None) -> dict[str, dict[str, Any]]:
        """Per-agent spend: ``{device_id: {"cost_usd", "calls"}}``.

        Calls with no bound agent are keyed under ``""``.
        """
        async with self._sessions() as session:
            query = select(
                LLMSpend.device_id,
                func.coalesce(func.sum(LLMSpend.cost_usd), 0.0),
                func.count(LLMSpend.id),
            ).group_by(LLMSpend.device_id)
            if since is not None:
                query = query.where(LLMSpend.created_at >= since)
            rows = (await session.execute(query)).all()
        return {
            (device_id or ""): {"cost_usd": float(cost), "calls": int(calls)}
            for device_id, cost, calls in rows
        }

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

    async def record_llm_spend(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        cost_estimated: bool,
        device_id: str | None = None,
    ) -> int:
        """Append one billed call to the spend ledger and return its row id."""
        async with self._sessions() as session:
            row = LLMSpend(
                device_id=device_id,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                cost_estimated=cost_estimated,
            )
            session.add(row)
            await session.commit()
            return int(row.id)

    async def settle_llm_spend(
        self, row_id: int, cost_usd: float, prompt_tokens: int, completion_tokens: int
    ) -> bool:
        """Replace an estimated ledger row with the provider's billed figures.

        Returns:
            False when the row no longer exists (a spend reset in between).
        """
        async with self._sessions() as session:
            row = await session.get(LLMSpend, row_id)
            if row is None:
                return False
            row.cost_usd = cost_usd
            row.prompt_tokens = prompt_tokens
            row.completion_tokens = completion_tokens
            row.cost_estimated = False
            await session.commit()
            return True

    async def llm_spend_summary(self, since: datetime | None = None) -> dict[str, float | int]:
        """Aggregate the spend ledger, optionally only rows at or after `since`.

        Returns cost_usd, prompt_tokens, completion_tokens, calls, and
        estimated_calls — the last so callers can tell how much of the total
        came from the local price table rather than the provider.
        """
        async with self._sessions() as session:
            query = select(
                func.coalesce(func.sum(LLMSpend.cost_usd), 0.0),
                func.coalesce(func.sum(LLMSpend.prompt_tokens), 0),
                func.coalesce(func.sum(LLMSpend.completion_tokens), 0),
                func.count(LLMSpend.id),
                func.coalesce(func.sum(LLMSpend.cost_estimated), 0),
            )
            if since is not None:
                query = query.where(LLMSpend.created_at >= since)
            cost, prompt, completion, calls, estimated = (await session.execute(query)).one()
            return {
                "cost_usd": float(cost),
                "prompt_tokens": int(prompt),
                "completion_tokens": int(completion),
                "calls": int(calls),
                "estimated_calls": int(estimated),
            }

    async def llm_spend_by_model(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Per-model spend breakdown, most expensive first."""
        async with self._sessions() as session:
            query = select(
                LLMSpend.model,
                func.coalesce(func.sum(LLMSpend.cost_usd), 0.0),
                func.count(LLMSpend.id),
            ).group_by(LLMSpend.model)
            if since is not None:
                query = query.where(LLMSpend.created_at >= since)
            rows = (await session.execute(query)).all()
            out = [
                {"model": model, "cost_usd": float(cost), "calls": int(calls)}
                for model, cost, calls in rows
            ]
            out.sort(key=lambda row: float(row["cost_usd"]), reverse=True)
            return out

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

    async def get_persona_by_id(self, persona_id: int) -> Persona | None:
        """A persona by primary key, or None if it was deleted."""
        async with self._sessions() as session:
            persona: Persona | None = await session.get(Persona, persona_id)
            return persona

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
        tts_model: str | None = None,
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
                tts_model=tts_model,
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
            switched = agent.persona_id != persona.id
            agent.persona_id = persona.id
            agent.status = AgentStatus.CLAIMED.value
            await session.commit()
            logger.info(f"Assigned persona '{persona_name}' to agent '{device_id}'")
        # A different persona starts a new conversation. Reassigning the same
        # one (every page agent registration does) must not split it.
        if switched:
            await self.end_open_conversations(device_id, ConversationKind.CHAT)
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
        tts_model: str | None = None,
        asr_provider: str | None = None,
        server_skills: str | None = None,
        mcp_tools_allowlist: str | None = None,
        linked_agents: str | None = None,
        memory_window: int | None = None,
        transcription: bool | None = None,
        conversation_idle_minutes: int | None = None,
        auto_title: bool | None = None,
        summarize_conversations: bool | None = None,
        remember_conversations: int | None = None,
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
            if tts_model is not None:
                persona.tts_model = tts_model or None
            if asr_provider is not None:
                persona.asr_provider = asr_provider
            if server_skills is not None:
                persona.server_skills = server_skills or None
            if mcp_tools_allowlist is not None:
                persona.mcp_tools_allowlist = mcp_tools_allowlist or None
            if linked_agents is not None:
                persona.linked_agents = linked_agents or None
            if memory_window is not None:
                persona.memory_window = memory_window
            if transcription is not None:
                persona.transcription = transcription
            if conversation_idle_minutes is not None:
                persona.conversation_idle_minutes = conversation_idle_minutes
            if auto_title is not None:
                persona.auto_title = auto_title
            if summarize_conversations is not None:
                persona.summarize_conversations = summarize_conversations
            if remember_conversations is not None:
                persona.remember_conversations = remember_conversations
            await session.commit()
            return True

    async def set_agent_conversation_settings(self, device_id: str, values: dict[str, Any]) -> bool:
        """Set an agent's conversation overrides; None means use the persona's.

        Only the conversation setting names are accepted. Returns False when the
        agent does not exist.
        """
        allowed = {
            "conversation_idle_minutes",
            "memory_window",
            "auto_title",
            "summarize_conversations",
            "remember_conversations",
        }
        async with self._sessions() as session:
            agent = await session.scalar(select(Agent).where(Agent.device_id == device_id))
            if agent is None:
                return False
            for name, value in values.items():
                if name in allowed:
                    setattr(agent, name, value)
            await session.commit()
            return True

    async def load_history(
        self, device_id: str, limit: int = 40, *, conversation_id: int | None = None
    ) -> list[dict[str, str]]:
        """Return the most recent messages for device_id, oldest first.

        Args:
            device_id: The device to load history for.
            limit: Maximum number of messages (not turns) to return.
            conversation_id: Only this conversation's messages.

        Returns:
            List of {role, content, created_at} dicts; pass through
            ``server.history.history_for_llm`` before sending to a model.
        """
        async with self._sessions() as session:
            query = select(ConversationTurn).where(ConversationTurn.device_id == device_id)
            if conversation_id is not None:
                query = query.where(ConversationTurn.conversation_id == conversation_id)
            result = await session.execute(query.order_by(ConversationTurn.id.desc()).limit(limit))
            rows = list(result.scalars().all())
        rows.reverse()
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    async def export_history(
        self, device_id: str, session_id: str | None = None
    ) -> list[dict[str, str]]:
        """Every persisted message for a device, oldest first, uncapped.

        Same shape as :meth:`load_history` (``role``/``content``/``created_at``);
        used for the transcript download, where a limit would silently truncate.
        With ``session_id`` set, only that transcription session's turns.
        """
        async with self._sessions() as session:
            query = select(ConversationTurn).where(ConversationTurn.device_id == device_id)
            if session_id is not None:
                query = query.where(ConversationTurn.session_id == session_id)
            result = await session.execute(query.order_by(ConversationTurn.id.asc()))
            rows = list(result.scalars().all())
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    async def latest_session_id(self, device_id: str) -> str | None:
        """The session_id of the most recent transcription turn for a device."""
        async with self._sessions() as session:
            return await session.scalar(
                select(ConversationTurn.session_id)
                .where(ConversationTurn.device_id == device_id)
                .where(ConversationTurn.session_id.is_not(None))
                .order_by(ConversationTurn.id.desc())
                .limit(1)
            )

    async def load_session(
        self, device_id: str, session_id: str | None = None
    ) -> list[dict[str, str]]:
        """The complete turns of one transcription session, oldest first.

        ``session_id`` defaults to the device's most recent session. Unlike
        :meth:`load_history` this is never capped — a transcription session is
        the unit of "complete memory" and must not be silently truncated.
        Returns ``[]`` when the device has no transcription session yet.
        """
        if session_id is None:
            session_id = await self.latest_session_id(device_id)
        if session_id is None:
            return []
        return await self.export_history(device_id, session_id=session_id)

    async def list_sessions(self, device_id: str) -> list[dict[str, Any]]:
        """One row per transcription session, newest first.

        ``{session_id, turns, started_at, ended_at}`` — for a future session
        browser. Only the current session matters operationally for now.
        """
        async with self._sessions() as session:
            result = await session.execute(
                select(
                    ConversationTurn.session_id,
                    func.count(ConversationTurn.id),
                    func.min(ConversationTurn.created_at),
                    func.max(ConversationTurn.created_at),
                    func.max(ConversationTurn.id),
                )
                .where(ConversationTurn.device_id == device_id)
                .where(ConversationTurn.session_id.is_not(None))
                .group_by(ConversationTurn.session_id)
                .order_by(func.max(ConversationTurn.id).desc())
            )
            return [
                {
                    "session_id": sid,
                    "turns": int(count),
                    "started_at": started.isoformat() if started else None,
                    "ended_at": ended.isoformat() if ended else None,
                }
                for sid, count, started, ended, _last in result.all()
            ]

    async def append_history(
        self,
        device_id: str,
        role: str,
        content: str,
        session_id: str | None = None,
        *,
        conversation_id: int | None = None,
    ) -> None:
        """Append one message to the persisted history, in a conversation if given.

        Appending to a conversation also moves its ``last_turn_at`` (which the
        idle gap is measured from) and counts user and transcript turns.
        """
        async with self._sessions() as session:
            session.add(
                ConversationTurn(
                    device_id=device_id,
                    role=role,
                    content=content,
                    session_id=session_id,
                    conversation_id=conversation_id,
                )
            )
            if conversation_id is not None:
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    conversation.last_turn_at = _utcnow()
                    if role in _COUNTED_ROLES:
                        conversation.turn_count = (conversation.turn_count or 0) + 1
            await session.commit()

    # ── Conversations ────────────────────────────────────────────────────────

    async def open_conversation(
        self,
        device_id: str,
        *,
        persona: Persona | None,
        idle_minutes: int,
        kind: ConversationKind = ConversationKind.CHAT,
        create: bool = True,
        now: datetime | None = None,
    ) -> Conversation | None:
        """The device's current conversation of ``kind``, applying the boundaries.

        An open conversation continues unless the silence since its last turn is
        longer than ``idle_minutes`` or it ran under a different persona; then
        it is ended. With ``create`` a new one is started when there is none to
        continue; without it (a device connecting, not yet speaking) None is
        returned instead, so connections don't leave empty conversations.
        """
        reference = _naive_utc(now) if now else _utcnow()
        async with self._sessions() as session:
            current = await session.scalar(
                select(Conversation)
                .where(Conversation.device_id == device_id)
                .where(Conversation.kind == kind.value)
                .where(Conversation.ended_at.is_(None))
                .order_by(Conversation.id.desc())
                .limit(1)
            )
            if current is not None:
                last = current.last_turn_at or current.started_at
                idle = last is not None and reference - _naive_utc(last) > timedelta(
                    minutes=idle_minutes
                )
                switched = persona is not None and current.persona_id not in (None, persona.id)
                if idle or switched:
                    current.ended_at = _naive_utc(last) if last else reference
                    await session.commit()
                    current = None
            if current is None and create:
                current = Conversation(
                    public_id=secrets.token_hex(8),
                    device_id=device_id,
                    kind=kind.value,
                    persona_id=persona.id if persona else None,
                    persona_name=persona.name if persona else None,
                    started_at=reference,
                    turn_count=0,
                )
                session.add(current)
                await session.commit()
                await session.refresh(current)
            return current

    async def transcript_conversation(
        self, device_id: str, session_id: str, persona: Persona | None
    ) -> Conversation:
        """The transcript conversation for one listen session, created on first use."""
        async with self._sessions() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.public_id == session_id)
            )
            if conversation is None:
                conversation = Conversation(
                    public_id=session_id,
                    device_id=device_id,
                    kind=ConversationKind.TRANSCRIPT.value,
                    persona_id=persona.id if persona else None,
                    persona_name=persona.name if persona else None,
                    started_at=_utcnow(),
                    turn_count=0,
                )
                session.add(conversation)
                await session.commit()
                await session.refresh(conversation)
            return conversation

    async def end_open_conversations(
        self, device_id: str, kind: ConversationKind | None = None
    ) -> int:
        """End a device's open conversations now (New conversation, persona switch).

        Returns:
            How many were ended.
        """
        async with self._sessions() as session:
            query = (
                select(Conversation)
                .where(Conversation.device_id == device_id)
                .where(Conversation.ended_at.is_(None))
            )
            if kind is not None:
                query = query.where(Conversation.kind == kind.value)
            open_ones = list((await session.execute(query)).scalars().all())
            now = _utcnow()
            for conversation in open_ones:
                conversation.ended_at = conversation.last_turn_at or now
            await session.commit()
            return len(open_ones)

    async def end_transcript_conversation(self, session_id: str) -> None:
        """End the transcript conversation for a listen session (on stop)."""
        async with self._sessions() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.public_id == session_id)
            )
            if conversation is not None and conversation.ended_at is None:
                conversation.ended_at = conversation.last_turn_at or _utcnow()
                await session.commit()

    async def open_chat_conversations(self) -> list[Conversation]:
        """Every chat conversation not yet ended, for the idle sweep."""
        async with self._sessions() as session:
            result = await session.execute(
                select(Conversation)
                .where(Conversation.ended_at.is_(None))
                .where(Conversation.kind == ConversationKind.CHAT.value)
            )
            return list(result.scalars().all())

    async def end_conversation(self, conversation_id: int) -> None:
        """End one conversation at its last turn (or now, if it never had one)."""
        async with self._sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None and conversation.ended_at is None:
                conversation.ended_at = conversation.last_turn_at or _utcnow()
                await session.commit()

    async def conversations_to_wrap_up(
        self, *, max_attempts: int, limit: int = 20
    ) -> list[Conversation]:
        """Ended conversations with messages whose wrap-up isn't done, oldest first."""
        async with self._sessions() as session:
            result = await session.execute(
                select(Conversation)
                .where(Conversation.ended_at.is_not(None))
                .where(Conversation.last_turn_at.is_not(None))
                .where(Conversation.wrapped_up_at.is_(None))
                .where(Conversation.wrap_up_attempts < max_attempts)
                .order_by(Conversation.id.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def save_wrap_up(
        self,
        conversation_id: int,
        *,
        title: str | None,
        title_source: str | None,
        summary: str | None,
        done: bool,
        max_attempts: int,
    ) -> None:
        """Record a wrap-up result. A manual title is never replaced.

        ``done=False`` counts a failed attempt; once ``max_attempts`` is reached
        the conversation is marked wrapped up anyway so the sweep stops trying.
        A title (fallback or model) is kept either way.
        """
        async with self._sessions() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None:
                return
            if title and conversation.title_source != "manual":
                conversation.title = title[:160]
                conversation.title_source = title_source
            if summary is not None:
                conversation.summary = summary
            if done:
                conversation.wrapped_up_at = _utcnow()
            else:
                conversation.wrap_up_attempts = (conversation.wrap_up_attempts or 0) + 1
                if conversation.wrap_up_attempts >= max_attempts:
                    conversation.wrapped_up_at = _utcnow()
            await session.commit()

    async def remembered_conversations(
        self, device_id: str, *, exclude_id: int | None, limit: int
    ) -> list[Conversation]:
        """This agent's most recent ended conversations that have a summary."""
        if limit <= 0:
            return []
        async with self._sessions() as session:
            query = (
                select(Conversation)
                .where(Conversation.device_id == device_id)
                .where(Conversation.ended_at.is_not(None))
                .where(Conversation.summary.is_not(None))
            )
            if exclude_id is not None:
                query = query.where(Conversation.id != exclude_id)
            result = await session.execute(
                query.order_by(Conversation.ended_at.desc(), Conversation.id.desc()).limit(limit)
            )
            return list(result.scalars().all())

    async def list_conversations(
        self, device_id: str, *, before_id: int | None = None, limit: int = 20
    ) -> list[Conversation]:
        """A device's conversations with at least one message, newest first, paged."""
        async with self._sessions() as session:
            query = (
                select(Conversation)
                .where(Conversation.device_id == device_id)
                .where(Conversation.last_turn_at.is_not(None))
            )
            if before_id is not None:
                query = query.where(Conversation.id < before_id)
            result = await session.execute(query.order_by(Conversation.id.desc()).limit(limit))
            return list(result.scalars().all())

    async def get_conversation(self, public_id: str) -> Conversation | None:
        """One conversation by its public id."""
        async with self._sessions() as session:
            conversation: Conversation | None = await session.scalar(
                select(Conversation).where(Conversation.public_id == public_id)
            )
            return conversation

    async def conversation_messages(self, conversation_id: int) -> list[dict[str, str]]:
        """Every message of one conversation, oldest first, uncapped."""
        async with self._sessions() as session:
            result = await session.execute(
                select(ConversationTurn)
                .where(ConversationTurn.conversation_id == conversation_id)
                .order_by(ConversationTurn.id.asc())
            )
            rows = list(result.scalars().all())
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at.isoformat()}
            for r in rows
        ]

    async def rename_conversation(self, public_id: str, title: str) -> bool:
        """Set a person's title, which wrap-up never replaces."""
        async with self._sessions() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.public_id == public_id)
            )
            if conversation is None:
                return False
            conversation.title = title.strip()[:160] or None
            conversation.title_source = "manual" if conversation.title else None
            await session.commit()
            return True

    async def delete_conversation(self, public_id: str) -> bool:
        """Delete a conversation, its messages, and its summary."""
        async with self._sessions() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.public_id == public_id)
            )
            if conversation is None:
                return False
            await session.execute(
                delete(ConversationTurn).where(ConversationTurn.conversation_id == conversation.id)
            )
            await session.delete(conversation)
            await session.commit()
            return True

    async def clear_history(self, device_id: str) -> None:
        """Delete all conversations and history (and so summaries) for a device."""

        async with self._sessions() as session:
            await session.execute(
                delete(ConversationTurn).where(ConversationTurn.device_id == device_id)
            )
            await session.execute(delete(Conversation).where(Conversation.device_id == device_id))
            await session.commit()
        logger.info(f"Cleared conversation history for {device_id!r}")

    async def conversation_turn_count(self) -> int:
        """Return the total number of persisted conversation messages."""
        async with self._sessions() as session:
            return int(await session.scalar(select(func.count(ConversationTurn.id))) or 0)

    async def clear_all_history(self) -> int:
        """Delete every device's conversation history.

        Used to wipe transcripts between public sessions. The registry itself —
        agents, personas, tokens — is untouched, so devices stay enrolled.

        Returns:
            Number of messages removed.
        """

        async with self._sessions() as session:
            removed = int(await session.scalar(select(func.count(ConversationTurn.id))) or 0)
            await session.execute(delete(ConversationTurn))
            await session.execute(delete(Conversation))
            await session.commit()
        logger.info(f"Cleared all conversation history ({removed} messages)")
        return removed

    async def clear_llm_spend(self) -> int:
        """Delete the entire LLM spend ledger.

        The cumulative spend cap is enforced against this table, so wiping it
        resets total-spend protection to zero. Callers must opt in explicitly.

        Returns:
            Number of ledger rows removed.
        """

        async with self._sessions() as session:
            removed = int(await session.scalar(select(func.count(LLMSpend.id))) or 0)
            await session.execute(delete(LLMSpend))
            await session.commit()
        logger.info(f"Cleared LLM spend ledger ({removed} rows)")
        return removed

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


def _fallback_title(turns: list[ConversationTurn], max_words: int = 8) -> str | None:
    """The first words of the conversation, for untitled conversations.

    Prefers what was said to the agent. Some older history holds only replies
    (a bug once dropped the user's words from a full context window), so it
    falls back to the first reply rather than leaving the title empty.
    """
    for roles in ({"user", "transcript"}, {"assistant"}):
        for turn in turns:
            text = re.sub(r"\[(?:image|volatile-tools):[^\]]*\]", "", turn.content).strip()
            if turn.role in roles and text:
                words = text.split()
                title = " ".join(words[:max_words])
                return (title + "…" if len(words) > max_words else title)[:160]
    return None
