"""Pairing attempts and agent keys: the records behind "type the code".

Shares the registry's SQLite database. Timestamps are naive UTC, the form
SQLite hands back, so stored and fresh values compare directly.
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_hub.registry.models import Agent, AgentKey, PairingAttempt

# How long a code stays usable. Long enough to walk to a laptop, short enough
# that a code read out loud is useless by tomorrow.
ATTEMPT_TTL = timedelta(minutes=10)
# Live attempts allowed per IP address, and per hub. They stop a stranger
# from flooding Add device on a hub reachable from the internet.
MAX_LIVE_PER_IP = 5
MAX_LIVE_TOTAL = 50


class PairingLimitError(Exception):
    """Raised when too many attempts are already waiting."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PairingRegistry:
    """Create, approve, and look up pairing attempts and agent keys."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        """Bind to the registry database.

        Args:
            sessions: The registry store's ``session_factory``.
        """
        self._sessions = sessions

    async def start_attempt(
        self,
        *,
        agent_id: str,
        kind: str,
        key_type: str | None,
        public_key: str | None,
        details: dict[str, Any],
        ip_address: str | None,
    ) -> PairingAttempt:
        """Open a pairing attempt with a fresh code.

        Args:
            agent_id: The id the agent will have once approved.
            kind: AgentKind value for the new agent row.
            key_type: Key type, when the agent pairs a key.
            public_key: OpenSSH public key, when the agent pairs a key.
            details: Label, persona, and owner label, applied on approval.
            ip_address: Where the request came from, for the per-IP limit.

        Returns:
            The new attempt, including its code.

        Raises:
            PairingLimitError: Too many attempts are waiting, overall or from this IP.
        """
        now = _now()
        async with self._sessions() as session:
            live = PairingAttempt.expires_at > now, PairingAttempt.approved_at.is_(None)
            total = await session.scalar(select(func.count(PairingAttempt.id)).where(*live))
            if int(total or 0) >= MAX_LIVE_TOTAL:
                raise PairingLimitError("too many devices are waiting to pair; try again soon")
            if ip_address:
                from_ip = await session.scalar(
                    select(func.count(PairingAttempt.id)).where(
                        *live, PairingAttempt.ip_address == ip_address
                    )
                )
                if int(from_ip or 0) >= MAX_LIVE_PER_IP:
                    raise PairingLimitError("too many pairing attempts from this address")
            live_codes = set(
                (await session.scalars(select(PairingAttempt.code).where(*live))).all()
            )
            code = f"{secrets.randbelow(1_000_000):06d}"
            while code in live_codes:
                code = f"{secrets.randbelow(1_000_000):06d}"
            attempt = PairingAttempt(
                attempt_id=secrets.token_urlsafe(24),
                agent_id=agent_id,
                kind=kind,
                code=code,
                key_type=key_type,
                public_key=public_key,
                details=json.dumps(details),
                ip_address=ip_address,
                created_at=now,
                expires_at=now + ATTEMPT_TTL,
            )
            session.add(attempt)
            await session.commit()
            return attempt

    async def get_attempt(self, attempt_id: str) -> PairingAttempt | None:
        """Return an attempt by its id, approved, waiting, or expired."""
        async with self._sessions() as session:
            result = await session.execute(
                select(PairingAttempt).where(PairingAttempt.attempt_id == attempt_id)
            )
            return result.scalar_one_or_none()

    async def find_waiting_by_code(self, code: str) -> PairingAttempt | None:
        """Return the unexpired, unapproved attempt with this code, if any."""
        async with self._sessions() as session:
            result = await session.execute(
                select(PairingAttempt).where(
                    PairingAttempt.code == code.strip(),
                    PairingAttempt.expires_at > _now(),
                    PairingAttempt.approved_at.is_(None),
                )
            )
            return result.scalars().first()

    async def list_waiting(self) -> list[PairingAttempt]:
        """Unexpired attempts nobody has approved yet, newest first."""
        async with self._sessions() as session:
            result = await session.execute(
                select(PairingAttempt)
                .where(PairingAttempt.expires_at > _now(), PairingAttempt.approved_at.is_(None))
                .order_by(PairingAttempt.created_at.desc())
            )
            return list(result.scalars())

    async def approve(
        self, attempt: PairingAttempt, approved_by: str, fingerprint: str | None
    ) -> AgentKey | None:
        """Mark an attempt approved and bind its key, revoking the agent's old keys.

        Args:
            attempt: A waiting attempt, from ``find_waiting_by_code``.
            approved_by: Who typed the code: an email, or "dashboard" without sign-in.
            fingerprint: The key's fingerprint, when the attempt carries a key.

        Returns:
            The new key, or None when the attempt carried no key.
        """
        now = _now()
        async with self._sessions() as session:
            await session.execute(
                update(PairingAttempt)
                .where(PairingAttempt.id == attempt.id)
                .values(approved_by=approved_by, approved_at=now)
            )
            key: AgentKey | None = None
            if attempt.public_key and attempt.key_type:
                await self._revoke(session, attempt.agent_id, now)
                key = AgentKey(
                    agent_id=attempt.agent_id,
                    key_type=attempt.key_type,
                    public_key=attempt.public_key,
                    fingerprint=fingerprint or "",
                    created_by=approved_by,
                    created_at=now,
                )
                session.add(key)
            await session.commit()
            return key

    async def active_key(self, agent_id: str) -> AgentKey | None:
        """The agent's current key, or None when it has none or it was revoked."""
        async with self._sessions() as session:
            result = await session.execute(
                select(AgentKey)
                .where(AgentKey.agent_id == agent_id, AgentKey.revoked_at.is_(None))
                .order_by(AgentKey.created_at.desc())
            )
            return result.scalars().first()

    async def list_active_keys(self) -> list[AgentKey]:
        """Every unrevoked key, newest first."""
        async with self._sessions() as session:
            result = await session.execute(
                select(AgentKey)
                .where(AgentKey.revoked_at.is_(None))
                .order_by(AgentKey.created_at.desc())
            )
            return list(result.scalars())

    async def revoke(self, agent_id: str) -> int:
        """Revoke the agent's keys and end its current session token.

        Returns:
            How many keys were revoked.
        """
        async with self._sessions() as session:
            count = await self._revoke(session, agent_id, _now())
            await session.execute(
                update(Agent).where(Agent.device_id == agent_id).values(websocket_token=None)
            )
            await session.commit()
            return count

    @staticmethod
    async def _revoke(session: AsyncSession, agent_id: str, now: datetime) -> int:
        result = await session.execute(
            update(AgentKey)
            .where(AgentKey.agent_id == agent_id, AgentKey.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(getattr(result, "rowcount", 0) or 0)


def attempt_details(attempt: PairingAttempt) -> dict[str, Any]:
    """The label, persona, and owner label an attempt asked for."""
    try:
        parsed = json.loads(attempt.details or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
