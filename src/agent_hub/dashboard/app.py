"""Dashboard: agent list + OpenRouter model picker.

Server-rendered with HTMX — no SPA build step.
"""

from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from loguru import logger

from agent_hub import spend
from agent_hub.config import config_bool, resolve_timezone
from agent_hub.conversation_wrapup import wrap_up_conversation
from agent_hub.conversations import (
    effective_settings,
    memory_note_for_turn,
)
from agent_hub.conversations import settings_for_device as conversation_settings_for_device
from agent_hub.dashboard import cleanup, conversations_view, model_field, persona_options
from agent_hub.dashboard._timefmt import fmt_ts
from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.agent_cards import (
    AGENT_CARD_CSS,
    interaction_link,
    is_agent_connected,
    launch_link,
    render_agent_cards,
)
from agent_hub.dashboard.audit import render_audit_table
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.dashboard.conversation import make_conversation_router
from agent_hub.dashboard.conversation_ui import CONVERSATION_CSS, CONVERSATION_SCRIPT
from agent_hub.dashboard.overview import render_fleet_overview, render_health_strip
from agent_hub.dashboard.styles import DASHBOARD_CSS
from agent_hub.providers.llm import get_provider as get_llm
from agent_hub.providers.llm.model_check import ModelCheck, check_model
from agent_hub.registry.models import Agent, AgentKind, Conversation, OperatorRole, Persona
from agent_hub.registry.page_identity import is_named_page_agent
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state, tool_policy
from agent_hub.server.agent_turn import TurnError, call_one_tool, run_turn
from agent_hub.server.history import history_for_llm

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# Conversations per page on an agent's page.
_PAGE_SIZE = 20

_PAGE = """\
<!doctype html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-hub</title>
<link rel="icon" href="data:,">
<style>{css}</style>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<script>
// htmx drops 4xx responses by default, so every refusal the dashboard explains
// (free mode, claims, named page agents) looked like a button that did nothing.
// Show 4xx replies that carry HTML; JSON errors from the auth layer stay out.
document.addEventListener("htmx:beforeSwap", (event) => {{
  const xhr = event.detail.xhr;
  const type = xhr.getResponseHeader("Content-Type") || "";
  if (xhr.status >= 400 && xhr.status < 500 && type.startsWith("text/html") && xhr.responseText) {{
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }}
}});
</script>
</head><body hx-headers='{{"X-Requested-With":"XMLHttpRequest"}}'
  hx-indicator="#global-progress">
<div id="global-progress" class="htmx-indicator" role="status" aria-live="polite">
  Working…
</div>
<div id="global-feedback" role="alert" aria-live="assertive"></div>
<header><h1>Agent Hub</h1>{operator}</header>
<nav>
  <a href="/dashboard/">Agents</a>
  <a href="/dashboard/health">Health</a>
  <a href="/dashboard/personas">Personas</a>
  <a href="/dashboard/models">Models</a>{free_badge}
  {admin_nav}
  {operator_nav}
  <a href="/dashboard/docs">Docs</a>
</nav>
{body}
<div id="conversation-host"></div>
<script>
{conversation_script}
document.body.addEventListener("htmx:beforeRequest", function(event) {{
  const source = event.detail.elt;
  source.setAttribute("aria-busy", "true");
  const buttons = source.matches("button") ? [source] : source.querySelectorAll("button");
  buttons.forEach(function(button) {{
    if (button.disabled) return;
    button.dataset.requestDisabled = "true";
    button.disabled = true;
  }});
  document.getElementById("global-feedback").textContent = "";
}});
document.body.addEventListener("htmx:afterRequest", function(event) {{
  const source = event.detail.elt;
  source.setAttribute("aria-busy", "false");
  const buttons = source.matches("button") ? [source] : source.querySelectorAll("button");
  buttons.forEach(function(button) {{
    if (!button.dataset.requestDisabled) return;
    button.disabled = false;
    delete button.dataset.requestDisabled;
  }});
  if (!event.detail.successful) {{
    document.getElementById("global-feedback").textContent =
      "That action could not be completed. Check your connection and try again.";
  }}
}});
</script>
</body></html>
"""


def make_router(
    store: RegistryStore,
    config: dict[str, Any],
    authorization: DashboardAuthorization | None = None,
) -> APIRouter:
    server_config = config.get("server") or {}
    display_tz = resolve_timezone(
        str(server_config.get("timezone") or ""),
        int(server_config.get("timezone_offset") or -8),
    )
    auth = authorization or DashboardAuthorization(store, config)
    heartbeat_timeout_seconds = max(
        1,
        int(server_config.get("heartbeat_timeout_seconds") or 180),
    )
    image_root = Path(str(server_config.get("dashboard_image_root") or "data/images")).resolve()

    def _dashboard_image_path(raw_path: str) -> Path | None:
        requested = Path(raw_path)
        candidates = (
            [requested.resolve()]
            if requested.is_absolute()
            else [(Path.cwd() / requested).resolve(), (image_root / requested).resolve()]
        )
        for candidate in candidates:
            if candidate.is_relative_to(image_root):
                return candidate
        return None

    router = APIRouter(
        dependencies=[
            Depends(auth.authenticate),
            Depends(auth.require_same_origin),
            Depends(auth.require_write),
        ]
    )
    router.include_router(make_conversation_router(store, config))
    api_key: str = config.get("llm", {}).get("openai", {}).get("api_key", "")
    # The model a persona with no model of its own runs.
    default_model: str = str(config.get("llm", {}).get("openai", {}).get("model", "") or "")
    # Free mode: the model picker only lists free OpenRouter models and paid
    # ids are refused on select/save. For hubs running on a $0 budget.
    free_only: bool = config_bool(
        config.get("llm", {}).get("free_only"), False, key="llm.free_only"
    )

    stale_policy = cleanup.StalePolicy.from_config(config)

    def _free_for(request: Request) -> bool:
        """Free mode as it applies to this viewer: on unless they may choose paid."""
        return free_only and not _may_choose_paid(request)

    async def _reject_model(model_id: str, request: Request) -> str | None:
        """Reason a model id cannot be used on this hub, or None if it can.

        Two gates: free mode (paid ids refused) and capability (a model the
        catalogue says cannot call tools is useless here — every persona
        relies on function calling for time, weather, camera and device
        tools). Ids the catalogue does not know (a local Ollama model, say)
        pass; only a known-incapable model is refused.
        """
        if not model_id:
            return None
        if _free_for(request) and not await is_free_model(model_id, api_key):
            return (
                f"Free models only: {model_id!r} is not a free model. "
                "An admin can allow paid models for you on the Operators page."
            )
        if not await supports_tools(model_id, api_key):
            return f"{model_id!r} cannot call tools, which every persona on this hub needs."
        return None

    # ── Static image serving ──────────────────────────────────────────────────

    @router.get("/dashboard/image")
    async def serve_image(path: str) -> Response:
        """Serve a saved device capture JPEG by filesystem path."""
        p = _dashboard_image_path(path)
        if p is None or not p.exists() or p.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            return Response(status_code=404)
        media_type = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return Response(content=p.read_bytes(), media_type=media_type)

    # ── Agents ───────────────────────────────────────────────────────────────

    _full_css = DASHBOARD_CSS + AGENT_CARD_CSS + CONVERSATION_CSS

    def _render_page(request: Request, body: str) -> str:
        identity = getattr(request.state, "operator_identity", None)
        role = str(getattr(request.state, "operator_role", OperatorRole.ADMIN.value))
        operator = _render_operator(identity, role)
        admin_nav = (
            '<a href="/dashboard/operators">Operators</a><a href="/dashboard/audit">Audit</a>'
            if role == OperatorRole.ADMIN.value
            else ""
        )
        operator_nav = (
            '<a href="/dashboard/page-agent">Launch agent</a>'
            if role in {OperatorRole.ADMIN.value, OperatorRole.OPERATOR.value}
            else ""
        )
        free_badge = (
            ' <span class="badge badge-free" title="Free mode: your agents run free '
            'OpenRouter models. An admin can allow paid models for you.">free mode</span>'
            if _free_for(request)
            else ""
        )
        return _PAGE.format(
            css=_full_css,
            operator=operator,
            admin_nav=admin_nav,
            operator_nav=operator_nav,
            free_badge=free_badge,
            body=body,
            conversation_script=CONVERSATION_SCRIPT,
        )

    @router.get("/dashboard/", response_class=HTMLResponse)
    async def dashboard_index(
        request: Request, owner: str = "", mine: str = "", view: str = "cards"
    ) -> Response:
        if view == "diagnostics":
            # The diagnostics table moved to Health; keep old links working.
            return RedirectResponse(
                "/dashboard/health" + _filter_query(owner, bool(mine)), status_code=303
            )
        overview = await _render_agent_overview(
            store,
            heartbeat_timeout_seconds,
            has_identity=getattr(request.state, "operator_identity", None) is not None,
            viewer_subject=_viewer_subject(request),
            owner=owner,
            mine=bool(mine),
            launcher=_launcher(request),
        )
        role = str(getattr(request.state, "operator_role", OperatorRole.ADMIN.value))
        launch = (
            '<a class="action-link primary" href="/dashboard/page-agent" '
            'target="_blank" rel="noopener">+ Launch browser agent</a>'
            if role != OperatorRole.VIEWER.value
            else ""
        )
        intro = (
            '<section class="workspace-intro"><div><h2>Your agent workspace</h2>'
            "<p>One fleet for devices, browser agents, and connected services. "
            "Each agent runs with its own persona.</p></div>" + launch + "</section>"
        )
        return HTMLResponse(_render_page(request, intro + overview))

    @router.get("/dashboard/health", response_class=HTMLResponse)
    async def dashboard_health(request: Request, owner: str = "", mine: str = "") -> HTMLResponse:
        """Fleet health, the diagnostics table, spend and cleanup, off the Agents page."""
        overview = await _render_agent_overview(
            store,
            heartbeat_timeout_seconds,
            has_identity=getattr(request.state, "operator_identity", None) is not None,
            viewer_subject=_viewer_subject(request),
            owner=owner,
            mine=bool(mine),
            view="diagnostics",
            launcher=_launcher(request),
        )
        body = overview + await _spend_panel() + await _cleanup_panel()
        return HTMLResponse(_render_page(request, body))

    async def _cleanup_panel() -> str:
        """Stale agents past the configured thresholds, with a one-click sweep."""
        stale = await cleanup.find_stale(store, stale_policy)
        if not stale:
            return (
                '<section id="cleanup-panel" aria-labelledby="cleanup-heading">'
                '<h2 id="cleanup-heading">Cleanup</h2>'
                f'<p class="doc-muted">No stale agents ({stale_policy.describe()}).</p></section>'
            )
        rows = "".join(
            f"<li>{html.escape(a.label or a.device_id)} "
            f'<span class="badge badge-kind">{html.escape(a.kind)}</span> '
            f'<span class="doc-muted">last seen {fmt_ts(a.last_seen, display_tz)}</span></li>'
            for a in stale[:20]
        )
        more = f"<li>… and {len(stale) - 20} more</li>" if len(stale) > 20 else ""
        return f"""\
<section id="cleanup-panel" aria-labelledby="cleanup-heading">
  <div class="section-heading"><div>
    <h2 id="cleanup-heading">Cleanup <span class="attention-count">{len(stale)}</span></h2>
    <p class="doc-muted">Stale: {stale_policy.describe()}. Removing deletes the row and
    its conversation history; spend records are kept. Per-tab page agents past their
    threshold are also removed automatically once an hour; named page agents and
    devices only ever by a person.</p>
  </div>
  <form hx-post="/dashboard/agents/prune" hx-target="#cleanup-panel" hx-swap="outerHTML"
        hx-confirm="Remove {len(stale)} stale agent(s) and their history?">
    <button type="submit" style="background:#b62324">Remove {len(stale)} stale</button>
  </form></div>
  <ul style="margin:0.5rem 0 0 1.2rem">{rows}{more}</ul>
</section>"""

    @router.post("/dashboard/agents/prune", response_class=HTMLResponse)
    async def agents_prune(request: Request) -> HTMLResponse:
        removed = await cleanup.prune(store, stale_policy)
        logger.info(f"Dashboard pruned {len(removed)} stale agent(s)")
        panel = await _cleanup_panel()
        note = f'<p class="msg">✓ Removed {len(removed)} agent(s).</p>'
        return HTMLResponse(panel.replace("</section>", note + "</section>", 1))

    @router.post("/dashboard/agents/{device_id}/claim", response_class=HTMLResponse)
    async def agent_claim(device_id: str, request: Request) -> HTMLResponse:
        """Claim an agent as the signed-in operator.

        The claim records the *verified* Access subject, so unlike the typed
        owner label it cannot be spoofed by a robot registering with someone
        else's name.
        """
        identity = getattr(request.state, "operator_identity", None)
        if identity is None:
            return HTMLResponse(
                _claim_panel(None, None, None)
                + '<p class="msg" style="color:#d29922">This hub has no verified sign-in, '
                "so there is nobody to claim as. Set the owner label instead.</p>",
                status_code=400,
            )
        agent = await store.get_agent(device_id)
        if agent is None:
            return HTMLResponse("<p>Agent not found.</p>", status_code=404)
        if is_named_page_agent(agent):
            return _named_page_ownership_refusal(agent, identity, _role(request))
        await store.claim_agent(device_id, identity.subject, identity.email)
        agent = await store.get_agent(device_id)
        return HTMLResponse(_claim_panel(agent, identity, _role(request)))

    @router.post("/dashboard/agents/{device_id}/release", response_class=HTMLResponse)
    async def agent_release(device_id: str, request: Request) -> HTMLResponse:
        """Give up a claim. Yours always; anyone else's only as an admin."""
        agent = await store.get_agent(device_id)
        if agent is None:
            return HTMLResponse("<p>Agent not found.</p>", status_code=404)
        identity = getattr(request.state, "operator_identity", None)
        role = _role(request)
        if is_named_page_agent(agent):
            return _named_page_ownership_refusal(agent, identity, role)
        mine = identity is not None and agent.owner_subject == identity.subject
        if agent.owner_subject and not mine and role != OperatorRole.ADMIN.value:
            return HTMLResponse(
                _claim_panel(agent, identity, role)
                + '<p class="msg" style="color:#d29922">That claim belongs to someone '
                "else. An admin can release it.</p>",
                status_code=403,
            )
        await store.release_agent(device_id)
        agent = await store.get_agent(device_id)
        return HTMLResponse(_claim_panel(agent, identity, role))

    @router.post("/dashboard/agents/{device_id}/owner", response_class=HTMLResponse)
    async def agent_owner(device_id: str, owner: str = Form(default="")) -> HTMLResponse:
        """Set or clear the builder an agent belongs to."""
        agent = await store.get_agent(device_id)
        if agent is None:
            return HTMLResponse("<p>Agent not found.</p>", status_code=404)
        if is_named_page_agent(agent):
            return HTMLResponse(
                '<span class="msg" style="color:#d29922">A named page agent always belongs '
                "to the person who named it.</span>",
                status_code=409,
            )
        await store.set_agent_owner(device_id, owner)
        name = owner.strip()
        return HTMLResponse(
            f'<span class="msg">✓ {html.escape(name)}</span>'
            if name
            else '<span class="msg">✓ owner cleared</span>'
        )

    @router.post("/dashboard/agents/{device_id}/call_tool", response_class=HTMLResponse)
    async def agent_call_tool(
        device_id: str,
        tool: str = Form(...),
        arguments: str = Form(default="{}"),
    ) -> HTMLResponse:
        """Call one tool on a bridged agent and show the raw result."""
        import json as _json

        raw = arguments.strip() or "{}"
        try:
            args = _json.loads(raw)
        except ValueError as exc:
            return HTMLResponse(
                f'<div class="tool-result" style="color:#f85149">arguments must be JSON: '
                f"{html.escape(str(exc))}</div>",
                status_code=400,
            )
        if not isinstance(args, dict):
            return HTMLResponse(
                '<div class="tool-result" style="color:#f85149">arguments must be a JSON '
                'object, e.g. {"speed": 5}</div>',
                status_code=400,
            )
        try:
            result = await call_one_tool(device_id, tool, args)
        except TurnError as exc:
            return HTMLResponse(
                f'<div class="tool-result" style="color:#f85149">{html.escape(str(exc))}</div>',
                status_code=400,
            )
        if result.startswith("data:image"):
            return HTMLResponse(
                f'<img src="{html.escape(result)}" style="max-width:320px;border-radius:4px">'
            )
        return HTMLResponse(f'<div class="tool-result">{html.escape(result)}</div>')

    @router.post("/dashboard/agents/{device_id}/ask", response_class=HTMLResponse)
    async def agent_ask(device_id: str, text: str = Form(default="")) -> HTMLResponse:
        """Run one full persona turn against a bridged agent."""
        message = text.strip()
        if not message:
            return HTMLResponse('<div class="tool-result">Type something to ask.</div>', 400)
        try:
            result = await run_turn(store, config, device_id, message)
        except TurnError as exc:
            return HTMLResponse(
                f'<div class="tool-result" style="color:#f85149">{html.escape(str(exc))}</div>',
                status_code=400,
            )
        tools_line = (
            f'<div style="color:#8b949e;font-size:0.8rem">tools called: '
            f"{html.escape(', '.join(result.tools_called))}</div>"
            if result.tools_called
            else ""
        )
        images = "".join(
            f'<img src="{html.escape(i)}" style="max-width:320px;border-radius:4px;display:block">'
            for i in result.images
        )
        return HTMLResponse(
            f'<div class="tool-result">{html.escape(result.reply) or "(no reply)"}</div>'
            f"{tools_line}{images}"
        )

    @router.post("/dashboard/agents/{device_id}/pin", response_class=HTMLResponse)
    async def agent_pin(device_id: str, pinned: str = Form(default="")) -> HTMLResponse:
        """Mark an agent long-term (kept out of cleanup) or clear the mark."""
        keep = pinned.strip() in {"1", "true", "on", "yes"}
        if not await store.set_agent_pinned(device_id, keep):
            return HTMLResponse("<p>Agent not found.</p>", status_code=404)
        logger.info(f"Dashboard {'pinned' if keep else 'unpinned'} agent {device_id!r}")
        return HTMLResponse(_pin_form(device_id, keep))

    @router.post("/dashboard/agents/{device_id}/remove", response_class=HTMLResponse)
    async def agent_remove(device_id: str, keep_history: str = Form(default="")) -> Response:
        keep = bool(keep_history)
        if not await cleanup.remove_agent(store, device_id, keep_history=keep):
            return HTMLResponse("<p>Agent not found.</p>", status_code=404)
        logger.info(f"Dashboard removed agent {device_id!r}{' (history kept)' if keep else ''}")
        return Response(status_code=204, headers={"HX-Redirect": "/dashboard/"})

    async def _spend_panel() -> str:
        """Spend summary for the dashboard header, or nothing if unmetered."""
        tracker = spend.get_tracker()
        if tracker is None:
            return ""
        totals = await tracker.totals()
        return _render_spend_panel(totals)

    @router.get("/dashboard/agents", response_class=HTMLResponse)
    async def dashboard_agents_partial(
        request: Request, owner: str = "", mine: str = "", view: str = "cards"
    ) -> HTMLResponse:
        identity = getattr(request.state, "operator_identity", None)
        subject = identity.subject if (mine and identity is not None) else ""
        if view != "diagnostics":
            cards = await _render_agent_cards(
                store,
                heartbeat_timeout_seconds,
                owner=owner,
                owner_subject=subject,
                viewer_subject=identity.subject if identity is not None else "",
                launcher=_launcher(request),
            )
            return HTMLResponse(_agent_card_view(cards, owner=owner, mine=bool(mine)))
        rows = await _render_agent_rows(
            store,
            heartbeat_timeout_seconds,
            owner=owner,
            owner_subject=subject,
            viewer_subject=identity.subject if identity is not None else "",
        )
        return HTMLResponse(_agent_table(rows, owner=owner, mine=bool(mine), view="diagnostics"))

    @router.get("/dashboard/fleet", response_class=HTMLResponse)
    async def dashboard_fleet_partial(
        request: Request, owner: str = "", mine: str = "", view: str = "cards"
    ) -> HTMLResponse:
        """The whole fleet section; polled only while the fleet is empty."""
        return HTMLResponse(
            await _render_agent_overview(
                store,
                heartbeat_timeout_seconds,
                has_identity=getattr(request.state, "operator_identity", None) is not None,
                viewer_subject=_viewer_subject(request),
                owner=owner,
                mine=bool(mine),
                view=view,
                launcher=_launcher(request),
            )
        )

    @router.get("/dashboard/overview", response_class=HTMLResponse)
    async def dashboard_overview_partial(compact: str = "") -> HTMLResponse:
        """The fleet health block alone; the table and filter refresh separately.

        Args:
            compact: Non-empty for the one-line strip the Agents page shows.
        """
        try:
            rows_data = await store.list_agents_with_personas()
        except Exception as exc:
            logger.error(f"Dashboard overview query failed: {exc}")
            return HTMLResponse('<p class="audit-failure">Could not load fleet status.</p>')
        if compact:
            return HTMLResponse(
                _fleet_health_poll(
                    render_health_strip(rows_data, heartbeat_timeout_seconds), compact=True
                )
            )
        return HTMLResponse(
            _fleet_health_poll(render_fleet_overview(rows_data, heartbeat_timeout_seconds))
        )

    # ── Project docs ─────────────────────────────────────────────────────────

    @router.get("/dashboard/docs", response_class=HTMLResponse)
    async def dashboard_docs(request: Request) -> HTMLResponse:
        body = _project_docs()
        return HTMLResponse(_render_page(request, body))

    # ── Operators ────────────────────────────────────────────────────────────

    @router.get(
        "/dashboard/operators",
        response_class=HTMLResponse,
        dependencies=[Depends(auth.require_admin)],
    )
    async def operators_page(request: Request) -> HTMLResponse:
        operators = await store.list_dashboard_operators()
        rows = "".join(_render_operator_row(operator) for operator in operators)
        paid_note = (
            "Free mode is on: everyone's agents run free models unless <b>paid models</b> "
            "is ticked for their owner. Admins always may. A free user's agent on a paid "
            "persona runs a free fallback model instead."
            if free_only
            else "Free mode is off, so every agent may run paid models; <b>paid models</b> "
            "takes effect when <code>llm.free_only</code> is turned on."
        )
        body = f"""\
<h2>Operators</h2>
<p class="doc-muted">Cloudflare Access decides who may sign in. Agent Hub assigns
what each verified identity may do. New identities start as viewers.</p>
<p class="doc-muted">{paid_note}</p>
<div id="operator-result" role="status" aria-live="polite"></div>
<table>
<thead><tr><th>email</th><th>authorization</th></tr></thead>
<tbody>{rows or '<tr><td colspan="2">No operators have signed in.</td></tr>'}</tbody>
</table>
<div class="doc-grid" style="margin-top:1.5rem">
  <div class="doc-card"><h3>Admin</h3><p>Manage operators and all dashboard actions.</p></div>
  <div class="doc-card"><h3>Operator</h3><p>Manage devices, personas, and models.</p></div>
  <div class="doc-card"><h3>Viewer</h3><p>Read dashboard status and history only.</p></div>
</div>"""
        return HTMLResponse(_render_page(request, body))

    @router.post(
        "/dashboard/operators/{subject}",
        response_class=HTMLResponse,
        dependencies=[Depends(auth.require_admin)],
    )
    async def operator_update(
        subject: str,
        role: str = Form(...),
        enabled: str = Form(default=""),
        paid_models: str = Form(default=""),
    ) -> HTMLResponse:
        try:
            parsed_role = OperatorRole(role)
        except ValueError:
            raise HTTPException(status_code=422, detail="Unknown operator role") from None
        ok = await store.update_dashboard_operator(
            subject,
            parsed_role,
            enabled=enabled == "1",
            paid_models=paid_models == "1",
        )
        if not ok:
            return HTMLResponse(
                '<p style="color:#f85149">Not changed. The final enabled admin '
                "cannot be disabled or demoted.</p>",
                status_code=409,
            )
        logger.info(f"Dashboard operator {subject!r} updated to {parsed_role.value}")
        return HTMLResponse('<p class="msg">✓ Operator updated. Refresh to confirm.</p>')

    # ── Audit timeline ───────────────────────────────────────────────────────

    @router.get(
        "/dashboard/audit",
        response_class=HTMLResponse,
        dependencies=[Depends(auth.require_admin)],
    )
    async def audit_page(request: Request) -> HTMLResponse:
        events = await store.list_audit_events(limit=200)
        body = f"""\
<h2>Audit timeline</h2>
<p class="doc-muted">The latest 200 authenticated dashboard changes. This log stores
identity and action metadata only—not prompts, transcripts, tokens, or form values.</p>
{render_audit_table(events)}"""
        return HTMLResponse(_render_page(request, body))

    # ── Agent detail ─────────────────────────────────────────────────────────

    @router.get("/dashboard/agents/{device_id}/history", response_class=HTMLResponse)
    async def agent_history_partial(device_id: str) -> HTMLResponse:
        import urllib.parse as _up

        def _render_content(raw: str) -> str:
            """Replace [image:path] markers with inline <img> tags.

            Text outside the markers is HTML-escaped. Transcript content is
            device- and LLM-supplied, so it must not be able to inject markup.
            """
            without_internal = re.sub(r"\n?\[volatile-tools:[^\]]+\]", "", raw).strip()
            parts: list[str] = []
            last = 0
            for match in re.finditer(r"\[image:([^\]]+)\]", without_internal):
                parts.append(html.escape(without_internal[last : match.start()]))
                enc = _up.quote(match.group(1), safe="")
                parts.append(
                    f'<br><img src="/dashboard/image?path={enc}" '
                    f'style="max-width:320px;border-radius:6px;margin-top:0.4rem;display:block">'
                )
                last = match.end()
            parts.append(html.escape(without_internal[last:]))
            return "".join(parts)

        persona = await store.get_persona_for_device(device_id)
        is_transcriber = bool(persona and persona.transcription)
        if is_transcriber:
            # A transcription session is the unit of "complete memory" — show the
            # whole current session, never a tail.
            session_id = await store.latest_session_id(device_id)
            turns = await store.load_session(device_id, session_id)
            caption = (
                f"Session {html.escape(session_id)} · {len(turns)} lines"
                if session_id
                else "No session yet"
            )
        else:
            turns = await store.load_history(device_id, limit=60)
            caption = f"{len(turns)} messages"
        if not turns:
            return HTMLResponse('<p style="color:#6e7681">No history yet.</p>')
        rows = "".join(
            f"<tr>"
            f'<td style="color:#8b949e;white-space:nowrap;font-size:0.75rem">'
            f"{fmt_ts(t.get('created_at'), display_tz, '%Y-%m-%d %H:%M:%S')}</td>"
            f'<td style="color:{"#79c0ff" if t["role"] == "user" else "#3fb950"};'
            f'white-space:nowrap">{t["role"]}</td>'
            f'<td style="white-space:pre-wrap;max-width:600px">'
            f"{_render_content(t['content'])}</td></tr>"
            for t in turns
        )
        return HTMLResponse(
            f'<table style="width:100%"><thead><tr>'
            f"<th>time</th><th>role</th><th>content</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f'<p style="color:#8b949e;font-size:0.8rem">{caption}</p>'
        )

    async def _conversation_list(device_id: str, before: int | None = None) -> str:
        persona = await store.get_persona_for_device(device_id)
        noun = "recording" if (persona and persona.transcription) else "conversation"
        page = await store.list_conversations(device_id, before_id=before, limit=_PAGE_SIZE)
        more = None
        if len(page) == _PAGE_SIZE:
            older = await store.list_conversations(device_id, before_id=page[-1].id, limit=1)
            more = page[-1].id if older else None
        return conversations_view.render_list(
            page, device_id, display_tz, more_before_id=more, noun=noun
        )

    @router.get("/dashboard/agents/{device_id}/conversations", response_class=HTMLResponse)
    async def agent_conversations(device_id: str, before: int | None = None) -> HTMLResponse:
        """One page of an agent's conversations, newest first."""
        return HTMLResponse(await _conversation_list(device_id, before))

    @router.post("/dashboard/agents/{device_id}/conversations/new", response_class=HTMLResponse)
    async def agent_conversation_new(device_id: str) -> HTMLResponse:
        """End the open conversation; the next turn starts a new one."""
        ended = await store.end_open_conversations(device_id)
        logger.info(f"Dashboard ended {ended} open conversation(s) for {device_id!r}")
        return HTMLResponse(await _conversation_list(device_id))

    @router.get("/dashboard/agents/{device_id}/context", response_class=HTMLResponse)
    async def agent_context(device_id: str) -> HTMLResponse:
        """What the model will be sent on the next turn."""
        settings = await conversation_settings_for_device(store, device_id)
        persona = await store.get_persona_for_device(device_id)
        open_conversation = await store.open_conversation(
            device_id, persona=persona, idle_minutes=settings.idle_minutes, create=False
        )
        conversation_id = open_conversation.id if open_conversation else None
        note = await memory_note_for_turn(store, config, device_id, conversation_id, settings)
        messages = (
            history_for_llm(
                await store.load_history(
                    device_id, limit=settings.memory_window * 2, conversation_id=conversation_id
                )
            )
            if conversation_id is not None
            else []
        )
        return HTMLResponse(conversations_view.render_context(note, messages, device_id))

    @router.get("/dashboard/agents/{device_id}/conversations.json")
    async def agent_conversations_json(device_id: str, before: int | None = None) -> JSONResponse:
        page = await store.list_conversations(device_id, before_id=before, limit=_PAGE_SIZE)
        return JSONResponse(
            {"conversations": [conversations_view.conversation_json(c) for c in page]}
        )

    async def _conversation_of(device_id: str, public_id: str) -> Conversation | None:
        conversation = await store.get_conversation(public_id)
        return conversation if conversation and conversation.device_id == device_id else None

    @router.get("/dashboard/agents/{device_id}/conversations/{public_id}.json")
    async def agent_conversation_json(device_id: str, public_id: str) -> JSONResponse:
        conversation = await _conversation_of(device_id, public_id)
        if conversation is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        data = conversations_view.conversation_json(conversation)
        data["messages"] = await store.conversation_messages(conversation.id)
        return JSONResponse(data)

    @router.get("/dashboard/agents/{device_id}/conversations/{public_id}.{suffix}")
    async def agent_conversation_export(device_id: str, public_id: str, suffix: str) -> Response:
        """Download one conversation as .txt or .md."""
        if suffix not in {"txt", "md"}:
            return Response(status_code=404)
        conversation = await _conversation_of(device_id, public_id)
        if conversation is None:
            return Response(status_code=404)
        agent = await store.get_agent(device_id)
        body = conversations_view.export_text(
            conversation,
            await store.conversation_messages(conversation.id),
            display_tz,
            agent_label=(agent.label or device_id) if agent else device_id,
            device_id=device_id,
            markdown=suffix == "md",
        )
        filename = conversations_view.export_filename(conversation, device_id, suffix)
        return Response(
            content=body,
            media_type=("text/markdown" if suffix == "md" else "text/plain") + "; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get(
        "/dashboard/agents/{device_id}/conversations/{public_id}", response_class=HTMLResponse
    )
    async def agent_conversation_page(
        device_id: str, public_id: str, request: Request
    ) -> HTMLResponse:
        """One conversation in full: every message, uncapped."""
        conversation = await _conversation_of(device_id, public_id)
        if conversation is None:
            return HTMLResponse(_render_page(request, "<p>Conversation not found.</p>"), 404)
        agent = await store.get_agent(device_id)
        messages = await store.conversation_messages(conversation.id)
        body = conversations_view.render_conversation(
            conversation,
            messages,
            device_id,
            display_tz,
            agent_label=(agent.label or device_id) if agent else device_id,
        )
        return HTMLResponse(_render_page(request, body))

    async def _conversation_actions(device_id: str, public_id: str, note: str) -> HTMLResponse:
        conversation = await _conversation_of(device_id, public_id)
        if conversation is None:
            return HTMLResponse("<p>Conversation not found.</p>", status_code=404)
        agent = await store.get_agent(device_id)
        rendered = conversations_view.render_conversation(
            conversation,
            [],
            device_id,
            display_tz,
            agent_label=(agent.label or device_id) if agent else device_id,
        )
        start = rendered.index('<div id="conversation-actions"')
        end = rendered.index("</div>", rendered.index("Delete</button>")) + len("</div>")
        return HTMLResponse(rendered[start:end] + note)

    @router.post(
        "/dashboard/agents/{device_id}/conversations/{public_id}/rename",
        response_class=HTMLResponse,
    )
    async def agent_conversation_rename(
        device_id: str, public_id: str, title: str = Form(default="")
    ) -> HTMLResponse:
        if await _conversation_of(device_id, public_id) is None:
            return HTMLResponse("<p>Conversation not found.</p>", status_code=404)
        await store.rename_conversation(public_id, title)
        return await _conversation_actions(device_id, public_id, '<p class="msg">✓ Renamed.</p>')

    @router.post(
        "/dashboard/agents/{device_id}/conversations/{public_id}/title",
        response_class=HTMLResponse,
    )
    async def agent_conversation_title(device_id: str, public_id: str) -> HTMLResponse:
        """Ask the model for a title and summary now, whatever the settings say."""
        conversation = await _conversation_of(device_id, public_id)
        if conversation is None:
            return HTMLResponse("<p>Conversation not found.</p>", status_code=404)
        result = await wrap_up_conversation(store, config, conversation, force=True)
        note = (
            '<p class="msg">✓ Named.</p>'
            if result.status == "model"
            else f'<p class="msg" style="color:#d29922">Could not name it: '
            f"{html.escape(result.error or result.status)}</p>"
        )
        return await _conversation_actions(device_id, public_id, note)

    @router.post(
        "/dashboard/agents/{device_id}/conversations/{public_id}/delete",
        response_class=HTMLResponse,
    )
    async def agent_conversation_delete(device_id: str, public_id: str) -> Response:
        if await _conversation_of(device_id, public_id) is None:
            return HTMLResponse("<p>Conversation not found.</p>", status_code=404)
        await store.delete_conversation(public_id)
        logger.info(f"Dashboard deleted conversation {public_id!r} of {device_id!r}")
        return Response(
            status_code=204, headers={"HX-Redirect": f"/dashboard/agents/{quote(device_id)}"}
        )

    @router.get("/dashboard/agents/{device_id}/transcript.txt")
    async def agent_transcript_download(device_id: str, session: str = "") -> Response:
        """The old transcript link: one session now redirects to its conversation."""
        agent = await store.get_agent(device_id)
        if agent is None:
            return Response(status_code=404)
        if session != "all":
            wanted = session or await store.latest_session_id(device_id)
            if wanted and await store.get_conversation(wanted) is not None:
                return RedirectResponse(
                    f"/dashboard/agents/{quote(device_id)}/conversations/"
                    f"{quote(wanted, safe='')}.txt",
                    status_code=307,
                )
        # ?session=<id> exports one transcription session; ?session=all the whole
        # history; default is the current (latest) session, or all history for a
        # device that has never run a transcription session.
        if session == "all":
            turns = await store.export_history(device_id)
            scope = "all history"
        else:
            session_id = session or await store.latest_session_id(device_id)
            if session_id:
                turns = await store.export_history(device_id, session_id=session_id)
                scope = f"session {session_id}"
            else:
                turns = await store.export_history(device_id)
                scope = "all history"
        header = (
            f"Transcript — {agent.label or device_id}\n"
            f"Device {device_id}\n"
            f"Scope: {scope}\n"
            f"Exported {fmt_ts(datetime.now(UTC), display_tz, '%Y-%m-%d %H:%M:%S %Z')}\n"
            f"{'=' * 48}\n\n"
        )
        lines: list[str] = []
        for t in turns:
            stamp = fmt_ts(t.get("created_at"), display_tz, "%H:%M:%S")
            content = re.sub(r"\[image:[^\]]+\]\s*", "[photo] ", t["content"]).strip()
            content = re.sub(r"\n?\[volatile-tools:[^\]]+\]", "", content).strip()
            lines.append(f"[{stamp}] {content}" if content else f"[{stamp}] [photo]")
        body = header + "\n".join(lines) + "\n"
        safe = device_id.replace(":", "-")
        return Response(
            content=body,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="transcript-{safe}.txt"'},
        )

    @router.get("/dashboard/agents/{device_id}/pipeline_status", response_class=HTMLResponse)
    async def agent_pipeline_status(device_id: str) -> HTMLResponse:

        _phase, text = session_state.get_pipeline_status(device_id)
        agent = await store.get_agent(device_id)
        activity = session_state.get_device_activity(
            device_id, agent.reported_activity if agent else None
        )
        phase_styles = {
            "idle": ("color:#6e7681", "Idle"),
            "listening": ("color:#d29922", "Listening"),
            "thinking": ("color:#58a6ff", "Thinking"),
            "speaking": ("color:#3fb950", "Speaking"),
            "paused": ("color:#d29922", "Paused"),
        }
        style, label = phase_styles[activity]
        snippet = (
            f' <span style="color:#8b949e;font-size:0.8rem">{text[:80]}</span>'
            if text and activity not in ("idle", "speaking")
            else ""
        )
        return HTMLResponse(f'<span style="{style}">{label}</span>{snippet}')

    @router.get("/dashboard/agents/{device_id}/status", response_class=HTMLResponse)
    async def agent_status_partial(device_id: str) -> HTMLResponse:
        ws_connected = session_state.is_connected(device_id)
        mcp_client = session_state.get_mcp_client(device_id)
        agent = await store.get_agent(device_id)
        db_status = agent.status if agent else "unknown"
        health = session_state.get_device_health(
            device_id,
            agent.last_heartbeat if agent else None,
            agent.health_fault if agent else None,
            heartbeat_timeout_seconds,
        )
        activity = session_state.get_device_activity(
            device_id, agent.reported_activity if agent else None
        )

        # A bridged agent (robot or page) has no voice socket and no device
        # MCP handshake. Describing it in firmware terms — "wake-word
        # standby", "MCP —" — reads as broken when it is working fine, so
        # report the bridge instead.
        bridged = agent is not None and agent.kind != AgentKind.XIAOZHI.value
        bridge = mcp_bridge.get_page_agent(device_id) if bridged else None

        transport_label = "Bridge" if bridged else "Voice transport"
        if bridged:
            if bridge is not None and bridge.connected:
                ws_html = '<span style="color:#3fb950">● connected</span>'
            elif bridge is not None:
                ws_html = '<span style="color:#d29922">○ registered, stream closed</span>'
            else:
                ws_html = '<span style="color:#6e7681">○ not connected</span>'
        elif ws_connected:
            ws_html = '<span style="color:#3fb950">● connected</span>'
        else:
            ws_html = '<span style="color:#6e7681">○ closed (wake-word standby)</span>'

        if bridged:
            tools = sorted(bridge.tools) if bridge else []
            mcp_html = (
                f'<span style="color:#3fb950">● {len(tools)} tools</span> '
                f'<span style="color:#8b949e;font-size:0.8rem">— {", ".join(tools)}</span>'
                if tools
                else '<span style="color:#6e7681">○ no tools declared</span>'
            )
        elif mcp_client and mcp_client.ready:
            tool_names = ", ".join(mcp_client.tools.keys())
            mcp_html = (
                f'<span style="color:#3fb950">● ready</span> '
                f'<span style="color:#8b949e;font-size:0.8rem">'
                f"— {len(mcp_client.tools)} tools: {tool_names}</span>"
            )
        elif mcp_client and not mcp_client.ready:
            mcp_html = '<span style="color:#d29922">⚠ handshake pending</span>'
        elif ws_connected:
            mcp_html = '<span style="color:#6e7681">— not supported</span>'
        else:
            mcp_html = '<span style="color:#6e7681">○ —</span>'

        health_color = {"healthy": "#3fb950", "degraded": "#d29922", "offline": "#6e7681"}[health]
        llm_error = session_state.get_llm_error(device_id)
        llm_error_row = (
            '<tr><th>Model</th><td><span style="color:#f85149">✗ last turn got no reply</span> '
            f"<code>{html.escape(str(llm_error['model']))}</code>: "
            f"{html.escape(str(llm_error['error']))} "
            f'<span class="doc-muted">({_ago(llm_error["at"])})</span></td></tr>'
            if llm_error
            else ""
        )
        model_notice = session_state.get_state(device_id).model_notice
        model_notice_row = (
            f'<tr><th>Model</th><td><span style="color:#d29922">⚠ {html.escape(model_notice)}'
            "</span></td></tr>"
            if model_notice
            else ""
        )
        return HTMLResponse(f"""\
<table style="width:auto;margin-bottom:0.5rem">
  <tr><th style="width:7rem">Health</th>
      <td><span style="color:{health_color}">{health.title()}</span></td></tr>
  <tr><th>Activity</th><td>{activity.title()}</td></tr>
  <tr><th>{transport_label}</th><td>{ws_html}</td></tr>
  <tr><th>MCP</th><td>{mcp_html}</td></tr>
  <tr><th>Registration</th><td>{db_status}</td></tr>
  {llm_error_row}{model_notice_row}
</table>""")

    @router.get("/dashboard/spend.json")
    async def spend_json() -> dict[str, Any]:
        """LLM spend so far, against the configured caps."""
        tracker = spend.get_tracker()
        if tracker is None:
            # Metering is wired up at server startup; a dashboard mounted
            # standalone (as in tests) has none.
            return {"enabled": False}
        totals = await tracker.totals()
        totals["enabled"] = True
        totals["by_model_today"] = await store.llm_spend_by_model(since=spend.day_start())
        return totals

    @router.get("/dashboard/agents/{device_id}/status.json")
    async def agent_status_json(device_id: str) -> dict[str, Any]:
        """Return live status and capability data for one registered device."""
        agent = await store.get_agent(device_id)
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        persona = await store.get_persona_for_device(device_id)
        dev = session_state.get_state(device_id)
        mcp_client = session_state.get_mcp_client(device_id)
        discovered_tools = _discovered_mcp_tools(
            mcp_client, dev.mcp_tools or agent.reported_mcp_tools_list
        )
        discovered_tool_names = [tool["name"] for tool in discovered_tools]
        persona_allowlist = persona.mcp_tools_allowlist_list if persona else None
        effective_tools = tool_policy.allowed_device_tools(
            discovered_tool_names,
            persona_allowlist,
        )
        pipeline_phase, pipeline_text = session_state.get_pipeline_status(device_id)
        health = session_state.get_device_health(
            device_id,
            agent.last_heartbeat,
            agent.health_fault,
            heartbeat_timeout_seconds,
        )
        activity = session_state.get_device_activity(device_id, agent.reported_activity)

        return {
            "device_id": agent.device_id,
            "kind": agent.kind,
            "status": agent.status,
            "health": health,
            "activity": activity,
            "connected": session_state.is_connected(device_id),
            "ip_address": agent.ip_address,
            "firmware_version": agent.firmware_version,
            "last_seen": agent.last_seen.isoformat() if agent.last_seen else None,
            "last_heartbeat": (agent.last_heartbeat.isoformat() if agent.last_heartbeat else None),
            "health_fault": agent.health_fault,
            "persona": _persona_status(persona),
            "mcp": {
                "connected": mcp_client is not None,
                "ready": bool(mcp_client and mcp_client.ready),
                "tool_count": len(discovered_tools),
                "tools": discovered_tools,
            },
            "effective_tool_allowlist": effective_tools,
            "last_tool_results": dev.last_tool_results,
            "pipeline": {
                "phase": pipeline_phase,
                "previous_phase": session_state.get_prev_pipeline_phase(device_id),
                "text": pipeline_text,
                "age_seconds": round(session_state.get_pipeline_age(device_id), 3),
            },
            "latency": {
                "turns": dev.turns,
                "last": _latency_status(dev.last),
                "avg": _latency_status(dev.avg),
            },
        }

    @router.post("/dashboard/agents/{device_id}/assign_persona", response_class=HTMLResponse)
    async def agent_assign_persona(device_id: str, persona_name: str = Form(...)) -> HTMLResponse:
        ok = await store.assign_persona(device_id, persona_name)
        if not ok:
            return HTMLResponse(
                '<p style="color:#f85149">Assignment failed — persona or device not found.</p>'
            )
        return HTMLResponse(
            f'<p class="msg">✓ Assigned <strong>{persona_name}</strong>. '
            f"Takes effect on next voice session.</p>"
        )

    @router.get("/dashboard/agents/{device_id}", response_class=HTMLResponse)
    async def agent_detail(device_id: str, request: Request) -> HTMLResponse:
        agent = await store.get_agent(device_id)
        if agent is None:
            return HTMLResponse(_render_page(request, "<p>Agent not found.</p>"))
        persona = await store.get_persona_for_device(device_id)
        all_personas = await store.list_personas()
        dev = session_state.get_state(device_id)

        # Persona section
        persona_options = "".join(
            f'<option value="{p.name}" {"selected" if persona and p.name == persona.name else ""}>'
            f"{p.name}</option>"
            for p in all_personas
        )
        assign_form = f"""\
<form hx-post="/dashboard/agents/{device_id}/assign_persona"
      hx-target="#assign-result" hx-swap="innerHTML"
      style="display:inline-flex;gap:0.5rem;align-items:center">
  <select name="persona_name">{persona_options}</select>
  <button type="submit">Assign</button>
</form>
<span id="assign-result" role="status" aria-live="polite"
      style="margin-left:0.5rem"></span>"""
        if persona:
            model_str = (
                persona.llm_model
                or config.get("llm", {}).get("openai", {}).get("model", "")
                or f"{persona.llm_provider} default"
            )
            base_url = config.get("llm", {}).get("openai", {}).get("base_url", "")
            provider_detail = f"{persona.llm_provider}"
            if base_url:
                provider_detail += (
                    f' <span style="color:#8b949e;font-size:0.75rem">({base_url})</span>'
                )
            persona_html = f"""\
<h3>Persona</h3>
{assign_form}
<table style="width:auto;margin-top:0.75rem">
  <tr><th>name</th><td>{persona.name} &nbsp;
    <a href="/dashboard/personas/{persona.name}"
       style="color:#58a6ff;font-size:0.8rem">edit →</a></td></tr>
  <tr><th>model</th><td>{model_str}</td></tr>
  <tr><th>LLM provider</th><td>{provider_detail}</td></tr>
  <tr><th>TTS provider</th><td>{
                " / ".join(
                    x for x in (persona.tts_provider, persona.tts_model, persona.tts_voice) if x
                )
            }</td></tr>
  <tr><th>ASR provider</th><td>{persona.asr_provider}</td></tr>
  <tr><th>system prompt</th><td style="white-space:pre-wrap;max-width:600px">{
                persona.system_prompt or "—"
            }</td></tr>
</table>"""
        else:
            persona_html = f"<h3>Persona</h3><p>No persona assigned.</p>{assign_form}"

        # Tools section
        import agent_hub.skills as _skills

        device_tool_badges = (
            "".join(
                f'<span class="badge badge-tool">{html.escape(t)}</span>'
                for t in (dev.mcp_tools or agent.reported_mcp_tools_list)
            )
            or '<span style="color:#6e7681">none discovered yet</span>'
        )
        skill_badges = "".join(
            f'<span class="badge badge-skill">{d["function"]["name"]}</span>'
            for d in _skills.get_definitions()
        )

        # Latency section
        if dev.turns > 0:
            L, A = dev.last, dev.avg
            lat_html = f"""\
<table style="width:auto">
  <tr><th></th><th>last turn</th><th>avg (EMA)</th></tr>
  <tr><td>ASR</td><td>{L.asr_ms} ms</td><td>{A.asr_ms} ms</td></tr>
  <tr><td>LLM</td><td>{L.llm_ms} ms</td><td>{A.llm_ms} ms</td></tr>
  <tr><td>TTS</td><td>{L.tts_ms} ms</td><td>{A.tts_ms} ms</td></tr>
  <tr><td><strong>total</strong></td>
      <td><strong>{L.total_ms} ms</strong></td>
      <td><strong>{A.total_ms} ms</strong></td></tr>
</table>
<p style="color:#8b949e;font-size:0.8rem">{dev.turns} turns recorded this session</p>"""
        else:
            lat_html = '<p style="color:#6e7681">No turns recorded this session.</p>'

        # Camera capture button (only for devices with the camera tool)
        dev_tools = session_state.get_state(device_id).mcp_tools
        has_camera = any("camera" in t or "photo" in t for t in dev_tools)
        camera_btn = ""
        if has_camera:
            camera_btn = f"""\
<form hx-post="/dashboard/agents/{device_id}/capture"
      hx-target="#capture-result" hx-swap="innerHTML" style="display:inline">
  <button type="submit" style="background:#1a4a6e">📷 Capture photo</button>
</form>
<div id="capture-result" role="status" aria-live="polite"
     style="margin-top:0.75rem"></div>"""

        is_transcriber = bool(persona and persona.transcription)

        # Assistant-only actions: both drive the *device* voice pipeline, so
        # they need a xiaozhi board in an assistant persona. A transcriber
        # never replies, and a bridged agent has no voice socket to speak
        # through — it gets the tool console and "Ask this agent" instead.
        assistant_actions = (
            ""
            if is_transcriber or agent.kind != AgentKind.XIAOZHI.value
            else f"""\
<h3>Inject utterance</h3>
<p style="color:#8b949e;font-size:0.85rem">
  Simulate speech — runs the full LLM pipeline and speaks the reply on the device.
</p>
<form hx-post="/dashboard/agents/{device_id}/inject"
      hx-target="#inject-result" hx-swap="innerHTML"
      style="display:flex;gap:0.5rem;align-items:center">
  <input type="text" name="text" value="tell me what you see"
         aria-label="Utterance to inject" style="width:360px">
  <button type="submit" style="background:#1a4a6e">▶ Inject</button>
</form>
<div id="inject-result" role="status" aria-live="polite"
     style="margin-top:0.5rem"></div>
<h3>Send message to device</h3>
<form hx-post="/dashboard/agents/{device_id}/speak"
      hx-target="#speak-result" hx-swap="innerHTML">
  <input type="text" name="text" placeholder="Say something..."
         aria-label="Message to speak" style="width:400px">
  <button type="submit">Speak</button>
</form>
<div id="speak-result" role="status" aria-live="polite"></div>"""
        )

        noun = "recording" if is_transcriber else "conversation"
        history_heading = "Recordings" if is_transcriber else "Conversations"
        clear_confirm = f"Delete every {noun} for this device, with its messages and summaries?"

        # Reboot is a firmware action. Only a xiaozhi board has firmware; a
        # page agent or a robot script has nothing to reboot.
        reboot_btn = (
            ""
            if agent.kind != AgentKind.XIAOZHI.value
            else f"""\
<form hx-post="/dashboard/agents/{device_id}/reboot"
      hx-target="#reboot-result" hx-swap="innerHTML"
      hx-confirm="Reboot this device now? Its active session will disconnect."
      style="display:inline">
  <button type="submit" style="background:#6e3a1e">↺ Reboot device</button>
</form>"""
        )
        pin_form = _pin_form(device_id, agent.pinned)
        owner_form = (
            "<h3>Owner</h3>"
            '<p style="color:#8b949e;font-size:0.85rem">Whose agent this is. Used to filter '
            "the fleet list on a busy build night; it does not restrict who can drive it.</p>"
            + _claim_panel(
                agent,
                getattr(request.state, "operator_identity", None),
                _role(request),
            )
        )

        # Tool console — call one tool by hand, no model in the loop. For a
        # bridged agent (robot or page) this is the fastest way to answer
        # "did my firmware actually wire that tool up?".
        bridged = mcp_bridge.get_page_agent(device_id)
        console_html = ""
        if bridged is not None and bridged.tools:
            forms = []
            for tname, tdata in bridged.tools.items():
                props = tdata.get("inputSchema", {}).get("properties", {}) or {}
                example = (
                    "{" + ", ".join(f'"{k}": ' for k in list(props)[:3]).rstrip(", ") + "}"
                    if props
                    else "{}"
                )
                desc = html.escape(str(tdata.get("description") or ""))
                forms.append(f"""\
<details class="tool-console">
  <summary>{html.escape(tname)}</summary>
  <p style="color:#8b949e;font-size:0.8rem;margin:.2rem 0">{desc}</p>
  <form hx-post="/dashboard/agents/{device_id}/call_tool"
        hx-target="#result-{html.escape(tname).replace(".", "-")}" hx-swap="innerHTML">
    <input type="hidden" name="tool" value="{html.escape(tname)}">
    <label style="font-size:0.8rem;color:#8b949e">arguments (JSON)</label>
    <input type="text" name="arguments" value='{example}' aria-label="Tool arguments JSON">
    <button type="submit" style="background:#1a4a6e">▶ Call</button>
  </form>
  <div id="result-{html.escape(tname).replace(".", "-")}" role="status" aria-live="polite"></div>
</details>""")
            console_html = (
                "<h3>Tool console</h3>"
                '<p style="color:#8b949e;font-size:0.85rem">Call one tool directly, with no '
                "model deciding for you. Arguments are JSON.</p>" + "".join(forms)
            )
        if bridged is not None and bridged.connected:
            console_html += f"""\
<h3>Ask this agent</h3>
<p style="color:#8b949e;font-size:0.85rem">Runs a full turn with its persona and tools —
the same loop the page agent and voice sessions use. Costs one model call.</p>
<form hx-post="/dashboard/agents/{device_id}/ask"
      hx-target="#ask-result" hx-swap="innerHTML">
  <input type="text" name="text" placeholder="what can you do?" aria-label="Message"
         style="width:400px">
  <button type="submit" style="background:#1a4a6e">▶ Ask</button>
</form>
<div id="ask-result" role="status" aria-live="polite" style="margin-top:0.5rem"></div>"""

        # Named page agents come back under the same id when their name is
        # reopened, so keeping their history is the default there.
        keep_checked = " checked" if is_named_page_agent(agent) else ""
        remove_btn = f"""\
<form hx-post="/dashboard/agents/{device_id}/remove" style="display:inline"
      hx-confirm="Remove this agent? A device re-registers on its next check-in, and a
named page agent when its name is reopened.">
  <button type="submit" style="background:#b62324">✕ Remove agent</button>
  <label style="font-size:0.8rem;color:#8b949e"><input type="checkbox" name="keep_history"
    value="1"{keep_checked}> keep conversation history</label>
</form>"""
        # Open the agent from here: the conversation panel, and for a stopped
        # page agent its owner can relaunch, a new tab running it.
        connected = is_agent_connected(agent)
        agent_actions = (
            '<div class="agent-card-actions" style="border-top:0;padding-top:0;margin-top:0">'
            + interaction_link(agent, persona, connected)
            + launch_link(agent, connected, _launcher(request))
            + "</div>"
        )
        speak_form = f"""\
<span id="device-actions"></span>
{reboot_btn}
{camera_btn}
{pin_form}
{remove_btn}
{owner_form}
<span id="interaction"></span>
{console_html}
<span id="reboot-result" role="status" aria-live="polite"
      style="margin-left:0.75rem"></span>
{assistant_actions}
<h3>{history_heading}</h3>
<div style="margin-bottom:0.4rem;font-size:0.85rem">
  Pipeline:&nbsp;<span
    hx-get="/dashboard/agents/{device_id}/pipeline_status"
    hx-trigger="load, every 1s"
    hx-swap="innerHTML"
    id="pipeline-status">—</span>
</div>
<div style="margin-bottom:0.5rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
  <form hx-post="/dashboard/agents/{device_id}/conversations/new"
        hx-target="#conversation-list" hx-swap="outerHTML" style="display:inline">
    <button type="submit">New {noun}</button>
  </form>
  <span class="doc-muted" style="font-size:.8rem">Ends the open {noun}; the next turn
  starts a fresh one.</span>
</div>
<div hx-get="/dashboard/agents/{device_id}/conversations"
     hx-trigger="load, every 10s"
     hx-swap="outerHTML"
     id="conversation-list">Loading…</div>
<h3>What the model sees next</h3>
<div hx-get="/dashboard/agents/{device_id}/context"
     hx-trigger="load"
     hx-swap="outerHTML"
     id="model-context">Loading…</div>
<form hx-post="/dashboard/agents/{device_id}/clear_history"
      hx-target="#conversation-list" hx-swap="outerHTML"
      hx-confirm="{clear_confirm}"
      style="margin-top:0.5rem">
  <button type="submit" style="background:#b62324">Delete all {noun}s</button>
</form>"""

        spend_rows = await store.llm_spend_by_device()
        sp = spend_rows.get(device_id)
        spend_line = (
            f"&nbsp;·&nbsp; Spend: ${sp['cost_usd']:.4f} over {sp['calls']} calls" if sp else ""
        )
        body = f"""\
<p><a href="/dashboard/" style="color:#58a6ff">← agents</a></p>
<h2>{html.escape(agent.label or device_id)}
  <span class="badge badge-kind">{html.escape(agent.kind)}</span></h2>
<p style="color:#8b949e;margin-top:-0.5rem">
  Device ID: {html.escape(device_id)} &nbsp;·&nbsp;
  IP: {agent.ip_address or "—"} &nbsp;·&nbsp;
  Firmware: {agent.firmware_version or "—"} &nbsp;·&nbsp;
  Last seen: {fmt_ts(agent.last_seen, display_tz, "%H:%M:%S")}{spend_line}
</p>
{agent_actions}
<h3>Connection</h3>
<div hx-get="/dashboard/agents/{device_id}/status"
     hx-trigger="load, every 3s"
     hx-swap="innerHTML"
     id="device-status">Loading…</div>
{persona_html}
{_conversation_settings_panel(agent, persona)}
<h3>Device MCP tools</h3>
<div>{device_tool_badges}</div>
<h3>Server skills</h3>
<div>{skill_badges}</div>
<h3>Latency</h3>
{lat_html}
{speak_form}"""
        return HTMLResponse(_render_page(request, body))

    @router.post("/dashboard/agents/{device_id}/conversation_settings", response_class=HTMLResponse)
    async def agent_conversation_settings(device_id: str, request: Request) -> HTMLResponse:
        """Save an agent's conversation overrides; blank or "persona" clears one."""
        form = await request.form()
        values: dict[str, Any] = {}
        limits = {
            "conversation_idle_minutes": (1, 1440),
            "memory_window": (1, 200),
            "remember_conversations": (0, 20),
        }
        for name, (low, high) in limits.items():
            raw = str(form.get(name, "")).strip()
            if not raw:
                values[name] = None
                continue
            try:
                values[name] = min(high, max(low, int(raw)))
            except ValueError:
                return HTMLResponse(
                    f'<p class="msg" style="color:#f85149">{html.escape(name)}: '
                    f"{html.escape(raw)!s} is not a whole number.</p>",
                    status_code=400,
                )
        for name in ("auto_title", "summarize_conversations"):
            choice = str(form.get(name, "")).strip()
            values[name] = {"on": True, "off": False}.get(choice)
        if not await store.set_agent_conversation_settings(device_id, values):
            return HTMLResponse("<p>Agent not found.</p>", status_code=404)
        agent = await store.get_agent(device_id)
        persona = await store.get_persona_for_device(device_id)
        return HTMLResponse(
            _conversation_settings_panel(agent, persona)
            + '<p class="msg">✓ Saved. Takes effect on the next turn.</p>'
        )

    @router.post("/dashboard/agents/{device_id}/reboot", response_class=HTMLResponse)
    async def agent_reboot(device_id: str) -> HTMLResponse:
        # Try WebSocket reboot first
        send_json = session_state.get_send_json(device_id)
        if send_json:
            try:
                await send_json({"type": "reboot"})
                return HTMLResponse('<p class="msg">↺ Reboot sent via WebSocket.</p>')
            except Exception as exc:
                logger.warning(f"WS reboot failed for {device_id}: {exc}")

        # Fall back to USB serial !reboot
        import asyncio as _asyncio
        import glob

        ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if not ports:
            return HTMLResponse(
                '<p style="color:#f85149">No serial port found and device not connected.</p>'
            )
        try:
            import serial as _serial

            port = ports[0]

            def _send_serial() -> None:
                with _serial.Serial(port, 115200, timeout=1) as ser:
                    ser.write(b"!reboot\r\n")

            await _asyncio.to_thread(_send_serial)
            return HTMLResponse(f'<p class="msg">↺ Reboot sent via {port}.</p>')
        except Exception as exc:
            return HTMLResponse(f'<p style="color:#f85149">Serial reboot failed: {exc}</p>')

    @router.post("/dashboard/agents/{device_id}/capture", response_class=HTMLResponse)
    async def agent_capture(device_id: str) -> HTMLResponse:
        mcp_client = session_state.get_mcp_client(device_id)
        if mcp_client is None or not mcp_client.ready:
            return HTMLResponse(
                '<p style="color:#f85149">Device not connected or MCP not ready.</p>'
            )
        if not any("camera" in t or "photo" in t for t in mcp_client.tools):
            return HTMLResponse(
                '<p style="color:#f85149">No camera tool available on this device.</p>'
            )
        try:
            result = await mcp_client.call_tool(
                "self_camera_take_photo",
                {"question": "Describe what you see in detail."},
                timeout=60.0,
            )
            if isinstance(result, str) and result.startswith("data:"):
                return HTMLResponse(
                    f'<img src="{result}" '
                    f'style="max-width:100%;border-radius:6px;margin-top:0.5rem">'
                    f'<p style="color:#8b949e;font-size:0.8rem">Captured</p>'
                )
            return HTMLResponse(f'<p style="color:#c9d1d9">{result}</p>')
        except Exception as exc:
            return HTMLResponse(f'<p style="color:#f85149">Capture failed: {exc}</p>')

    @router.post("/dashboard/agents/{device_id}/inject", response_class=HTMLResponse)
    async def agent_inject(device_id: str, text: str = Form(...)) -> HTMLResponse:
        if not text.strip():
            return HTMLResponse('<p style="color:#f85149">Empty message.</p>')
        injector = session_state.get_injector(device_id)
        if injector is None:
            return HTMLResponse('<p style="color:#f85149">Device not connected.</p>')
        try:
            reply, img_path = await asyncio.wait_for(injector(text.strip()), timeout=90.0)
        except TimeoutError:
            return HTMLResponse('<p style="color:#f85149">Timed out waiting for reply (>90s).</p>')
        except Exception as exc:
            return HTMLResponse(f'<p style="color:#f85149">Pipeline error: {exc}</p>')
        if not reply:
            return HTMLResponse('<p style="color:#6e7681">Pipeline ran but produced no reply.</p>')
        # Show reply + the image captured *during this turn* only (img_path is None
        # unless this turn actually triggered a capture), so non-camera replies no
        # longer render a stale photo from an earlier turn.
        img_html = ""
        if img_path:
            import urllib.parse as _up

            enc = _up.quote(img_path, safe="")
            img_html = (
                f'<img src="/dashboard/image?path={enc}" '
                f'style="max-width:100%;border-radius:6px;margin-top:0.5rem;display:block">'
            )
        return HTMLResponse(f'<p class="msg">▶ {reply}</p>{img_html}')

    @router.post("/dashboard/agents/{device_id}/clear_history", response_class=HTMLResponse)
    async def agent_clear_history(device_id: str) -> HTMLResponse:
        await store.clear_history(device_id)
        return HTMLResponse('<p style="color:#6e7681">History cleared.</p>')

    @router.post("/dashboard/agents/{device_id}/speak", response_class=HTMLResponse)
    async def agent_speak(device_id: str, text: str = Form(...)) -> HTMLResponse:
        if not text.strip():
            return HTMLResponse('<p style="color:#f85149">Empty message.</p>')
        speak = session_state.get_speak(device_id)
        if speak is None:
            return HTMLResponse('<p style="color:#f85149">Device not connected.</p>')
        try:
            await speak(text.strip())
            return HTMLResponse(f'<p class="msg">✓ sent: "{text.strip()}"</p>')
        except Exception as exc:
            return HTMLResponse(f'<p style="color:#f85149">Error: {exc}</p>')

    # ── Personas ──────────────────────────────────────────────────────────────

    @router.get("/dashboard/personas", response_class=HTMLResponse)
    async def personas_list(request: Request) -> HTMLResponse:
        personas = await store.list_personas()

        def _persona_row(p: Persona) -> str:
            badge = " <span class=badge>transcriber</span>" if p.transcription else ""
            if p.transcription:
                llm_cell = tts_cell = memory_cell = "—"
            else:
                llm_cell = f"{p.llm_provider} / {p.llm_model or 'default'}"
                tts_cell = " / ".join(x for x in (p.tts_provider, p.tts_model, p.tts_voice) if x)
                memory_cell = str(p.memory_window)
            return (
                f'<tr><td><a href="/dashboard/personas/{p.name}" '
                f'style="color:#58a6ff">{p.name}</a>{badge}</td>'
                f"<td>{llm_cell}</td><td>{tts_cell}</td>"
                f"<td>{p.asr_provider}</td><td>{memory_cell}</td>"
                f'<td><a href="/dashboard/personas/{p.name}" style="color:#58a6ff">edit</a>'
                f' &nbsp; <a href="/dashboard/page-agent?persona={quote(p.name)}" '
                f'style="color:#58a6ff">launch</a></td></tr>'
            )

        rows = "".join(_persona_row(p) for p in personas) or (
            "<tr><td colspan=6>no personas</td></tr>"
        )
        body = f"""\
<h2>Personas</h2>
<table>
<thead><tr>
  <th>name</th><th>LLM</th><th>TTS</th><th>ASR</th><th>memory</th><th></th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<h3 style="margin-top:2rem">New persona</h3>
<p style="color:#6e7681;font-size:0.8rem;margin:0 0 0.5rem">
  Creates a copy of <code>hub-default</code> that you then configure.
</p>
<div id="new-persona-result" role="status" aria-live="polite"></div>
<form hx-post="/dashboard/personas"
      hx-target="#new-persona-result" hx-swap="innerHTML">
  <label>Name</label>
  <input type="text" name="name" required placeholder="e.g. toaster3000" style="width:300px">
  <button type="submit">Create</button>
</form>"""
        return HTMLResponse(_render_page(request, body))

    @router.post("/dashboard/personas", response_class=HTMLResponse)
    async def persona_create(name: str = Form(...)) -> HTMLResponse:
        base = await store.get_persona_by_name("hub-default")
        persona = await store.create_persona(
            name,
            system_prompt=base.system_prompt if base else "",
            llm_provider=base.llm_provider if base else "openai",
            llm_model=base.llm_model if base else None,
            tts_provider=base.tts_provider if base else "edge",
            tts_voice=base.tts_voice if base else None,
            asr_provider=base.asr_provider if base else "funasr_onnx",
            tts_model=base.tts_model if base else None,
        )
        if persona is None:
            return HTMLResponse(f"<p style=\"color:#f85149\">Name '{name}' already taken.</p>")
        if base is not None:
            # create_persona does not carry these; copy them so the new persona
            # is a faithful starting point.
            await store.update_persona(
                name,
                server_skills=base.server_skills or "",
                mcp_tools_allowlist=base.mcp_tools_allowlist or "",
                linked_agents=base.linked_agents or "",
                memory_window=base.memory_window,
                conversation_idle_minutes=base.conversation_idle_minutes,
                auto_title=base.auto_title,
                summarize_conversations=base.summarize_conversations,
                remember_conversations=base.remember_conversations,
            )
        return HTMLResponse(
            f'<p class="msg">✓ Created. <a href="/dashboard/personas/{persona.name}" '
            f'style="color:#58a6ff">Configure {persona.name} →</a></p>'
        )

    @router.get("/dashboard/personas/{name}", response_class=HTMLResponse)
    async def persona_edit_page(name: str, request: Request) -> HTMLResponse:
        import agent_hub.skills as _skills

        persona = await store.get_persona_by_name(name)
        if persona is None:
            return HTMLResponse(_render_page(request, "<p>Persona not found.</p>"))

        enabled = persona.server_skills_list  # None = all enabled

        def _select(field: str, choices: list[str], current: str, attrs: str = "") -> str:
            merged = list(dict.fromkeys([*choices, current]))
            opts = "".join(
                f'<option value="{html.escape(v)}"'
                f"{' selected' if v == current else ''}>{html.escape(v)}</option>"
                for v in merged
                if v
            )
            return f'<select name="{field}"{attrs}>{opts}</select>'

        llm_select = _select("llm_provider", ["openai"], persona.llm_provider)
        # Changing the voice system swaps the voice list to that system's voices.
        tts_select = _select(
            "tts_provider",
            list(persona_options.TTS_PROVIDERS),
            persona.tts_provider,
            ' hx-get="/dashboard/persona-voices" hx-trigger="change"'
            ' hx-target="#tts-voices" hx-swap="outerHTML"',
        )
        asr_select = _select("asr_provider", persona_options.asr_providers(), persona.asr_provider)
        voice_datalist = _voice_datalist(persona.tts_provider, persona.tts_voice or "")
        tts_model_datalist = _tts_model_datalist(persona.tts_provider, persona.tts_model or "")
        # Models the hub can actually use, offered in-form so nobody has to
        # copy ids from the Models page. Same gates as the picker.
        catalogue = await _fetch_openrouter_models(api_key)
        free_view = _free_for(request)
        usable_models = [m for m in catalogue if m["tools"] and (not free_view or m["free"])]
        effective_model = persona.llm_model or default_model
        replacement = model_field.paid_version(effective_model, catalogue)
        replacement_hint = (
            f" Its paid version <code>{html.escape(replacement)}</code> is still listed"
            + (" (paid models are off for you)." if free_view else ", under Paid.")
            if replacement
            else ""
        )
        model_warning = (
            '<p class="msg" style="color:#f85149">⚠ '
            f"<code>{html.escape(effective_model)}</code> is not in OpenRouter's model list any "
            "more. It was probably removed, and every turn on it will fail. Pick another "
            f"model and test it.{replacement_hint}</p>"
            if catalogue
            and effective_model
            and _uses_openrouter(config, persona.llm_provider)
            and all(m["id"] != effective_model for m in catalogue)
            else ""
        )
        model_input = (
            model_field.model_select(persona.llm_model or "", usable_models, default_model)
            if usable_models and _uses_openrouter(config, persona.llm_provider)
            else model_field.model_text_input(persona.llm_model or "", usable_models)
        )
        preset_opts = "".join(
            f'<option value="{html.escape(k)}">{html.escape(k)}</option>'
            for k in persona_options.PROMPT_PRESETS
        )
        skill_rows: list[str] = []
        for _d in _skills.get_definitions():
            _sn = _d["function"]["name"]
            _sd = _d["function"].get("description", "")
            _ck = " checked" if enabled is None or _sn in enabled else ""
            skill_rows.append(
                '<label style="display:flex;gap:0.5rem;align-items:flex-start;'
                'margin-top:0.5rem">'
                f'<input type="checkbox" name="server_skills" value="{html.escape(_sn)}"'
                f'{_ck} style="margin-top:0.2rem;width:auto">'
                f"<span><strong>{html.escape(_sn)}</strong>"
                f'<span style="color:#6e7681"> — {html.escape(_sd)}</span>'
                "</span></label>"
            )
        skill_boxes = (
            "".join(skill_rows) or '<p style="color:#6e7681">No server skills installed.</p>'
        )

        allowed_tools = persona.mcp_tools_allowlist_list  # None = safe defaults
        tools_val = ", ".join(allowed_tools) if allowed_tools is not None else ""

        linked_now = set(persona.linked_agents_list)
        connected_now = set(mcp_bridge.connected_agent_ids())
        linked_ids = list(dict.fromkeys([*connected_now, *persona.linked_agents_list]))
        linked_rows: list[str] = []
        for _aid in linked_ids:
            _n = len(mcp_bridge.list_page_tool_definitions(_aid))
            _off = "" if _aid in connected_now else ", offline"
            linked_rows.append(
                '<label style="display:flex;gap:0.5rem;align-items:center;'
                'margin-top:0.5rem">'
                f'<input type="checkbox" name="linked_agents" value="{html.escape(_aid)}"'
                f'{" checked" if _aid in linked_now else ""} style="width:auto">'
                f"<span><strong>{html.escape(_aid)}</strong>"
                f'<span style="color:#6e7681"> — {_n} tools{_off}</span></span></label>'
            )
        linked_boxes = (
            "".join(linked_rows)
            or '<p style="color:#6e7681">No other agents connected right now.</p>'
        )

        prompt_val = html.escape(persona.system_prompt or "")
        free_hint = model_field.access_note(free_only, _may_choose_paid(request))
        tts_voice_val = html.escape(persona.tts_voice or "")
        tts_model_val = html.escape(persona.tts_model or "")
        tools_val_esc = html.escape(tools_val)
        transcription_checked = " checked" if persona.transcription else ""
        used_by = [
            row
            for row in await store.list_agents_with_personas()
            if row[1] is not None and row[1].id == persona.id
        ]
        used_by_html = _persona_used_by(used_by, _viewer_subject(request))

        body = f"""\
<p><a href="/dashboard/personas" style="color:#58a6ff">← personas</a></p>
<h2>Edit persona: {name}</h2>
<p><a href="/dashboard/page-agent?persona={quote(name)}" style="color:#58a6ff">
  ▶ Launch as page agent</a> &nbsp;— talk to this persona in the browser, no hardware.</p>
{used_by_html}
<div id="save-result" role="status" aria-live="polite"></div>
<form hx-post="/dashboard/personas/{name}"
      hx-target="#save-result" hx-swap="innerHTML">

  <div class="form-section">
    <h3>Mode</h3>
    <label style="display:flex;gap:0.5rem;align-items:flex-start">
      <input type="checkbox" name="transcription" value="1"{transcription_checked}
        style="margin-top:0.2rem;width:auto">
      <span><strong>Transcription mode</strong>
      <span style="color:#6e7681"> — the device streams audio continuously and
      the hub logs each utterance (ASR only, no LLM reply, no speech). Photos are
      captioned into the same transcript. The prompt, TTS, skills and linked
      agents below are ignored in this mode.</span></span>
    </label>
  </div>

  <div class="form-section" data-assistant-only>
    <h3>Prompt</h3>
    <label>Starter (fills the box below — then edit it)</label>
    <select name="preset" hx-get="/dashboard/personas/{quote(name)}/_preset"
            hx-target="#system-prompt" hx-swap="outerHTML">
      <option value="">— keep current —</option>
      {preset_opts}
    </select>
    <label>System prompt</label>
    <textarea id="system-prompt" name="system_prompt" rows="6">{prompt_val}</textarea>
  </div>

  <div class="form-section">
    <h3>Providers</h3>
    <div class="field-row" data-assistant-only>
      <div><label>LLM provider</label>{llm_select}</div>
      <div><label for="llm-model">LLM model{free_hint}</label>
        {model_input}
        <button type="button" style="background:#1a4a6e"
          hx-post="/dashboard/models/test" hx-include="[name=llm_model],[name=llm_provider]"
          hx-target="#persona-model-test" hx-swap="innerHTML"
          title="Ask the model a few voice-style questions, including one that needs a tool"
          >Test model</button></div>
    </div>
    <div id="persona-model-test" role="status" aria-live="polite" data-assistant-only>
      {model_warning}</div>
    <div class="field-row" data-assistant-only>
      <div><label>TTS system</label>{tts_select}</div>
      <div><label>TTS voice (blank = system default)</label>
        <input type="text" name="tts_voice" value="{tts_voice_val}"
          list="tts-voices" style="width:300px" placeholder="blank = system default">
        {voice_datalist}</div>
    </div>
    <div class="field-row" data-assistant-only>
      <div><label>TTS model (blank = hub default; Edge has none)</label>
        <input type="text" name="tts_model" value="{tts_model_val}"
          list="tts-models" style="width:300px" placeholder="blank = hub default">
        {tts_model_datalist}</div>
      <div></div>
    </div>
    <div class="field-row">
      <div><label>ASR system</label>{asr_select}</div>
      <div></div>
    </div>
  </div>

  <div class="form-section" data-assistant-only>
    <h3>Skills</h3>
    <p style="color:#6e7681;font-size:0.8rem;margin:0">
      Server-side tools this persona can call. All checked = all enabled.
    </p>
    {skill_boxes}
  </div>

  <div class="form-section">
    <h3>Device tools</h3>
    <label>Device MCP tool allowlist (comma-separated)
      <span style="color:#6e7681"> — blank = safe defaults (camera/photo/status
      etc.; risky reboot/firmware/Wi-Fi/filesystem/exec tools excluded). List
      tools explicitly to run an admin/custom set, including risky ones.</span>
    </label>
    <input type="text" name="mcp_tools_allowlist" value="{tools_val_esc}"
      style="width:100%">
  </div>

  <div class="form-section" data-assistant-only>
    <h3>Linked agents</h3>
    <p style="color:#6e7681;font-size:0.8rem;margin:0">
      Borrow the non-destructive MCP tools of other connected agents (a robot,
      another page). Borrowed tool names are prefixed with the agent id.
    </p>
    {linked_boxes}
  </div>

  <div class="form-section">
    <h3>Conversations &amp; memory</h3>
    <p class="doc-muted">Defaults for every agent using this persona; an agent's page can
    override any of them.</p>
    <div class="field-row">
      <div><label>New conversation after this many minutes of silence</label>
        <input type="number" name="conversation_idle_minutes"
          value="{persona.conversation_idle_minutes}" min="1" max="1440"></div>
      <div data-assistant-only><label>Recent turns the model sees</label>
        <input type="number" name="memory_window" value="{persona.memory_window}"
          min="1" max="200"></div>
    </div>
    <label style="display:flex;gap:0.5rem;align-items:center">
      <input type="checkbox" name="auto_title" value="1"{" checked" if persona.auto_title else ""}>
      Name finished conversations (one call to this persona's model per conversation)</label>
    <label style="display:flex;gap:0.5rem;align-items:center">
      <input type="checkbox" name="summarize_conversations" value="1"{
            " checked" if persona.summarize_conversations else ""
        }> Summarize finished conversations (same call)</label>
    <div data-assistant-only><label>Remember this many earlier conversations
      (their summaries go into a new one; 0 = off)</label>
      <input type="number" name="remember_conversations"
        value="{persona.remember_conversations}" min="0" max="20"></div>
  </div>

  <button type="submit">Save</button>
</form>
<script>
// Transcription mode ignores the prompt, LLM, TTS, skills, linked agents and
// memory; dim them so the form shows what is in play instead of describing it.
(function () {{
  const box = document.querySelector('input[name="transcription"]');
  const dim = () => document.querySelectorAll("[data-assistant-only]").forEach((el) => {{
    el.style.opacity = box.checked ? "0.45" : "";
    el.title = box.checked ? "ignored in transcription mode" : "";
  }});
  box.addEventListener("change", dim);
  dim();
  // Remembering needs summaries.
  const summarize = document.querySelector('input[name="summarize_conversations"]');
  const remember = document.querySelector('input[name="remember_conversations"]');
  const gate = () => {{ remember.disabled = !summarize.checked; }};
  summarize.addEventListener("change", gate);
  gate();
}})();
</script>"""
        return HTMLResponse(_render_page(request, body))

    @router.get("/dashboard/persona-voices", response_class=HTMLResponse)
    async def persona_voices(tts_provider: str = "", current: str = "") -> HTMLResponse:
        """The voice datalist for one TTS system (swapped in when it changes).

        The model datalist rides along out of band, since models also depend
        on the system.
        """
        return HTMLResponse(
            _voice_datalist(tts_provider, current) + _tts_model_datalist(tts_provider, "", oob=True)
        )

    @router.get("/dashboard/personas/{name}/_preset", response_class=HTMLResponse)
    async def persona_preset(name: str, preset: str = "") -> HTMLResponse:
        """Return a replacement system-prompt textarea filled with a preset.

        An unknown/blank preset restores the persona's saved prompt, so
        choosing "— keep current —" is non-destructive.
        """
        text = persona_options.PROMPT_PRESETS.get(preset)
        if text is None:
            existing = await store.get_persona_by_name(name)
            text = (existing.system_prompt if existing else "") or ""
        return HTMLResponse(
            f'<textarea id="system-prompt" name="system_prompt" rows="6">'
            f"{html.escape(text)}</textarea>"
        )

    @router.post("/dashboard/personas/{name}", response_class=HTMLResponse)
    async def persona_save(
        request: Request,
        name: str,
        system_prompt: str = Form(default=""),
        llm_provider: str = Form(default=""),
        llm_model: str = Form(default=""),
        tts_provider: str = Form(default=""),
        tts_voice: str = Form(default=""),
        tts_model: str = Form(default=""),
        asr_provider: str = Form(default=""),
        mcp_tools_allowlist: str = Form(default=""),
        memory_window: int = Form(default=20),
        transcription: str = Form(default=""),
        conversation_idle_minutes: int = Form(default=30),
        auto_title: str = Form(default=""),
        summarize_conversations: str = Form(default=""),
        remember_conversations: int | None = Form(default=None),
    ) -> HTMLResponse:
        import json as _json

        import agent_hub.skills as _skills

        # Skills come in as repeated checkbox fields; Form(list) hits a ruff
        # B008 edge case, so read them straight off the parsed form.
        form = await request.form()
        all_skill_names = {d["function"]["name"] for d in _skills.get_definitions()}
        selected = {str(s).strip() for s in form.getlist("server_skills") if str(s).strip()}
        # "" tells update_persona to store NULL (= all enabled); a JSON list
        # pins an explicit subset, and [] disables every skill.
        skills_arg = "" if selected >= all_skill_names else _json.dumps(sorted(selected))

        tool_parts = [s.strip() for s in mcp_tools_allowlist.split(",") if s.strip()]
        # "" clears the allowlist back to the safe defaults; a JSON list pins it.
        tools_arg = _json.dumps(tool_parts) if tool_parts else ""

        linked = sorted({str(a).strip() for a in form.getlist("linked_agents") if str(a).strip()})
        linked_arg = _json.dumps(linked) if linked else ""

        refusal = await _reject_model(llm_model.strip(), request)
        if refusal:
            return HTMLResponse(f'<p style="color:#f85149">{html.escape(refusal)}</p>', 403)
        if tts_provider == "openrouter" and _free_for(request):
            current = await store.get_persona_by_name(name)
            switching = current is None or current.tts_provider != "openrouter"
        else:
            switching = False
        if switching:
            # Cloud speech is billed per character; free mode means no new bills.
            return HTMLResponse(
                '<p style="color:#f85149">Free models only: the OpenRouter voice is paid. '
                "An admin can allow paid models for you on the Operators page.</p>",
                403,
            )
        # A system without model choices (Edge) keeps no model, rather than
        # refusing a save that switched to it with the old model still typed.
        # A blank system field means "unchanged", so judge the model against
        # the persona's current system.
        model_system = tts_provider
        if not model_system:
            current_persona = await store.get_persona_by_name(name)
            model_system = current_persona.tts_provider if current_persona else ""
        tts_model = tts_model.strip() if persona_options.tts_models_for(model_system) else ""
        bad_voice = persona_options.voice_problem(
            tts_provider, tts_voice
        ) or persona_options.tts_model_problem(model_system, tts_model)
        if bad_voice:
            return HTMLResponse(f'<p style="color:#f85149">{html.escape(bad_voice)}</p>', 400)

        ok = await store.update_persona(
            name,
            system_prompt=system_prompt,
            llm_provider=llm_provider or None,
            llm_model=llm_model,
            tts_provider=tts_provider or None,
            tts_voice=tts_voice,
            tts_model=tts_model,
            asr_provider=asr_provider or None,
            server_skills=skills_arg,
            mcp_tools_allowlist=tools_arg,
            linked_agents=linked_arg,
            memory_window=max(1, memory_window),
            transcription=bool(transcription.strip()),
            conversation_idle_minutes=min(1440, max(1, conversation_idle_minutes)),
            auto_title=bool(auto_title.strip()),
            summarize_conversations=bool(summarize_conversations.strip()),
            # A disabled field isn't submitted (summaries off): keep the number.
            remember_conversations=(
                None if remember_conversations is None else min(20, max(0, remember_conversations))
            ),
        )
        if ok:
            logger.info(f"Persona '{name}' updated via dashboard")
            return HTMLResponse('<p class="msg">✓ Saved.</p>')
        return HTMLResponse(f"<p style=\"color:#f85149\">Persona '{name}' not found.</p>")

    # ── Models ────────────────────────────────────────────────────────────────

    @router.get("/dashboard/models", response_class=HTMLResponse)
    async def models_page(request: Request) -> HTMLResponse:
        personas = await store.list_personas()
        current = next(
            (p.llm_model for p in personas if p.name == "hub-default"), None
        ) or config.get("llm", {}).get("openai", {}).get("model", "")
        free_view = _free_for(request)
        free_attrs = " checked disabled" if free_view else ""
        free_note = (
            '<p class="doc-muted" style="margin:.3rem 0 0">Free mode is on '
            "(<code>llm.free_only</code>): only free models are listed and paid ids "
            "are refused. An admin can allow paid models for you on the Operators page.</p>"
            if free_view
            else ""
        )
        body = f"""\
<h2>Model Picker</h2>
<p class="doc-muted">These buttons set the <strong>hub-default</strong> persona's model. To change
another persona's model, edit it on the <a href="/dashboard/personas">Personas</a> page.</p>
<p>Hub default: <strong id="current-model">{html.escape(current or "not set")}</strong>
  <button type="button" style="background:#1a4a6e" hx-post="/dashboard/models/test"
    hx-vals='{{"model_id": ""}}' hx-target="#model-test" hx-swap="innerHTML"
    >Test current model</button></p>
<p class="doc-muted">A test asks the model a greeting and, twice, a question that needs a
tool, the way a voice turn does. Tool use is required: a model that answers without the
tool is not usable here.</p>
<div id="model-test" role="status" aria-live="polite"></div>
<div class="controls">
  <input id="search" type="text" placeholder="Search models..."
    hx-get="/dashboard/models/list"
    hx-trigger="input changed delay:300ms"
    hx-target="#model-list"
    hx-include="#multimodal-only,#free-only"
    name="search">
  <label>
    <input id="multimodal-only" type="checkbox" name="multimodal" value="1"
      hx-get="/dashboard/models/list"
      hx-trigger="change"
      hx-target="#model-list"
      hx-include="#search,#free-only">
    Multimodal only
  </label>
  <label>
    <input id="free-only" type="checkbox" name="free" value="1"{free_attrs}
      hx-get="/dashboard/models/list"
      hx-trigger="change"
      hx-target="#model-list"
      hx-include="#search,#multimodal-only">
    Free only
  </label>
</div>
{free_note}
<div id="model-list"
  hx-get="/dashboard/models/list"
  hx-trigger="load"
  hx-include="#search,#multimodal-only,#free-only">
  Loading…
</div>
"""
        return HTMLResponse(_render_page(request, body))

    @router.get("/dashboard/models/list", response_class=HTMLResponse)
    async def models_list(
        request: Request,
        search: str = "",
        multimodal: str = "",
        free: str = "",
    ) -> HTMLResponse:
        models = await _fetch_openrouter_models(api_key)
        personas = await store.list_personas()
        current = next(
            (p.llm_model for p in personas if p.name == "hub-default"), None
        ) or config.get("llm", {}).get("openai", {}).get("model", "")

        only_multi = bool(multimodal)
        only_free = bool(free) or _free_for(request)
        q = search.lower()

        # Only models that can call tools are offered: every persona on this
        # hub depends on function calling, so a model without it is a trap.
        usable = [m for m in models if m["tools"]]
        hidden = len(models) - len(usable)
        filtered = [
            m
            for m in usable
            if (not q or q in m["id"].lower() or q in m["name"].lower())
            and (not only_multi or m["multimodal"])
            and (not only_free or m["free"])
        ]
        hidden_note = (
            f'<p class="doc-muted" style="margin:0 0 .4rem">{hidden} models without tool '
            "calling are hidden.</p>"
            if hidden
            else ""
        )

        if not filtered:
            return HTMLResponse(hidden_note + "<p>No models match.</p>")

        rows = []
        for m in filtered:
            selected = m["id"] == current
            badge_multi = '<span class="badge badge-multi">vision</span>' if m["multimodal"] else ""
            badge_free = '<span class="badge badge-free">free</span>' if m["free"] else ""
            btn_class = "selected" if selected else ""
            rows.append(f"""\
<tr>
  <td>{m["id"]}{badge_multi}{badge_free}</td>
  <td>{m["name"]}</td>
  <td>{m["context_k"]}k</td>
  <td>{m["price_in"]}</td>
  <td>
    <button type="button" style="background:#1a4a6e"
      hx-post="/dashboard/models/test"
      hx-vals='{{"model_id":"{m["id"]}"}}'
      hx-target="#model-test"
      hx-swap="innerHTML"
      hx-on::before-request="document.getElementById('model-test').scrollIntoView()"
    >test</button>
    <button class="{btn_class}"
      hx-post="/dashboard/models/select"
      hx-vals='{{"model_id":"{m["id"]}","persona":"hub-default"}}'
      hx-target="#model-list"
      hx-swap="none"
      hx-on::after-request="document.getElementById('current-model').innerText='{m["id"]}'"
      title="Make this the model of the hub-default persona; other personas keep theirs"
    >{"✓ hub default" if selected else "set hub default"}</button>
  </td>
</tr>""")

        table = f"""\
<table>
<thead><tr>
  <th>model id</th><th>name</th><th>ctx</th><th>$/M in</th><th></th>
</tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>"""
        return HTMLResponse(hidden_note + table)

    @router.post("/dashboard/models/test", response_class=HTMLResponse)
    async def models_test(
        request: Request,
        model_id: str = Form(default=""),
        llm_model: str = Form(default=""),
        llm_provider: str = Form(default="openai"),
    ) -> HTMLResponse:
        """Check a model the way a voice turn would use it (blank = hub default).

        The picker sends ``model_id``; the persona form sends its own
        ``llm_model`` field, so both are accepted.
        """
        provider = llm_provider.strip() or "openai"
        model = model_id.strip() or llm_model.strip() or default_model
        if not model:
            return HTMLResponse('<p class="msg">No model set to test.</p>', status_code=400)
        # Free mode is a cost control, so a paid model is not called even to test it.
        # A model the catalogue no longer lists is not refused: calling it costs
        # nothing, and "it's gone" is exactly what the test should report.
        if (
            _free_for(request)
            and _uses_openrouter(config, provider)
            and await _is_known_paid(model, api_key)
        ):
            return HTMLResponse(
                f'<p class="msg" style="color:#f85149">Free mode is on: not testing the paid '
                f"model <code>{html.escape(model)}</code>.</p>",
                status_code=403,
            )
        try:
            llm = get_llm(provider, config, model_override=model)
        except Exception as exc:  # noqa: BLE001 - a bad provider is a result to show
            return HTMLResponse(
                f'<p class="msg" style="color:#f85149">Could not set up {html.escape(provider)}: '
                f"{html.escape(str(exc))}</p>",
                status_code=400,
            )
        result = await check_model(llm, model)
        logger.info(f"Model check {model!r}: {result.verdict} ({result.reason})")
        return HTMLResponse(_render_model_check(result))

    @router.post("/dashboard/models/select", response_class=HTMLResponse)
    async def models_select(
        request: Request,
        model_id: str = Form(...),
        persona: str = Form(default="hub-default"),
    ) -> HTMLResponse:
        refusal = await _reject_model(model_id, request)
        if refusal:
            return HTMLResponse(f'<p style="color:#f85149">{html.escape(refusal)}</p>', 403)
        ok = await store.update_persona_model(persona, model_id)
        if ok:
            logger.info(f"Persona '{persona}' model set to {model_id!r}")
            return HTMLResponse("")
        return HTMLResponse(f"<p>Persona '{persona}' not found.</p>", status_code=404)

    return router


# ── Helpers ───────────────────────────────────────────────────────────────────


def _render_operator(identity: OperatorIdentity | None, role: str) -> str:
    if identity is None:
        return (
            '<div class="operator"><span>Local session</span>'
            '<span class="operator-role">Admin</span></div>'
        )
    return (
        '<div class="operator">'
        f'<span class="operator-email">{html.escape(identity.email)}</span>'
        f'<span class="operator-role">{html.escape(role.title())}</span>'
        '<a href="/cdn-cgi/access/logout">Sign out</a>'
        "</div>"
    )


def _render_operator_row(operator: Any) -> str:
    """Render one operator-management table row."""
    options = "".join(
        f'<option value="{role.value}"'
        f"{' selected' if operator.role == role.value else ''}>{role.value.title()}</option>"
        for role in OperatorRole
    )
    checked = " checked" if operator.enabled else ""
    is_admin = operator.role == OperatorRole.ADMIN.value
    # Admins always may; the box shows that rather than a setting to change.
    paid = " checked disabled" if is_admin else (" checked" if operator.paid_models else "")
    last_seen = fmt_ts(operator.last_seen_at)
    subject = quote(operator.subject, safe="")
    return f"""\
<tr><td>{html.escape(operator.email)}</td>
<td><form class="controls" style="margin:0"
  hx-post="/dashboard/operators/{subject}"
  hx-target="#operator-result" hx-swap="innerHTML">
  <select name="role">{options}</select>
  <label style="margin:0"><input type="checkbox" name="enabled" value="1"{checked}> enabled</label>
  <label style="margin:0" title="In free mode, whether this person's agents may run paid models">
    <input type="checkbox" name="paid_models" value="1"{paid}> paid models</label>
  <span class="doc-muted">last seen {last_seen}</span>
  <button type="submit">Save</button>
</form></td></tr>"""


def _render_spend_panel(totals: dict[str, Any]) -> str:
    """Render the LLM spend summary shown above the agent table."""
    today = totals["today"]
    total = totals["total"]
    limits = totals["limits"]

    def _cap(spent: float, limit: float, fraction: float | None) -> str:
        if not limit:
            return f"${spent:.4f} <span class='doc-muted'>(no cap)</span>"
        pct = 0.0 if fraction is None else fraction * 100
        # Colour tracks the same thresholds the server enforces, so the
        # dashboard and the guard never disagree about what state we're in.
        if pct >= 100:
            cls = "spend-over"
        elif pct >= limits["warn_at"] * 100:
            cls = "spend-warn"
        else:
            cls = "spend-ok"
        return f"<span class='{cls}'>${spent:.4f} / ${limit:.2f} ({pct:.0f}%)</span>"

    estimated = int(today["estimated_calls"])
    estimate_note = (
        f" <span class='doc-muted'>· {estimated} estimated from the local price table</span>"
        if estimated
        else ""
    )
    blocked = (
        "<p class='spend-over'>LLM calls are blocked — a spend cap has been reached.</p>"
        if totals["blocked"]
        else ""
    )
    return f"""\
<section class="form-section">
<h3>LLM spend</h3>
{blocked}
<p>today: {_cap(float(today["cost_usd"]), limits["daily_usd"], totals["utilisation"]["daily"])}
 <span class="doc-muted">· {today["calls"]} calls ·
 {int(today["prompt_tokens"]) + int(today["completion_tokens"])} tokens</span>{estimate_note}</p>
<p>total: {_cap(float(total["cost_usd"]), limits["total_usd"], totals["utilisation"]["total"])}
 <span class="doc-muted">· {total["calls"]} calls</span></p>
</section>"""


def _filter_query(owner: str = "", mine: bool = False, view: str = "cards") -> str:
    """The query string that selects one fleet filter ("" for everyone)."""
    params = []
    if mine:
        params.append("mine=1")
    elif owner:
        params.append(f"owner={quote(owner, safe='')}")
    if view == "diagnostics":
        params.append("view=diagnostics")
    return f"?{'&'.join(params)}" if params else ""


def _owner_filter(
    owners: list[str],
    current: str = "",
    *,
    has_identity: bool = False,
    mine: bool = False,
    view: str = "cards",
) -> str:
    """Chips that filter the fleet table by whose agent it is.

    The bar is rendered once and never replaced by a poll. A chip swaps only
    the table, whose own poll then asks for the same filter, and pushes the
    filter into the page URL so reload and back keep the choice too.
    """
    if not owners and not has_identity:
        return ""
    active = _filter_query(current, mine, view)
    target = "#agent-table" if view == "diagnostics" else "#agent-cards"
    page = "/dashboard/health" if view == "diagnostics" else "/dashboard/"

    def chip(owner: str, mine: bool, label: str) -> str:
        query = _filter_query(owner, mine, view)
        pressed = "true" if query == active else "false"
        return (
            f'<button class="owner-chip" aria-pressed="{pressed}" '
            f'hx-get="/dashboard/agents{html.escape(query)}" hx-target="{target}" '
            f'hx-swap="outerHTML" hx-push-url="{html.escape(page + _filter_query(owner, mine))}" '
            # Mark the choice straight away; the bar itself is never re-rendered.
            "hx-on::before-request=\"this.parentElement.querySelectorAll('.owner-chip')"
            ".forEach((b) => b.setAttribute('aria-pressed', b === this))\">"
            f"{html.escape(label)}</button>"
        )

    chips = chip("", False, "everyone")
    if has_identity:
        # Keyed on the verified claim, so "mine" cannot be spoofed by a robot
        # registering with someone else's owner label.
        chips += chip("", True, "mine")
    chips += "".join(chip(o, False, o) for o in owners)
    return f'<div class="owner-filter">Show: {chips}</div>'


def _agent_card_view(cards: str, *, owner: str = "", mine: bool = False) -> str:
    query = html.escape(_filter_query(owner, mine, "cards"))
    return (
        f'<div id="agent-cards" hx-get="/dashboard/agents{query}" '
        f'hx-trigger="every 5s" hx-swap="outerHTML">{cards}</div>'
    )


def _agent_table(
    rows: str,
    *,
    poll: bool = True,
    owner: str = "",
    mine: bool = False,
    view: str = "diagnostics",
) -> str:
    query = html.escape(_filter_query(owner, mine, view))
    poll_attributes = (
        f' hx-get="/dashboard/agents{query}" hx-trigger="every 5s" hx-swap="outerHTML"'
        if poll
        else ""
    )
    return f"""\
<div id="agent-table"{poll_attributes}>
<table>
<thead><tr>
  <th>device</th><th>health · activity</th><th>persona / model</th>
  <th>owner</th><th>tools</th><th>latency (last / avg)</th><th>spend</th>
  <th>ip</th><th>fw</th><th>last seen</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</div>"""


def _project_docs() -> str:
    """Render dashboard-facing project documentation."""
    return """\
<div class="doc-page">
<h2>Project Documentation</h2>
<p class="doc-muted">
  agent-hub is the control plane for voice-enabled ESP32 devices and other
  local agents on the same network.
</p>

<section>
  <h3>What It Is</h3>
  <p>
    agent-hub turns small xiaozhi-compatible devices into managed voice agents.
    A device checks in, receives a WebSocket voice-session URL, and immediately
    runs with an assigned persona. The server handles speech recognition,
    LLM calls, text-to-speech, tool routing, registry state, and the dashboard.
  </p>
  <p>
    This is a clean Python implementation of the device-facing pieces needed by
    xiaozhi-esp32 firmware, shaped for homelabs, classrooms, and makerspaces
    instead of a multi-service cloud stack.
  </p>
</section>

<section>
  <h3>Architecture</h3>
  <div class="doc-flow">ESP32 device
  -> /checkin/ or /xiaozhi/ota/
  -> registry + hub-default persona
  -> /xiaozhi/v1/ WebSocket
  -> ASR -> LLM + tools -> TTS
  -> audio response back to device</div>
  <div class="doc-grid">
    <div class="doc-card">
      <h3>Check-In</h3>
      <p>
        The firmware posts its device ID, client ID, version, and board details.
        agent-hub registers first-contact devices and returns the WebSocket URL,
        time data, and firmware-compatible response fields.
      </p>
    </div>
    <div class="doc-card">
      <h3>Voice Session</h3>
      <p>
        The WebSocket accepts xiaozhi hello messages and Opus audio frames, then
        streams a full ASR, LLM, tool, and TTS turn back to the device.
      </p>
    </div>
    <div class="doc-card">
      <h3>Registry</h3>
      <p>
        SQLite stores devices, personas, provider choices, status, transcript
        history, and per-device assignments so the hub can manage many agents
        without a separate admin backend.
      </p>
    </div>
    <div class="doc-card">
      <h3>Dashboard</h3>
      <p>
        The HTMX dashboard shows live device status, MCP readiness, latency,
        tools, personas, model selection, and conversation history from one
        server-rendered UI.
      </p>
    </div>
  </div>
</section>

<section>
  <h3>These Are Agents</h3>
  <p>
    In this hub, an agent is any network participant that can converse, expose
    tools, or be managed through the registry. A xiaozhi ESP32 device is an
    agent with a microphone, speaker, optional camera, and device-side MCP
    tools. A persona is the behavior profile assigned to that agent: prompt,
    LLM model, voice, ASR provider, server skills, tool permissions, and memory
    window.
  </p>
</section>

<section>
  <h3>xiaozhi-esp32 MCP Compatibility</h3>
  <p>
    agent-hub preserves the firmware-facing endpoints and wire protocol used by
    xiaozhi-esp32: the `/xiaozhi/ota/` check-in alias, the `/xiaozhi/v1/`
    WebSocket session, hello messages, Opus audio frames, and device-side
    MCP-over-WebSocket JSON-RPC framing. Devices can advertise tools such as
    volume, screen, status, and camera actions; the hub exposes those tools to
    the LLM under policy control.
  </p>
</section>

<section>
  <h3>Agent Hub Value Adds</h3>
  <ul>
    <li><strong>No activation gate:</strong> first-contact devices auto-bind to
      `hub-default` and work immediately.</li>
    <li><strong>Per-device personas:</strong> each device can use different
      prompts, voices, models, skills, and tool allowlists.</li>
    <li><strong>Unified registry:</strong> one place to see xiaozhi devices,
      voice agents, and future MCP or AG2 agents.</li>
    <li><strong>MCP bridge:</strong> server-side skills and device-side MCP tools
      can be routed through the same voice session.</li>
    <li><strong>Provider flexibility:</strong> OpenAI-compatible LLMs, local ASR,
      and multiple TTS providers can be swapped per persona.</li>
    <li><strong>Operational dashboard:</strong> live status, MCP readiness,
      latency, transcripts, model picker, and device actions are built in.</li>
    <li><strong>Classroom and homelab fit:</strong> single-container deployment,
      SQLite storage, no MySQL, no Redis, no Java manager service, and no
      frontend build step.</li>
    <li><strong>Protocol safety:</strong> backward-compatible check-in JSON is
      tested so field devices keep working when the hub evolves.</li>
  </ul>
</section>
</div>"""


async def _render_agent_rows(
    store: RegistryStore,
    heartbeat_timeout_seconds: int,
    owner: str = "",
    owner_subject: str = "",
    viewer_subject: str = "",
) -> str:
    try:
        rows_data = await store.list_agents_with_personas()
        if owner_subject:
            rows_data = [(a, p) for a, p in rows_data if a.owner_subject == owner_subject]
        elif owner:
            rows_data = [(a, p) for a, p in rows_data if (a.owner or "") == owner]
        spend_by_device = await store.llm_spend_by_device()
    except Exception as exc:
        logger.error(f"Dashboard agent query failed: {exc}")
        return "<tr><td colspan=10>error loading agents</td></tr>"

    return _render_grouped_rows(
        rows_data, heartbeat_timeout_seconds, spend_by_device, viewer_subject=viewer_subject
    )


async def _render_agent_cards(
    store: RegistryStore,
    heartbeat_timeout_seconds: int,
    owner: str = "",
    owner_subject: str = "",
    viewer_subject: str = "",
    launcher: str | None = None,
) -> str:
    """Load and render fleet cards, preserving database failures as failures."""
    try:
        rows_data = await store.list_agents_with_personas()
    except Exception as exc:
        logger.error(f"Dashboard agent query failed: {exc}")
        return '<p class="audit-failure">Could not load agents.</p>'
    rows_data = _filter_agents(rows_data, owner, owner_subject)
    return render_agent_cards(
        _group_agents(rows_data, viewer_subject), heartbeat_timeout_seconds, launcher
    )


def _filter_agents(
    rows_data: list[tuple[Agent, Persona | None]], owner: str, owner_subject: str
) -> list[tuple[Agent, Persona | None]]:
    """Apply one owner filter to fleet rows."""
    if owner_subject:
        return [
            (agent, persona) for agent, persona in rows_data if agent.owner_subject == owner_subject
        ]
    if owner:
        return [(agent, persona) for agent, persona in rows_data if (agent.owner or "") == owner]
    return rows_data


async def _render_agent_overview(
    store: RegistryStore,
    heartbeat_timeout_seconds: int,
    *,
    has_identity: bool = False,
    viewer_subject: str = "",
    owner: str = "",
    mine: bool = False,
    view: str = "cards",
    launcher: str | None = None,
) -> str:
    """Fleet health, stable controls, and the selected agent collection.

    Only the health block and agent collection refresh, each on its own 5-second
    poll. The filter bar is never replaced, so a click can't land on a button
    a refresh just threw away, and the collection poll carries the filter in its
    own URL so a refresh never quietly shows everyone again.
    """
    try:
        rows_data = await store.list_agents_with_personas()
    except Exception as exc:
        logger.error(f"Dashboard overview query failed: {exc}")
        return '<p class="audit-failure">Could not load fleet status.</p>'
    if not rows_data:
        # Nothing to filter yet: show the first-device guidance and check for
        # the first agent, then swap in the full layout once.
        query = html.escape(_filter_query(owner, mine, view))
        return (
            f'<div id="fleet-overview" hx-get="/dashboard/fleet{query}" '
            'hx-trigger="every 5s" '
            'hx-swap="outerHTML">'
            + render_fleet_overview(rows_data, heartbeat_timeout_seconds)
            + "</div>"
        )
    # "mine" means nothing without a verified identity; fall back to everyone.
    mine = mine and bool(viewer_subject)
    view = "diagnostics" if view == "diagnostics" else "cards"
    owners = sorted({a.owner for a, _p in rows_data if a.owner})
    filtered = _filter_agents(
        rows_data,
        "" if mine else owner,
        viewer_subject if mine else "",
    )
    if view == "diagnostics":
        try:
            spend_by_device = await store.llm_spend_by_device()
            rows = _render_grouped_rows(
                filtered,
                heartbeat_timeout_seconds,
                spend_by_device,
                viewer_subject=viewer_subject,
            )
        except Exception as exc:
            logger.error(f"Dashboard agent query failed: {exc}")
            rows = "<tr><td colspan=10>error loading agents</td></tr>"
        collection = _agent_table(rows, owner=owner, mine=mine, view=view)
    else:
        cards = render_agent_cards(
            _group_agents(filtered, viewer_subject), heartbeat_timeout_seconds, launcher
        )
        collection = _agent_card_view(cards, owner=owner, mine=mine)
    health = (
        _fleet_health_poll(render_fleet_overview(rows_data, heartbeat_timeout_seconds))
        if view == "diagnostics"
        else _fleet_health_poll(
            render_health_strip(rows_data, heartbeat_timeout_seconds), compact=True
        )
    )
    heading = "Diagnostics" if view == "diagnostics" else "All agents"
    return (
        '<div id="fleet-overview">'
        + health
        + f'<div class="fleet-toolbar"><div><h2>{heading}</h2>'
        + _owner_filter(
            owners,
            owner,
            has_identity=has_identity,
            mine=mine,
            view=view,
        )
        + "</div></div>"
        + collection
        + "</div>"
    )


def _fleet_health_poll(content: str, *, compact: bool = False) -> str:
    """Fleet health, refreshed on its own: the full block, or the Agents page strip."""
    url = "/dashboard/overview?compact=1" if compact else "/dashboard/overview"
    return (
        f'<div id="fleet-health" hx-get="{url}" '
        'hx-trigger="every 5s" hx-swap="outerHTML">'
        f"{content}</div>"
    )


# Boards first, then robots and other software agents, then browser pages.
_KIND_ORDER = {AgentKind.XIAOZHI.value: 0, AgentKind.PAGE.value: 2}


def _group_agents(
    rows_data: list[tuple[Agent, Persona | None]],
    viewer_subject: str = "",
) -> list[tuple[str, list[tuple[Agent, Persona | None]]]]:
    """Split the fleet into owner sections: yours, each owner's, then unowned.

    Verified owners (an Access claim) come before owner labels an agent typed
    about itself, which are marked unverified. Within a section agents are
    ordered boards, robots, pages, then by name.
    """
    mine: list[tuple[Agent, Persona | None]] = []
    verified: dict[str, list[tuple[Agent, Persona | None]]] = {}
    labelled: dict[str, list[tuple[Agent, Persona | None]]] = {}
    unowned: list[tuple[Agent, Persona | None]] = []
    for row in rows_data:
        agent = row[0]
        if viewer_subject and agent.owner_subject == viewer_subject:
            mine.append(row)
        elif agent.owner_subject:
            verified.setdefault(agent.owner or agent.owner_subject, []).append(row)
        elif agent.owner:
            labelled.setdefault(agent.owner, []).append(row)
        else:
            unowned.append(row)

    def ordered(rows: list[tuple[Agent, Persona | None]]) -> list[tuple[Agent, Persona | None]]:
        return sorted(
            rows,
            key=lambda r: (
                _KIND_ORDER.get(r[0].kind, 1),
                (r[0].label or r[0].device_id).casefold(),
            ),
        )

    groups: list[tuple[str, list[tuple[Agent, Persona | None]]]] = []
    if mine:
        groups.append(("Mine", ordered(mine)))
    groups += [(name, ordered(verified[name])) for name in sorted(verified, key=str.casefold)]
    groups += [
        (f"{name} (unverified label)", ordered(labelled[name]))
        for name in sorted(labelled, key=str.casefold)
    ]
    if unowned:
        groups.append(("Unowned", ordered(unowned)))
    return groups


def _render_grouped_rows(
    rows_data: list[tuple[Agent, Persona | None]],
    heartbeat_timeout_seconds: int,
    spend_by_device: dict[str, dict[str, Any]] | None = None,
    *,
    viewer_subject: str = "",
) -> str:
    """Agent table rows under one header row per owner section."""
    if not rows_data:
        return _render_agent_rows_data(rows_data, heartbeat_timeout_seconds, spend_by_device)
    parts: list[str] = []
    for title, rows in _group_agents(rows_data, viewer_subject):
        parts.append(
            '<tr class="owner-group"><th colspan="10" scope="colgroup">'
            f'{html.escape(title)} <span class="attention-count">{len(rows)}</span></th></tr>'
        )
        parts.append(_render_agent_rows_data(rows, heartbeat_timeout_seconds, spend_by_device))
    return "".join(parts)


def _persona_used_by(rows: list[tuple[Agent, Persona | None]], viewer_subject: str) -> str:
    """Which agents run a persona, in the same owner sections as the fleet table.

    Saving a persona changes every one of these at once, so it is shown before
    the form rather than discovered afterwards.
    """
    if not rows:
        return '<p class="doc-muted">Used by: no agents yet.</p>'
    sections = []
    for title, group in _group_agents(rows, viewer_subject):
        items = "".join(
            f'<li><a href="/dashboard/agents/{html.escape(quote(a.device_id, safe=""))}" '
            f'style="color:#58a6ff">{html.escape(a.label or a.device_id)}</a> '
            f'<span class="badge badge-kind">{html.escape(a.kind)}</span></li>'
            for a, _p in group
        )
        sections.append(
            f"<div><strong>{html.escape(title)}</strong>"
            f'<ul style="margin:0.2rem 0 0.5rem 1.2rem">{items}</ul></div>'
        )
    return (
        f'<section aria-label="Agents using this persona"><h3>Used by {len(rows)} '
        f"agent{'s' if len(rows) != 1 else ''}</h3>"
        '<p class="doc-muted">Saving changes all of them.</p>'
        f"{''.join(sections)}</section>"
    )


def _conversation_settings_panel(agent: Agent | None, persona: Persona | None) -> str:
    """This agent's conversation settings: what's in force, and an override form.

    Each field is blank (or "persona") to follow the persona, and says what that
    value is, so it's clear which settings this agent has changed.
    """
    if agent is None:
        return '<div id="conversation-settings"></div>'
    settings = effective_settings(persona, agent)
    device_id = html.escape(quote(agent.device_id, safe=""))
    persona_name = html.escape(persona.name) if persona else "no persona"

    def origin(name: str) -> str:
        if settings.sources.get(name) == "agent":
            return '<span class="badge badge-kind">this agent</span>'
        return f'<span class="doc-muted">from {persona_name}</span>'

    def number(name: str, label: str, value: int, low: int, high: int) -> str:
        own = getattr(agent, name)
        return (
            f"<tr><th>{label}</th><td>"
            f'<input type="number" name="{name}" min="{low}" max="{high}" '
            f'value="{"" if own is None else own}" placeholder="{value}" style="width:6rem"> '
            f"{origin(name)}</td></tr>"
        )

    def switch(name: str, label: str, value: bool) -> str:
        own = getattr(agent, name)
        options = "".join(
            f'<option value="{v}"{" selected" if sel else ""}>{text}</option>'
            for v, text, sel in (
                ("", f"persona ({'on' if value else 'off'})", own is None),
                ("on", "on", own is True),
                ("off", "off", own is False),
            )
        )
        return (
            f'<tr><th>{label}</th><td><select name="{name}">{options}</select> '
            f"{origin(name)}</td></tr>"
        )

    return f"""\
<div id="conversation-settings">
<h3>Conversations &amp; memory</h3>
<form hx-post="/dashboard/agents/{device_id}/conversation_settings"
      hx-target="#conversation-settings" hx-swap="outerHTML">
<table style="width:auto">
  {
        number(
            "conversation_idle_minutes",
            "New conversation after (min)",
            settings.idle_minutes,
            1,
            1440,
        )
    }
  {number("memory_window", "Recent turns the model sees", settings.memory_window, 1, 200)}
  {switch("auto_title", "Name finished conversations", settings.auto_title)}
  {switch("summarize_conversations", "Summarize finished conversations", settings.summarize)}
  {number("remember_conversations", "Earlier conversations remembered", settings.remember, 0, 20)}
</table>
<p class="doc-muted" style="font-size:0.8rem">Blank or "persona" follows the persona.
Remembering needs summaries: with summaries off, nothing is remembered.</p>
<button type="submit">Save overrides</button>
</form>
</div>"""


def _named_page_ownership_refusal(
    agent: Agent, identity: OperatorIdentity | None, role: str | None
) -> HTMLResponse:
    """Refuse a claim, release, or owner change on a named page agent.

    Its id is derived from its owner and name, so changing the owner would
    orphan it: the owner could no longer reopen it, and it would be swept as a
    per-tab page. Removing it is the way to let it go.
    """
    return HTMLResponse(
        _claim_panel(agent, identity, role)
        + '<p class="msg" style="color:#d29922">A named page agent always belongs to the '
        "person who named it. Remove it instead.</p>",
        status_code=409,
    )


def _render_agent_rows_data(
    rows_data: list[tuple[Agent, Persona | None]],
    heartbeat_timeout_seconds: int,
    spend_by_device: dict[str, dict[str, Any]] | None = None,
) -> str:
    if not rows_data:
        return "<tr><td colspan=10>no agents registered yet</td></tr>"
    spend_by_device = spend_by_device or {}

    import agent_hub.skills as server_skills  # local import avoids circular at module level

    skill_names = [d["function"]["name"] for d in server_skills.get_definitions()]

    rows = []
    for agent, persona in rows_data:
        device_id = html.escape(agent.device_id)
        label = html.escape(agent.label or agent.device_id)
        kind_badge = (
            f'<span class="badge badge-kind">{html.escape(agent.kind)}</span> '
            if agent.kind != "xiaozhi"
            else ""
        )
        device_cell = (
            f'{kind_badge}<a href="/dashboard/agents/{device_id}" style="color:#58a6ff">{label}</a>'
        )
        if agent.pinned:
            device_cell += (
                ' <span class="badge badge-kind" title="long-term: never pruned">kept</span>'
            )
        if agent.label:
            device_cell += f'<span class="model">{device_id}</span>'
        last_seen = fmt_ts(agent.last_seen, fmt="%H:%M:%S")
        dev = session_state.get_state(agent.device_id)

        # Persona / model cell
        persona_name = persona.name if persona else "—"
        model = (persona.llm_model or "") if persona else ""
        if not model and persona:
            model = persona.llm_provider or ""
        model_line = f'<span class="model">{model}</span>' if model else ""

        # Tools cell — device MCP badges + skill badges
        device_tools = dev.mcp_tools or agent.reported_mcp_tools_list
        tool_badges = "".join(
            f'<span class="badge badge-tool">{html.escape(t)}</span>' for t in device_tools
        )
        skill_badges = "".join(f'<span class="badge badge-skill">{s}</span>' for s in skill_names)
        tools_cell = (tool_badges + skill_badges) or '<span style="color:#6e7681">—</span>'

        # Health and activity are independent from the on-demand voice socket.
        ws_connected = session_state.is_connected(agent.device_id)
        mcp_client = session_state.get_mcp_client(agent.device_id)
        health = session_state.get_device_health(
            agent.device_id,
            agent.last_heartbeat,
            agent.health_fault,
            heartbeat_timeout_seconds,
        )
        activity = session_state.get_device_activity(agent.device_id, agent.reported_activity)
        health_color = {
            "healthy": "#3fb950",
            "degraded": "#d29922",
            "offline": "#6e7681",
        }[health]
        if agent.kind != "xiaozhi":
            # A bridged agent (browser page or robot) has no wake-word
            # firmware; what matters is whether it still holds its MCP bridge
            # stream open.
            noun = "page" if agent.kind == "page" else "agent"
            bridge = mcp_bridge.get_page_agent(agent.device_id)
            if bridge is not None and bridge.connected:
                transport = f"{noun} connected · {len(bridge.tools)} tools"
            else:
                transport = f"{noun} not connected"
        else:
            transport = "voice connected" if ws_connected else "wake-word standby"
            if mcp_client and mcp_client.ready:
                transport += f" · {len(mcp_client.tools)} MCP tools"
        fault_detail = (
            f'<div style="font-size:0.75rem;color:#d29922">{html.escape(agent.health_fault)}</div>'
            if agent.health_fault and health == "degraded"
            else ""
        )
        conn_cell = (
            f'<span style="color:{health_color}">● {health.title()}</span>'
            f" · {activity.title()}"
            f'<div style="font-size:0.75rem;color:#6e7681;margin-top:0.1rem">{transport}</div>'
            f"{fault_detail}"
        )

        # Latency cell
        if dev.turns > 0:
            L, A = dev.last, dev.avg
            lat_cell = (
                f'<div class="lat">ASR <span>{L.asr_ms}ms</span> / '
                f"LLM <span>{L.llm_ms}ms</span> / "
                f"TTS <span>{L.tts_ms}ms</span></div>"
                f'<div class="lat">avg <span>{A.asr_ms}</span>/'
                f"<span>{A.llm_ms}</span>/<span>{A.tts_ms}</span>ms "
                f"· {dev.turns} turns</div>"
            )
        else:
            lat_cell = '<span style="color:#6e7681">—</span>'

        owner_cell = (
            html.escape(agent.owner) if agent.owner else '<span style="color:#6e7681">—</span>'
        )

        # Spend cell — the same ledger for every kind of agent.
        sp = spend_by_device.get(agent.device_id)
        spend_cell = (
            f'${sp["cost_usd"]:.4f}<span class="model">{sp["calls"]} calls</span>'
            if sp
            else '<span style="color:#6e7681">—</span>'
        )

        rows.append(f"""\
<tr>
  <td>{device_cell}</td>
  <td>{conn_cell}</td>
  <td>{persona_name}{model_line}</td>
  <td>{owner_cell}</td>
  <td>{tools_cell}</td>
  <td>{lat_cell}</td>
  <td>{spend_cell}</td>
  <td>{agent.ip_address or "—"}</td>
  <td>{agent.firmware_version or "—"}</td>
  <td>{last_seen}</td>
</tr>""")
    return "".join(rows)


def _discovered_mcp_tools(
    mcp_client: Any | None,
    fallback_names: list[str],
) -> list[dict[str, Any]]:
    """Return discovered MCP tools with descriptions when a live client has them."""
    if mcp_client is not None and getattr(mcp_client, "tools", None):
        return [
            {
                "name": name,
                "description": data.get("description", "") if isinstance(data, dict) else "",
                "inputSchema": data.get("inputSchema", {}) if isinstance(data, dict) else {},
            }
            for name, data in mcp_client.tools.items()
        ]
    return [{"name": name, "description": "", "inputSchema": {}} for name in fallback_names]


def _latency_status(latency: session_state.TurnLatency) -> dict[str, int]:
    """Serialize a turn latency sample for the status API."""
    return {
        "asr_ms": latency.asr_ms,
        "llm_ms": latency.llm_ms,
        "tts_ms": latency.tts_ms,
        "total_ms": latency.total_ms,
    }


def _persona_status(persona: Any | None) -> dict[str, Any] | None:
    """Serialize persona settings relevant to device capability decisions."""
    if persona is None:
        return None
    return {
        "name": persona.name,
        "llm_provider": persona.llm_provider,
        "llm_model": persona.llm_model,
        "tts_provider": persona.tts_provider,
        "tts_voice": persona.tts_voice,
        "tts_model": persona.tts_model,
        "asr_provider": persona.asr_provider,
        "server_skills": persona.server_skills_list,
        "mcp_tools_allowlist": persona.mcp_tools_allowlist_list,
        "memory_window": persona.memory_window,
        "transcription": bool(persona.transcription),
    }


_MODELS_CACHE_TTL_S = 600.0
_models_cache: tuple[float, list[dict[str, Any]]] | None = None


def _viewer_subject(request: Request) -> str:
    """Verified Access subject of whoever is viewing, or "" without Access."""
    identity = getattr(request.state, "operator_identity", None)
    return identity.subject if identity is not None else ""


def _launcher(request: Request) -> str | None:
    """Who may open page agents from the dashboard: their subject, or None for viewers."""
    return None if _role(request) == OperatorRole.VIEWER.value else _viewer_subject(request)


def _may_choose_paid(request: Request) -> bool:
    """Whether this viewer may pick paid models in free mode (set by authentication).

    Only a verified Access identity can be allowed; a session without one
    (no Access configured) stays in free mode like everyone else.
    """
    if getattr(request.state, "operator_identity", None) is None:
        return False
    default = _role(request) == OperatorRole.ADMIN.value
    return bool(getattr(request.state, "paid_models", default))


def _role(request: Request) -> str:
    """The operator role attached to this request by authentication."""
    return str(getattr(request.state, "operator_role", OperatorRole.ADMIN.value))


def _claim_panel(
    agent: Agent | None,
    identity: OperatorIdentity | None,
    role: str | None,
) -> str:
    """Owner state plus the button that changes it, as one swappable block.

    Three cases matter: nobody has claimed it, you have, or somebody else has.
    A typed owner label with no verified claim behind it is shown as
    unverified, because a robot supplies that string about itself.
    """
    if agent is None:
        return '<div id="claim-panel"></div>'
    device_id = html.escape(agent.device_id)
    claimed_by_me = identity is not None and agent.owner_subject == identity.subject
    label = html.escape(agent.owner or "")
    if is_named_page_agent(agent):
        # Owned by whoever named it, for as long as it exists: nothing to
        # claim or release (see _named_page_ownership_refusal).
        who = "you" if claimed_by_me else (label or "this hub")
        return (
            '<div id="claim-panel" style="display:flex;gap:0.6rem;align-items:center">'
            f"<span><strong>Named page agent of {who}</strong> "
            '<span class="doc-muted">(always owned by whoever named it)</span></span></div>'
        )

    if agent.owner_subject and claimed_by_me:
        state = f'<strong>Claimed by you</strong> <span class="doc-muted">({label})</span>'
        button = "Release"
    elif agent.owner_subject:
        state = f"<strong>Claimed by {label}</strong>"
        button = "Release" if role == OperatorRole.ADMIN.value else ""
    elif agent.owner:
        state = (
            f"<strong>{label}</strong> "
            '<span class="doc-muted" title="Set by the agent itself at registration, '
            'not verified">unverified label</span>'
        )
        button = "Release"
    else:
        state = '<span class="doc-muted">Unclaimed</span>'
        button = ""

    actions = ""
    if button:
        actions += (
            f'<form hx-post="/dashboard/agents/{device_id}/release" hx-target="#claim-panel" '
            'hx-swap="outerHTML" style="display:inline">'
            f'<button type="submit" style="background:#30363d">{button}</button></form> '
        )
    if identity is not None and not claimed_by_me:
        actions += (
            f'<form hx-post="/dashboard/agents/{device_id}/claim" hx-target="#claim-panel" '
            'hx-swap="outerHTML" style="display:inline">'
            f'<button type="submit">Claim as {html.escape(identity.email)}</button></form> '
        )
    hint = (
        ""
        if identity is not None
        else '<div class="doc-muted" style="font-size:0.8rem;margin-top:0.3rem">'
        "This hub has no verified sign-in, so agents can only carry the label they "
        "register with.</div>"
    )
    return f"""\
<div id="claim-panel" style="display:flex;gap:0.6rem;align-items:center;flex-wrap:wrap">
  <span>{state}</span>{actions}
</div>{hint}"""


def _pin_form(device_id: str, pinned: bool) -> str:
    """Toggle for the long-term mark; swaps itself on submit."""
    label = "📌 Kept (long-term) — click to unpin" if pinned else "📌 Keep as long-term agent"
    return f"""\
<form hx-post="/dashboard/agents/{device_id}/pin" hx-swap="outerHTML" style="display:inline"
      title="Long-term agents are never counted as stale or pruned">
  <input type="hidden" name="pinned" value="{"0" if pinned else "1"}">
  <button type="submit" style="background:{"#1f6feb" if pinned else "#30363d"}">{label}</button>
</form>"""


def _voice_datalist(tts_provider: str, current: str) -> str:
    """``<datalist id="tts-voices">`` for one TTS system, keeping the current value."""
    voices = list(dict.fromkeys([*persona_options.voices_for(tts_provider), current.strip()]))
    opts = "".join(f'<option value="{html.escape(v)}"></option>' for v in voices if v)
    return f'<datalist id="tts-voices">{opts}</datalist>'


def _tts_model_datalist(tts_provider: str, current: str, *, oob: bool = False) -> str:
    """``<datalist id="tts-models">`` for one TTS system, keeping the current value."""
    models = list(dict.fromkeys([*persona_options.tts_models_for(tts_provider), current.strip()]))
    opts = "".join(f'<option value="{html.escape(m)}"></option>' for m in models if m)
    swap = ' hx-swap-oob="true"' if oob else ""
    return f'<datalist id="tts-models"{swap}>{opts}</datalist>'


_VERDICT_STYLE = {
    "usable": ("#3fb950", "✓ Usable for voice"),
    "slow": ("#d29922", "⚠ Slow"),
    "unreliable": ("#d29922", "⚠ Unreliable"),
    "unusable": ("#f85149", "✗ Not usable"),
}


def _render_model_check(result: ModelCheck) -> str:
    """The verdict on a model, with each test turn that led to it."""
    color, label = _VERDICT_STYLE[result.verdict]
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(p.prompt)}</td>"
        + (
            '<td style="color:#3fb950">✓</td>'
            if p.ok
            else f'<td style="color:#f85149">✗ {html.escape(p.problem or "failed")}</td>'
        )
        + f"<td>{'—' if p.first_text_s is None else f'{p.first_text_s:.1f}s'}</td>"
        f"<td>{p.total_s:.1f}s</td>"
        f"<td>{'yes' if p.tool_called else ('no' if p.expects_tool else '—')}</td>"
        f'<td style="white-space:pre-wrap;max-width:28rem">{html.escape(p.reply[:200])}</td>'
        "</tr>"
        for p in result.probes
    )
    return f"""\
<div class="model-check" style="border:1px solid {color};border-radius:6px;padding:.6rem .8rem;
  margin:.5rem 0">
  <strong style="color:{color}">{label}</strong> <code>{html.escape(result.model)}</code>
  <div>{html.escape(result.reason)}</div>
  <table style="width:auto;margin-top:.4rem">
    <thead><tr><th>asked</th><th>result</th><th>first words</th><th>total</th>
      <th>tool called</th><th>reply</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>"""


def _uses_openrouter(config: dict[str, Any], provider: str | None) -> bool:
    """Whether a provider's models come from OpenRouter's catalogue."""
    base_url = ((config.get("llm") or {}).get(provider or "openai") or {}).get("base_url", "")
    return "openrouter.ai" in str(base_url)


def _ago(epoch_s: float) -> str:
    """How long ago, roughly, for status panels."""
    secs = max(0, int(time.time() - epoch_s))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    return f"{secs // 3600} h ago"


async def _is_known_paid(model_id: str, api_key: str) -> bool:
    """True when the catalogue lists ``model_id`` as paid (offline: no ``:free`` suffix)."""
    models = await _fetch_openrouter_models(api_key)
    if not models:
        return not model_id.endswith(":free")
    return any(m["id"] == model_id and not m["free"] for m in models)


async def supports_tools(model_id: str, api_key: str) -> bool:
    """False only when the catalogue knows ``model_id`` and says it lacks tool calling."""
    models = await _fetch_openrouter_models(api_key)
    for m in models:
        if m["id"] == model_id:
            return bool(m["tools"])
    return True


async def is_free_model(model_id: str, api_key: str) -> bool:
    """True when OpenRouter prices ``model_id`` at $0 for prompt tokens.

    Falls back to the ``:free`` id suffix when the catalogue cannot be
    fetched, so free mode still works offline (and never lets a paid model
    through just because the network was down).
    """
    models = await _fetch_openrouter_models(api_key)
    if models:
        return any(m["id"] == model_id and m["free"] for m in models)
    return model_id.endswith(":free")


async def _fetch_openrouter_models(api_key: str) -> list[dict[str, Any]]:
    # Cached for a few minutes: the picker, free-mode checks, and persona
    # saves all consult the catalogue, and it changes rarely.
    global _models_cache
    now = asyncio.get_running_loop().time()
    if _models_cache is not None and now - _models_cache[0] < _MODELS_CACHE_TTL_S:
        return _models_cache[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            resp = await client.get(_OPENROUTER_MODELS_URL, headers=headers)
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception as exc:
        logger.error(f"OpenRouter models fetch failed: {exc}")
        return []

    out = []
    for m in data:
        arch = m.get("architecture", {})
        modality = arch.get("modality", "") or arch.get("input_modalities", [])
        multimodal = (
            "image" in str(modality)
            if isinstance(modality, str)
            else any("image" in str(x) for x in modality)
        )
        pricing = m.get("pricing", {})
        try:
            price_in = float(pricing.get("prompt", 0)) * 1_000_000
            price_str = f"${price_in:.3f}" if price_in > 0 else "free"
            free = price_in == 0
        except (ValueError, TypeError):
            price_str = "—"
            free = False
        ctx = m.get("context_length", 0)
        params = m.get("supported_parameters") or []
        out.append(
            {
                "id": m.get("id", ""),
                "name": m.get("name", ""),
                "context_k": ctx // 1000 if ctx else "—",
                "price_in": price_str,
                "multimodal": multimodal,
                "free": free,
                # OpenRouter lists "tools" in supported_parameters for models
                # that accept function-calling requests.
                "tools": "tools" in params if isinstance(params, list) else False,
            }
        )

    out.sort(key=lambda x: (not x["multimodal"], x["id"]))
    _models_cache = (now, out)
    return out
