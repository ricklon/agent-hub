"""Dashboard: agent list + OpenRouter model picker.

Server-rendered with HTMX — no SPA build step.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger

from agent_hub import spend
from agent_hub.config import resolve_timezone
from agent_hub.dashboard import persona_options
from agent_hub.dashboard._timefmt import fmt_ts
from agent_hub.dashboard.access_identity import OperatorIdentity
from agent_hub.dashboard.audit import render_audit_table
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.dashboard.overview import render_fleet_overview
from agent_hub.registry.models import Agent, OperatorRole, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state, tool_policy

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_CSS = """\
body{font-family:monospace;padding:2rem;background:#0d1117;color:#c9d1d9;margin:0}
h1{color:#58a6ff;margin-bottom:0.25rem}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}
.operator{display:flex;align-items:center;gap:0.6rem;color:#8b949e;font-size:0.8rem}
.operator-email{color:#c9d1d9}
.operator-role{border:1px solid #30363d;border-radius:999px;padding:0.15rem 0.45rem}
.operator a{color:#58a6ff;text-decoration:none}
.operator a:hover{text-decoration:underline}
nav{display:flex;flex-wrap:wrap;gap:0.5rem 1.5rem;margin-bottom:2rem}
nav a{color:#58a6ff;text-decoration:none}
nav a:hover{text-decoration:underline}
section{margin-bottom:2rem}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #30363d;padding:0.5rem 0.75rem;text-align:left;vertical-align:top}
th{background:#161b22;white-space:nowrap}
tr:hover td{background:#161b22}
.badge{font-size:0.68rem;padding:0.1rem 0.35rem;border-radius:3px;
  margin:0.1rem 0.1rem 0 0;display:inline-block}
.badge-multi{background:#1f4a2e;color:#3fb950}
.badge-free{background:#2d1f6e;color:#a5a0ff}
.badge-tool{background:#1a2a3a;color:#79c0ff}
.badge-skill{background:#2a1a3a;color:#d2a8ff}
.badge-kind{background:#3a2a1a;color:#f0883e}
.status-active{color:#3fb950}
.status-idle{color:#d29922}
.status-degraded{color:#d29922}
.status-offline{color:#6e7681}
.status-discovered{color:#58a6ff}
.lat{font-size:0.75rem;color:#8b949e}
.lat span{color:#c9d1d9}
.model{font-size:0.75rem;color:#8b949e;display:block;margin-top:0.15rem}
input,select{background:#161b22;color:#c9d1d9;border:1px solid #30363d;
  padding:0.4rem 0.6rem;border-radius:4px;margin-right:0.5rem}
button{background:#238636;color:#fff;border:none;padding:0.4rem 0.9rem;
  border-radius:4px;cursor:pointer}
button:hover{background:#2ea043}
button:disabled{cursor:wait;opacity:0.65}
button.selected{background:#1f4a2e;color:#3fb950;border:1px solid #3fb950}
.msg{color:#3fb950;margin-top:0.5rem}
.controls{display:flex;align-items:center;flex-wrap:wrap;gap:0.5rem;margin-bottom:1rem}
:where(a,button,input,select,textarea):focus-visible{outline:3px solid #58a6ff;
  outline-offset:2px}
.htmx-indicator{opacity:0}
#global-progress{position:fixed;z-index:100;top:0;left:0;right:0;padding:0.35rem 1rem;
  text-align:center;background:#1f6feb;color:#fff;pointer-events:none;
  opacity:0;transition:opacity 120ms linear}
/* Show-delay: the dashboard polls a few regions every 1-5s, and each poll
   toggles this global indicator. A 600ms delay before it fades in means those
   quick requests finish first and never flash the banner, while a real
   navigation or a slow action still surfaces it. */
.htmx-request#global-progress{opacity:1;transition-delay:600ms}
@media (prefers-reduced-motion:reduce){#global-progress{transition:none}}
#global-feedback{position:fixed;z-index:101;right:1rem;bottom:1rem;max-width:28rem;
  border:1px solid #f85149;border-radius:6px;padding:0.75rem 1rem;background:#2d1117;
  color:#ff7b72;box-shadow:0 4px 20px #010409}
#global-feedback:empty{display:none}
form.htmx-request{opacity:0.78}
"""

_CSS_EXTRA = """\
textarea{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:0.4rem 0.6rem;
  border-radius:4px;width:100%;box-sizing:border-box;font-family:monospace;resize:vertical}
label{display:block;color:#8b949e;font-size:0.8rem;margin-top:0.75rem;
  margin-bottom:0.2rem}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.form-section{background:#161b22;border:1px solid #30363d;border-radius:6px;
  padding:1.25rem;margin-bottom:1.5rem}
.form-section h3{margin:0 0 1rem;color:#58a6ff}
input[type=number]{width:6rem}
.doc-page{max-width:980px;line-height:1.55}
.doc-page h2{color:#58a6ff;margin-bottom:0.35rem}
.doc-page h3{color:#c9d1d9;margin:1.25rem 0 0.4rem}
.doc-page p{color:#c9d1d9}
.doc-page ul{padding-left:1.4rem}
.doc-page li{margin:0.35rem 0}
.doc-muted{color:#8b949e}
.doc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem}
.doc-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:1rem}
.doc-card h3{margin-top:0;color:#58a6ff}
.doc-flow{background:#010409;border:1px solid #30363d;border-radius:6px;
  padding:1rem;white-space:pre-wrap;overflow:auto}
.spend-ok{color:#3fb950}
.spend-warn{color:#d29922}
.spend-over{color:#f85149}
.audit-success{color:#3fb950}
.audit-failure{color:#f85149}
.section-heading{display:flex;justify-content:space-between;align-items:center;gap:1rem}
.section-heading h2,.section-heading p{margin:0 0 0.35rem}
.action-link{display:inline-block;color:#58a6ff;border:1px solid #30363d;border-radius:4px;
  padding:0.4rem 0.7rem;text-decoration:none;white-space:nowrap}
.action-link:hover{border-color:#58a6ff;background:#161b22;text-decoration:none}
.action-link.primary{color:#fff;background:#238636;border-color:#238636}
.overview-grid{display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:0.75rem;
  margin-top:0.75rem}
.overview-card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:1rem}
.overview-value{font-size:1.8rem;display:block;color:#c9d1d9}
.overview-label{font-size:0.75rem;color:#8b949e}
.overview-good .overview-value{color:#3fb950}
.overview-warn .overview-value{color:#d29922}
.overview-muted .overview-value{color:#8b949e}
.attention-panel{border:1px solid #5a4217;background:#17130b;border-radius:6px;padding:1rem}
.attention-count{font-size:0.75rem;background:#5a4217;color:#f2cc60;border-radius:999px;
  padding:0.15rem 0.45rem;vertical-align:middle}
.attention-list{display:grid;gap:0.6rem;margin-top:0.75rem}
.attention-item{display:flex;justify-content:space-between;align-items:center;gap:1rem;
  background:#0d1117;border:1px solid #30363d;border-radius:4px;padding:0.75rem}
.attention-status{font-size:0.72rem;margin-left:0.5rem}
.attention-detail{font-size:0.75rem;color:#8b949e;margin-top:0.25rem}
.attention-clear{display:flex;gap:0.75rem;align-items:center;border:1px solid #1f4a2e;
  background:#0e1711;border-radius:6px;padding:0.8rem 1rem;color:#3fb950}
.attention-clear span{color:#8b949e;font-size:0.8rem}
.empty-state{text-align:center;max-width:650px;margin:4rem auto;padding:2rem;
  border:1px dashed #30363d;border-radius:8px;background:#161b22}
.empty-state h2{color:#58a6ff}.empty-state p{line-height:1.6;color:#8b949e}
.empty-icon{font-size:2.5rem;color:#58a6ff}.empty-actions{display:flex;gap:0.75rem;
  justify-content:center;margin-top:1.25rem}
@media (max-width:760px){
  body{padding:1rem}
  header{align-items:flex-start;flex-direction:column}
  .operator{align-items:flex-start;flex-wrap:wrap}
  nav{gap:0.75rem 1.25rem;margin:1rem 0 1.5rem}
  .overview-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .field-row{grid-template-columns:1fr}
  .attention-item,.section-heading{align-items:flex-start;flex-direction:column}
  .empty-actions{align-items:stretch;flex-direction:column}
  table{display:block;max-width:100%;overflow-x:auto}
  input:not([type=checkbox]),select,textarea{box-sizing:border-box;max-width:100%;width:100%}
  form[style*="display:flex"],form[style*="display:inline-flex"]{align-items:stretch!important;
    flex-direction:column}
  button,.action-link{min-height:44px}
}
@media (prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
"""

_PAGE = """\
<!doctype html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-hub</title>
<style>{css}</style>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
</head><body hx-headers='{{"X-Requested-With":"XMLHttpRequest"}}'
  hx-indicator="#global-progress">
<div id="global-progress" class="htmx-indicator" role="status" aria-live="polite">
  Working…
</div>
<div id="global-feedback" role="alert" aria-live="assertive"></div>
<header><h1>agent-hub</h1>{operator}</header>
<nav>
  <a href="/dashboard/">Agents</a>
  <a href="/dashboard/personas">Personas</a>
  <a href="/dashboard/models">Models</a>
  {admin_nav}
  {operator_nav}
  <a href="/dashboard/docs">Docs</a>
</nav>
{body}
<script>
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
    api_key: str = config.get("llm", {}).get("openai", {}).get("api_key", "")

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

    _full_css = _CSS + _CSS_EXTRA

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
            '<a href="/dashboard/page-agent">Page Agent</a>'
            if role in {OperatorRole.ADMIN.value, OperatorRole.OPERATOR.value}
            else ""
        )
        return _PAGE.format(
            css=_full_css,
            operator=operator,
            admin_nav=admin_nav,
            operator_nav=operator_nav,
            body=body,
        )

    @router.get("/dashboard/", response_class=HTMLResponse)
    async def dashboard_index(request: Request) -> HTMLResponse:
        overview = await _render_agent_overview(store, heartbeat_timeout_seconds)
        body = await _spend_panel() + overview
        return HTMLResponse(_render_page(request, body))

    async def _spend_panel() -> str:
        """Spend summary for the dashboard header, or nothing if unmetered."""
        tracker = spend.get_tracker()
        if tracker is None:
            return ""
        totals = await tracker.totals()
        return _render_spend_panel(totals)

    @router.get("/dashboard/agents", response_class=HTMLResponse)
    async def dashboard_agents_partial(request: Request) -> HTMLResponse:
        rows = await _render_agent_rows(store, heartbeat_timeout_seconds)
        return HTMLResponse(_agent_table(rows))

    @router.get("/dashboard/overview", response_class=HTMLResponse)
    async def dashboard_overview_partial(request: Request) -> HTMLResponse:
        return HTMLResponse(await _render_agent_overview(store, heartbeat_timeout_seconds))

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
        body = f"""\
<h2>Operators</h2>
<p class="doc-muted">Cloudflare Access decides who may sign in. Agent Hub assigns
what each verified identity may do. New identities start as viewers.</p>
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
    ) -> HTMLResponse:
        try:
            parsed_role = OperatorRole(role)
        except ValueError:
            raise HTTPException(status_code=422, detail="Unknown operator role") from None
        ok = await store.update_dashboard_operator(
            subject,
            parsed_role,
            enabled=enabled == "1",
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

    @router.get("/dashboard/agents/{device_id}/transcript.txt")
    async def agent_transcript_download(device_id: str, session: str = "") -> Response:
        agent = await store.get_agent(device_id)
        if agent is None:
            return Response(status_code=404)
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

        if ws_connected:
            ws_html = '<span style="color:#3fb950">● connected</span>'
        else:
            ws_html = '<span style="color:#6e7681">○ closed (wake-word standby)</span>'

        if mcp_client and mcp_client.ready:
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
        return HTMLResponse(f"""\
<table style="width:auto;margin-bottom:0.5rem">
  <tr><th style="width:7rem">Health</th>
      <td><span style="color:{health_color}">{health.title()}</span></td></tr>
  <tr><th>Activity</th><td>{activity.title()}</td></tr>
  <tr><th>Voice transport</th><td>{ws_html}</td></tr>
  <tr><th>MCP</th><td>{mcp_html}</td></tr>
  <tr><th>Registration</th><td>{db_status}</td></tr>
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
  <tr><th>TTS provider</th><td>{persona.tts_provider}{
                f" / {persona.tts_voice}" if persona.tts_voice else ""
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

        # Assistant-only actions: both run the LLM/TTS pipeline, which a
        # transcriber device never does.
        assistant_actions = (
            ""
            if is_transcriber
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

        history_heading = "Transcript" if is_transcriber else "Conversation history"
        clear_confirm = (
            "Clear the full transcript for this device?"
            if is_transcriber
            else "Clear all conversation history for this device?"
        )
        download_link = (
            f'&nbsp;·&nbsp;<a href="/dashboard/agents/{device_id}/transcript.txt" '
            f'style="color:#58a6ff;font-size:0.85rem">⬇ download .txt</a>'
            if is_transcriber
            else ""
        )

        # Reboot + send message
        speak_form = f"""\
<form hx-post="/dashboard/agents/{device_id}/reboot"
      hx-target="#reboot-result" hx-swap="innerHTML"
      hx-confirm="Reboot this device now? Its active session will disconnect."
      style="display:inline">
  <button type="submit" style="background:#6e3a1e">↺ Reboot device</button>
</form>
{camera_btn}
<span id="reboot-result" role="status" aria-live="polite"
      style="margin-left:0.75rem"></span>
{assistant_actions}
<h3>{history_heading}{download_link}</h3>
<div style="margin-bottom:0.4rem;font-size:0.85rem">
  Pipeline:&nbsp;<span
    hx-get="/dashboard/agents/{device_id}/pipeline_status"
    hx-trigger="load, every 1s"
    hx-swap="innerHTML"
    id="pipeline-status">—</span>
</div>
<div hx-get="/dashboard/agents/{device_id}/history"
     hx-trigger="load, every 2s"
     hx-swap="innerHTML"
     id="history-view">Loading…</div>
<form hx-post="/dashboard/agents/{device_id}/clear_history"
      hx-target="#history-view" hx-swap="innerHTML"
      hx-confirm="{clear_confirm}"
      style="margin-top:0.5rem">
  <button type="submit" style="background:#b62324">Clear history</button>
</form>"""

        body = f"""\
<p><a href="/dashboard/" style="color:#58a6ff">← agents</a></p>
<h2>{html.escape(agent.label or device_id)}</h2>
<p style="color:#8b949e;margin-top:-0.5rem">
  Device ID: {html.escape(device_id)} &nbsp;·&nbsp;
  IP: {agent.ip_address or "—"} &nbsp;·&nbsp;
  Firmware: {agent.firmware_version or "—"} &nbsp;·&nbsp;
  Last seen: {fmt_ts(agent.last_seen, display_tz, "%H:%M:%S")}
</p>
<h3>Connection</h3>
<div hx-get="/dashboard/agents/{device_id}/status"
     hx-trigger="load, every 3s"
     hx-swap="innerHTML"
     id="device-status">Loading…</div>
{persona_html}
<h3>Device MCP tools</h3>
<div>{device_tool_badges}</div>
<h3>Server skills</h3>
<div>{skill_badges}</div>
<h3>Latency</h3>
{lat_html}
{speak_form}"""
        return HTMLResponse(_render_page(request, body))

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
                tts_cell = p.tts_provider + (f" / {p.tts_voice}" if p.tts_voice else "")
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

        def _select(field: str, choices: list[str], current: str) -> str:
            merged = list(dict.fromkeys([*choices, current]))
            opts = "".join(
                f'<option value="{html.escape(v)}"'
                f"{' selected' if v == current else ''}>{html.escape(v)}</option>"
                for v in merged
                if v
            )
            return f'<select name="{field}">{opts}</select>'

        llm_select = _select("llm_provider", ["openai"], persona.llm_provider)
        tts_select = _select(
            "tts_provider", list(persona_options.TTS_PROVIDERS), persona.tts_provider
        )
        asr_select = _select("asr_provider", persona_options.asr_providers(), persona.asr_provider)
        voice_datalist = "".join(
            f'<option value="{html.escape(v)}"></option>'
            for v in persona_options.TTS_VOICE_SUGGESTIONS
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
        llm_model_val = html.escape(persona.llm_model or "")
        tts_voice_val = html.escape(persona.tts_voice or "")
        tools_val_esc = html.escape(tools_val)
        transcription_checked = " checked" if persona.transcription else ""

        body = f"""\
<p><a href="/dashboard/personas" style="color:#58a6ff">← personas</a></p>
<h2>Edit persona: {name}</h2>
<p><a href="/dashboard/page-agent?persona={quote(name)}" style="color:#58a6ff">
  ▶ Launch as page agent</a> &nbsp;— talk to this persona in the browser, no hardware.</p>
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

  <div class="form-section">
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
    <div class="field-row">
      <div><label>LLM provider</label>{llm_select}</div>
      <div><label>LLM model (blank = config default)</label>
        <input type="text" name="llm_model" value="{llm_model_val}"
          style="width:300px" placeholder="browse ids on the Models page"></div>
    </div>
    <div class="field-row">
      <div><label>TTS system</label>{tts_select}</div>
      <div><label>TTS voice (blank = system default)</label>
        <input type="text" name="tts_voice" value="{tts_voice_val}"
          list="tts-voices" style="width:300px">
        <datalist id="tts-voices">{voice_datalist}</datalist></div>
    </div>
    <div class="field-row">
      <div><label>ASR system</label>{asr_select}</div>
      <div></div>
    </div>
  </div>

  <div class="form-section">
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

  <div class="form-section">
    <h3>Linked agents</h3>
    <p style="color:#6e7681;font-size:0.8rem;margin:0">
      Borrow the non-destructive MCP tools of other connected agents (a robot,
      another page). Borrowed tool names are prefixed with the agent id.
    </p>
    {linked_boxes}
  </div>

  <div class="form-section">
    <h3>Memory</h3>
    <label>Conversation window (turns kept in LLM context)</label>
    <input type="number" name="memory_window" value="{persona.memory_window}" min="1" max="200">
  </div>

  <button type="submit">Save</button>
</form>"""
        return HTMLResponse(_render_page(request, body))

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
        asr_provider: str = Form(default=""),
        mcp_tools_allowlist: str = Form(default=""),
        memory_window: int = Form(default=20),
        transcription: str = Form(default=""),
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

        ok = await store.update_persona(
            name,
            system_prompt=system_prompt,
            llm_provider=llm_provider or None,
            llm_model=llm_model,
            tts_provider=tts_provider or None,
            tts_voice=tts_voice,
            asr_provider=asr_provider or None,
            server_skills=skills_arg,
            mcp_tools_allowlist=tools_arg,
            linked_agents=linked_arg,
            memory_window=max(1, memory_window),
            transcription=bool(transcription.strip()),
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
        body = f"""\
<h2>Model Picker</h2>
<p>Current: <strong id="current-model">{current or "not set"}</strong></p>
<div class="controls">
  <input id="search" type="text" placeholder="Search models..."
    hx-get="/dashboard/models/list"
    hx-trigger="input changed delay:300ms"
    hx-target="#model-list"
    hx-include="#multimodal-only"
    name="search">
  <label>
    <input id="multimodal-only" type="checkbox" name="multimodal" value="1"
      hx-get="/dashboard/models/list"
      hx-trigger="change"
      hx-target="#model-list"
      hx-include="#search">
    Multimodal only
  </label>
</div>
<div id="model-list"
  hx-get="/dashboard/models/list"
  hx-trigger="load"
  hx-include="#search,#multimodal-only">
  Loading…
</div>
"""
        return HTMLResponse(_render_page(request, body))

    @router.get("/dashboard/models/list", response_class=HTMLResponse)
    async def models_list(
        request: Request,
        search: str = "",
        multimodal: str = "",
    ) -> HTMLResponse:
        models = await _fetch_openrouter_models(api_key)
        personas = await store.list_personas()
        current = next(
            (p.llm_model for p in personas if p.name == "hub-default"), None
        ) or config.get("llm", {}).get("openai", {}).get("model", "")

        only_multi = bool(multimodal)
        q = search.lower()

        filtered = [
            m
            for m in models
            if (not q or q in m["id"].lower() or q in m["name"].lower())
            and (not only_multi or m["multimodal"])
        ]

        if not filtered:
            return HTMLResponse("<p>No models match.</p>")

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
    <button class="{btn_class}"
      hx-post="/dashboard/models/select"
      hx-vals='{{"model_id":"{m["id"]}","persona":"hub-default"}}'
      hx-target="#model-list"
      hx-swap="none"
      hx-on::after-request="document.getElementById('current-model').innerText='{m["id"]}'"
    >{"✓ active" if selected else "select"}</button>
  </td>
</tr>""")

        table = f"""\
<table>
<thead><tr>
  <th>model id</th><th>name</th><th>ctx</th><th>$/M in</th><th></th>
</tr></thead>
<tbody>{"".join(rows)}</tbody>
</table>"""
        return HTMLResponse(table)

    @router.post("/dashboard/models/select", response_class=HTMLResponse)
    async def models_select(
        model_id: str = Form(...),
        persona: str = Form(default="hub-default"),
    ) -> HTMLResponse:
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
    last_seen = fmt_ts(operator.last_seen_at)
    subject = quote(operator.subject, safe="")
    return f"""\
<tr><td>{html.escape(operator.email)}</td>
<td><form class="controls" style="margin:0"
  hx-post="/dashboard/operators/{subject}"
  hx-target="#operator-result" hx-swap="innerHTML">
  <select name="role">{options}</select>
  <label style="margin:0"><input type="checkbox" name="enabled" value="1"{checked}> enabled</label>
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


def _agent_table(rows: str, *, poll: bool = True) -> str:
    poll_attributes = (
        ' hx-get="/dashboard/agents" hx-trigger="every 5s" hx-swap="outerHTML"' if poll else ""
    )
    return f"""\
<div{poll_attributes}>
<table>
<thead><tr>
  <th>device</th><th>health · activity</th><th>persona / model</th>
  <th>tools</th><th>latency (last / avg)</th>
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


async def _render_agent_rows(store: RegistryStore, heartbeat_timeout_seconds: int) -> str:
    try:
        rows_data = await store.list_agents_with_personas()
    except Exception as exc:
        logger.error(f"Dashboard agent query failed: {exc}")
        return "<tr><td colspan=8>error loading agents</td></tr>"

    return _render_agent_rows_data(rows_data, heartbeat_timeout_seconds)


async def _render_agent_overview(
    store: RegistryStore,
    heartbeat_timeout_seconds: int,
) -> str:
    try:
        rows_data = await store.list_agents_with_personas()
    except Exception as exc:
        logger.error(f"Dashboard overview query failed: {exc}")
        return '<p class="audit-failure">Could not load fleet status.</p>'
    overview = render_fleet_overview(rows_data, heartbeat_timeout_seconds)
    if not rows_data:
        return _overview_poll_wrapper(overview)
    rows = _render_agent_rows_data(rows_data, heartbeat_timeout_seconds)
    return _overview_poll_wrapper(overview + "<h2>All agents</h2>" + _agent_table(rows, poll=False))


def _overview_poll_wrapper(content: str) -> str:
    return (
        '<div id="fleet-overview" hx-get="/dashboard/overview" '
        'hx-trigger="every 5s" hx-swap="outerHTML">'
        f"{content}</div>"
    )


def _render_agent_rows_data(
    rows_data: list[tuple[Agent, Persona | None]],
    heartbeat_timeout_seconds: int,
) -> str:
    if not rows_data:
        return "<tr><td colspan=8>no agents registered yet</td></tr>"

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

        rows.append(f"""\
<tr>
  <td>{device_cell}</td>
  <td>{conn_cell}</td>
  <td>{persona_name}{model_line}</td>
  <td>{tools_cell}</td>
  <td>{lat_cell}</td>
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
        "asr_provider": persona.asr_provider,
        "server_skills": persona.server_skills_list,
        "mcp_tools_allowlist": persona.mcp_tools_allowlist_list,
        "memory_window": persona.memory_window,
        "transcription": bool(persona.transcription),
    }


async def _fetch_openrouter_models(api_key: str) -> list[dict[str, Any]]:
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
        out.append(
            {
                "id": m.get("id", ""),
                "name": m.get("name", ""),
                "context_k": ctx // 1000 if ctx else "—",
                "price_in": price_str,
                "multimodal": multimodal,
                "free": free,
            }
        )

    out.sort(key=lambda x: (not x["multimodal"], x["id"]))
    return out
