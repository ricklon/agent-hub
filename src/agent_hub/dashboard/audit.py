"""Persistent, privacy-minimal audit capture for dashboard HTTP actions."""

from __future__ import annotations

import html
from typing import Final

from fastapi import Request
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from starlette.types import ASGIApp

from agent_hub.dashboard._timefmt import fmt_ts
from agent_hub.registry.models import AuditEvent
from agent_hub.registry.store import RegistryStore

_STATE_CHANGING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TARGET_FIELDS: Final = (
    ("subject", "operator"),
    ("device_id", "agent"),
    ("name", "persona"),
)


class DashboardAuditMiddleware(BaseHTTPMiddleware):
    """Record authenticated dashboard mutations after their response is known."""

    def __init__(self, app: ASGIApp, store: RegistryStore) -> None:
        super().__init__(app)
        self._store = store

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Run the request, then append action metadata without its body."""
        try:
            response = await call_next(request)
        except Exception:
            await self._record(request, 500)
            raise
        await self._record(request, response.status_code)
        return response

    async def _record(self, request: Request, status_code: int) -> None:
        if request.method not in _STATE_CHANGING_METHODS:
            return
        role = getattr(request.state, "operator_role", None)
        if not isinstance(role, str):
            return
        identity = getattr(request.state, "operator_identity", None)
        subject = getattr(identity, "subject", None)
        email = getattr(identity, "email", "local")
        if not isinstance(subject, str):
            subject = None
        if not isinstance(email, str):
            email = "local"

        route = request.scope.get("route")
        route_name = getattr(route, "name", None)
        action = route_name if isinstance(route_name, str) else request.method.lower()
        target_type, target_id = self._target(request)
        try:
            await self._store.record_audit_event(
                operator_subject=subject,
                operator_email=email,
                operator_role=role,
                action=action,
                target_type=target_type,
                target_id=target_id,
                outcome="success" if status_code < 400 else "failure",
                status_code=status_code,
            )
        except Exception as exc:
            logger.warning(f"Could not persist dashboard audit event: {exc}")

    @staticmethod
    def _target(request: Request) -> tuple[str | None, str | None]:
        for field, target_type in _TARGET_FIELDS:
            value = request.path_params.get(field)
            if isinstance(value, str) and value:
                return target_type, value
        return None, None


def render_audit_table(events: list[AuditEvent]) -> str:
    """Render an escaped audit event table for the admin dashboard."""
    rows = "".join(_render_audit_row(event) for event in events)
    empty = '<tr><td colspan="7">No dashboard changes have been recorded.</td></tr>'
    return f"""\
<table>
<thead><tr>
  <th>time</th><th>operator</th><th>role</th><th>action</th>
  <th>target</th><th>result</th><th>status</th>
</tr></thead>
<tbody>{rows or empty}</tbody>
</table>"""


def _render_audit_row(event: AuditEvent) -> str:
    target = "—"
    if event.target_type and event.target_id:
        target = f"{event.target_type}: {event.target_id}"
    result_class = "audit-success" if event.outcome == "success" else "audit-failure"
    created_at = fmt_ts(event.created_at, fmt="%Y-%m-%d %H:%M:%S")
    return (
        "<tr>"
        f"<td>{html.escape(created_at)}</td>"
        f"<td>{html.escape(event.operator_email)}</td>"
        f"<td>{html.escape(event.operator_role)}</td>"
        f"<td>{html.escape(event.action)}</td>"
        f"<td>{html.escape(target)}</td>"
        f'<td class="{result_class}">{html.escape(event.outcome)}</td>'
        f"<td>{event.status_code}</td>"
        "</tr>"
    )
