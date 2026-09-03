"""Authentication and role enforcement shared by dashboard-facing routers."""

from __future__ import annotations

import base64
import binascii
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, WebSocket, status
from loguru import logger

from agent_hub.dashboard.access_identity import AccessIdentityError, AccessIdentityVerifier
from agent_hub.registry.models import OperatorRole
from agent_hub.registry.store import RegistryStore

_STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _normalise_hosts(raw: Any) -> set[str]:
    """Parse a host/origin allowlist into bare lowercase hosts."""
    if not raw:
        return set()
    items = [o.strip() for o in raw.split(",")] if isinstance(raw, str) else [str(o) for o in raw]
    hosts: set[str] = set()
    for item in items:
        value = item.strip().lower()
        if not value or "*" in value:
            continue
        hosts.add(urlsplit(value).netloc or value)
    return hosts


class DashboardAuthorization:
    """Authenticate dashboard requests and enforce local operator roles."""

    def __init__(self, store: RegistryStore, config: dict[str, Any]) -> None:
        """Build authorization policy from the raw Agent Hub configuration."""
        self._store = store
        server = config.get("server") or {}
        self._username = str(server.get("dashboard_username") or "admin")
        self._password = str(server.get("dashboard_password") or "")
        team_domain = str(server.get("dashboard_access_team_domain") or "")
        audience = str(server.get("dashboard_access_audience") or "")
        self._default_role = self._resolve_default_role(
            str(server.get("dashboard_default_role") or OperatorRole.VIEWER.value)
        )
        self._admin_emails = {
            email.strip().lower()
            for email in str(server.get("dashboard_admin_emails") or "").split(",")
            if email.strip()
        }
        if bool(team_domain) != bool(audience):
            raise ValueError(
                "server.dashboard_access_team_domain and dashboard_access_audience "
                "must be configured together"
            )
        if team_domain and not self._admin_emails:
            raise ValueError(
                "server.dashboard_admin_emails must name at least one bootstrap admin "
                "when Cloudflare Access identity is enabled"
            )
        self._access_verifier = (
            AccessIdentityVerifier(team_domain, audience) if team_domain and audience else None
        )
        self._origin_hosts = _normalise_hosts(
            server.get("dashboard_allowed_origins")
        ) | _normalise_hosts(server.get("allowed_hosts"))

    @staticmethod
    def _resolve_default_role(raw: str) -> str:
        """Validate the configured role for first-seen identities.

        Admin is deliberately not allowed: an Access policy is a guest list,
        and a mistake in it should not hand out operator administration. Name
        bootstrap admins explicitly in ``dashboard_admin_emails`` instead.
        """
        value = raw.strip().lower() or OperatorRole.VIEWER.value
        allowed = {OperatorRole.VIEWER.value, OperatorRole.OPERATOR.value}
        if value in allowed:
            return value
        if value == OperatorRole.ADMIN.value:
            logger.warning(
                "server.dashboard_default_role=admin is not allowed; using 'viewer'. "
                "List bootstrap admins in server.dashboard_admin_emails instead."
            )
        else:
            logger.warning(
                f"Unknown server.dashboard_default_role {raw!r}; using 'viewer'. "
                f"Valid values: {sorted(allowed)}."
            )
        return OperatorRole.VIEWER.value

    async def authenticate(self, request: Request) -> None:
        """Attach a trusted identity and role or reject the request."""
        await self._authenticate_connection(request)

    async def authorize_websocket(self, websocket: WebSocket) -> None:
        """Authenticate a dashboard WebSocket and require write access."""
        await self._authenticate_connection(websocket)
        self._check_operator_role(websocket)

    async def _authenticate_connection(self, connection: Request | WebSocket) -> None:
        """Attach a trusted identity and role to an HTTP or WebSocket connection."""
        if self._access_verifier is not None:
            assertion = connection.headers.get("cf-access-jwt-assertion", "")
            try:
                identity = await self._access_verifier.verify(assertion)
            except AccessIdentityError:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Valid Cloudflare Access identity required",
                ) from None
            operator = await self._store.get_or_create_dashboard_operator(
                identity.subject,
                identity.email,
                self._admin_emails,
                self._default_role,
            )
            if not operator.enabled:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Dashboard operator is disabled",
                )
            connection.state.operator_identity = identity
            connection.state.operator_role = operator.role
            return

        connection.state.operator_identity = None
        connection.state.operator_role = OperatorRole.ADMIN.value
        if not self._password:
            return
        auth = connection.headers.get("authorization", "")
        scheme, _, encoded = auth.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            raise self._basic_auth_error()
        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise self._basic_auth_error() from None
        username, separator, password = decoded.partition(":")
        if not separator or not (
            secrets.compare_digest(username, self._username)
            and secrets.compare_digest(password, self._password)
        ):
            raise self._basic_auth_error()

    async def require_same_origin(self, request: Request) -> None:
        """Reject cross-origin state-changing browser requests."""
        if request.method not in _STATE_CHANGING_METHODS:
            return
        origin = request.headers.get("origin")
        if not origin:
            return
        parsed = urlsplit(origin)
        candidates = {parsed.netloc.lower()}
        if parsed.hostname:
            candidates.add(parsed.hostname.lower())
        candidates.discard("")
        allowed = self._origin_hosts
        if not allowed:
            host = request.headers.get("host")
            allowed = {host.lower()} if host else set()
        if candidates & allowed:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected",
        )

    async def require_write(self, request: Request) -> None:
        """Restrict mutations to operators and administrators."""
        if request.method not in _STATE_CHANGING_METHODS:
            return
        await self.require_operator(request)

    async def require_operator(self, request: Request) -> None:
        """Require an operator or administrator, including for a GET route."""
        self._check_operator_role(request)

    @staticmethod
    def _check_operator_role(connection: Request | WebSocket) -> None:
        role = getattr(connection.state, "operator_role", OperatorRole.ADMIN.value)
        if role not in {OperatorRole.ADMIN.value, OperatorRole.OPERATOR.value}:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Operator role required",
            )

    async def require_admin(self, request: Request) -> None:
        """Restrict operator administration to administrators."""
        if getattr(request.state, "operator_role", None) != OperatorRole.ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrator role required",
            )

    @staticmethod
    def _basic_auth_error() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
