"""Add device: type the code a robot or script shows you, and manage paired keys.

The page lists what is waiting to pair but never shows codes. The code is the
proof that the person approving can see the thing being paired, so it has to
come from that thing's screen or console, not from this page.
"""

from __future__ import annotations

import html
import time
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from loguru import logger

from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.registry.models import AgentKind, OperatorRole
from agent_hub.registry.pairing import PairingRegistry, attempt_details
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server.agent_keys import InvalidAgentKey, parse_public_key

_TAG = "pairing"

# Wrong codes allowed per person (or per address without sign-in) per window.
# Six digits with five guesses per ten minutes keeps guessing hopeless.
_MAX_WRONG_CODES = 5
_WRONG_CODE_WINDOW_S = 600

PAIRING_CSS = """
.pair-form{display:flex;gap:.5rem;align-items:center;margin:1rem 0}
.pair-form input{font-size:1.6rem;letter-spacing:.3em;width:9ch;text-align:center}
.pair-msg{padding:.5rem .75rem;border-radius:6px;margin:.5rem 0}
.pair-ok{background:#12351f;color:#7ee2a8}.pair-err{background:#3a1d1d;color:#f3a6a6}
.pair-table{border-collapse:collapse;width:100%;font-size:.85rem}
.pair-table td,.pair-table th{padding:.35rem .5rem;border-bottom:1px solid #30363d;text-align:left}
.pair-table code{font-size:.75rem}
"""


class _WrongCodes:
    """Counts recent wrong codes per person so guessing is pointless."""

    def __init__(self) -> None:
        self._misses: dict[str, list[float]] = {}

    def blocked(self, who: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._misses.get(who, []) if now - t < _WRONG_CODE_WINDOW_S]
        self._misses[who] = recent
        return len(recent) >= _MAX_WRONG_CODES

    def miss(self, who: str) -> None:
        self._misses.setdefault(who, []).append(time.monotonic())


def make_router(
    store: RegistryStore,
    auth: DashboardAuthorization,
    render_page: Callable[[Request, str], str],
) -> APIRouter:
    """Build the Add device page and key revocation.

    Args:
        store: Registry store; pairing shares its database.
        auth: Dashboard authorization, for the operator-role check.
        render_page: The dashboard's page shell.

    Returns:
        Router serving /dashboard/add-device and key revocation.
    """
    router = APIRouter()
    pairing = PairingRegistry(store.session_factory)
    wrong_codes = _WrongCodes()

    def _who(request: Request) -> str:
        identity = getattr(request.state, "operator_identity", None)
        if identity is not None:
            return str(identity.email)
        return f"ip:{request.client.host if request.client else 'unknown'}"

    async def _page(request: Request, message: str = "", ok: bool = True) -> HTMLResponse:
        waiting = await pairing.list_waiting()
        keys = await pairing.list_active_keys()
        now = datetime.now(UTC).replace(tzinfo=None)
        notice = (
            f'<p class="pair-msg {"pair-ok" if ok else "pair-err"}">{html.escape(message)}</p>'
            if message
            else ""
        )
        waiting_rows = (
            "".join(
                f"<tr><td>{html.escape(a.agent_id)}</td>"
                f"<td>{html.escape(attempt_details(a).get('label', '') or '—')}</td>"
                f"<td>{html.escape(a.kind)}</td><td>{html.escape(a.ip_address or '—')}</td>"
                f"<td>{int((now - a.created_at).total_seconds() // 60)} min ago</td></tr>"
                for a in waiting
            )
            or '<tr><td colspan="5">Nothing is waiting.</td></tr>'
        )
        key_rows = (
            "".join(
                f"<tr><td>{html.escape(k.agent_id)}</td><td><code>{html.escape(k.fingerprint)}"
                f"</code></td><td>{html.escape(k.created_by or '—')}</td>"
                f"<td>{k.created_at:%Y-%m-%d %H:%M}</td><td>"
                f'<form method="post" action="/dashboard/agent-keys/'
                f'{html.escape(k.agent_id)}/revoke" '
                f"onsubmit=\"return confirm('Revoke this key? The agent is signed out "
                f"and must pair again.')\"><button>Revoke</button></form></td></tr>"
                for k in keys
            )
            or '<tr><td colspan="5">No agent has paired a key yet.</td></tr>'
        )
        body = f"""\
<style>{PAIRING_CSS}</style>
<h2>Add device</h2>
<p class="doc-muted">Type the 6-digit code your robot or script shows you. Codes last
10 minutes. For robots and scripts, run <code>scripts/agent_pair.py</code> to get one.</p>
{notice}
<form class="pair-form" method="post" action="/dashboard/add-device">
  <input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]{{6,7}}"
    maxlength="7" placeholder="000000" autofocus required aria-label="Pairing code">
  <button>Pair</button>
</form>
<h3>Waiting to pair</h3>
<table class="pair-table"><tr><th>Agent</th><th>Label</th><th>Kind</th><th>From</th>
<th>Asked</th></tr>{waiting_rows}</table>
<h3>Paired keys</h3>
<table class="pair-table"><tr><th>Agent</th><th>Fingerprint</th><th>Paired by</th>
<th>When (UTC)</th><th></th></tr>{key_rows}</table>"""
        return HTMLResponse(render_page(request, body), status_code=200 if ok else 400)

    @router.get(
        "/dashboard/add-device",
        response_class=HTMLResponse,
        dependencies=[Depends(auth.require_operator)],
    )
    async def add_device_page(request: Request) -> HTMLResponse:
        """The code box, what is waiting, and the keys already paired."""
        return await _page(request)

    @router.post("/dashboard/add-device", response_class=HTMLResponse)
    async def add_device(request: Request, code: str = Form(default="")) -> HTMLResponse:
        """Approve the attempt whose code was typed."""
        who = _who(request)
        if wrong_codes.blocked(who):
            return await _page(request, "Too many wrong codes. Wait a few minutes.", ok=False)
        attempt = await pairing.find_waiting_by_code(code.replace(" ", ""))
        if attempt is None:
            wrong_codes.miss(who)
            logger.bind(tag=_TAG).warning(f"Wrong pairing code from {who}")
            return await _page(
                request, "That code doesn't match anything waiting. Check it and try again.", False
            )

        details = attempt_details(attempt)
        try:
            fingerprint = (
                parse_public_key(attempt.public_key).fingerprint if attempt.public_key else None
            )
        except InvalidAgentKey:
            return await _page(request, "That request carried a key the hub can't use.", False)
        identity = getattr(request.state, "operator_identity", None)
        await store.get_or_create_agent(
            device_id=attempt.agent_id,
            kind=AgentKind(attempt.kind),
            label=details.get("label") or None,
            ip_address=attempt.ip_address,
            firmware_version=details.get("version") or None,
            owner=details.get("owner") or None,
        )
        if identity is not None:
            await store.claim_agent(attempt.agent_id, identity.subject, identity.email)
        persona = details.get("persona")
        if persona and await store.get_persona_by_name(persona):
            await store.assign_persona(attempt.agent_id, persona)
        await pairing.approve(attempt, who if identity else "dashboard", fingerprint)
        logger.bind(tag=_TAG).info(f"{who} paired {attempt.agent_id!r} ({fingerprint})")
        name = details.get("label") or attempt.agent_id
        return await _page(request, f"Paired {name}. It can sign in now.")

    @router.post("/dashboard/agent-keys/{agent_id}/revoke")
    async def revoke_key(agent_id: str, request: Request) -> Response:
        """Revoke an agent's key and sign it out. Yours, unowned, or any as an admin."""
        agent = await store.get_agent(agent_id)
        identity = getattr(request.state, "operator_identity", None)
        role = str(getattr(request.state, "operator_role", OperatorRole.ADMIN.value))
        owner_subject = agent.owner_subject if agent else None
        mine = identity is not None and owner_subject == identity.subject
        if agent and agent.owner_subject and not mine and role != OperatorRole.ADMIN.value:
            return await _page(request, "That agent belongs to someone else.", ok=False)
        revoked = await pairing.revoke(agent_id)
        mcp_bridge.unregister_page_agent(agent_id)
        logger.bind(tag=_TAG).info(f"{_who(request)} revoked {revoked} key(s) for {agent_id!r}")
        return RedirectResponse("/dashboard/add-device", status_code=303)

    return router
