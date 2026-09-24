"""Run a browser agent from its card: the page agent inside the side panel.

A browser agent lives in whatever page runs it, since that page holds its
microphone, speaker, and camera. Launch runs it in an iframe in the fleet's
side panel, so starting it and talking to it happen from the cards; closing
the panel stops it, as closing its tab would. "Own window" opens the same
page in a separate tab for an agent that should outlive the dashboard page.
"""

from __future__ import annotations

import html
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from agent_hub.registry.models import Persona

PAGE_RUN_CSS = """
#conversation-panel.page-run{display:flex;flex-direction:column;width:min(560px,100vw)}
#conversation-panel.page-run iframe{flex:1;min-height:0;width:100%;box-sizing:border-box;
  border:1px solid #334155;
  border-radius:6px;background:#0d1117}
.page-run-note{color:#94a3b8;font-size:.78rem;margin:.3rem 0 .6rem}
.new-agent-card{border-left-color:#38bdf8;border-style:dashed}
.new-agent-card h4{margin:0 0 .5rem}.new-agent-card label{display:block;font-size:.75rem;
  color:#94a3b8;margin:.5rem 0 .2rem}.new-agent-card input,.new-agent-card select{width:100%;
  box-sizing:border-box}
@media(min-width:1000px){body:has(#conversation-panel.page-run){padding-right:590px}}
"""


def page_url(
    name: str = "", persona: str = "", *, embed: bool = False, handoff: bool = False
) -> str:
    """The page-agent page for ``name`` (and ``persona``), embedded or standalone.

    ``handoff`` marks a move out of the side panel: the new window takes the
    agent over, since the panel's goodbye can arrive after its registration.
    """
    params = {k: v for k, v in (("name", name), ("persona", persona)) if v}
    if embed:
        params["embed"] = "1"
    if handoff:
        params["handoff"] = "1"
    query = urlencode(params)
    return "/dashboard/page-agent" + (f"?{query}" if query else "")


def run_url(name: str = "", persona: str = "") -> str:
    """The side-panel route that runs a browser agent in this page."""
    params = {k: v for k, v in (("name", name), ("persona", persona)) if v}
    return "/dashboard/page-agent/run" + (f"?{urlencode(params)}" if params else "")


def launch_links(name: str = "", persona: str = "") -> str:
    """Launch (in the side panel) plus Own window (a separate tab), as card actions."""
    run = html.escape(run_url(name, persona))
    window = html.escape(page_url(name, persona))
    return (
        f'<a class="action-link primary" href="{run}" hx-get="{run}" '
        f'hx-target="#conversation-host" hx-swap="innerHTML" data-conversation-open '
        f'title="Run this agent here, in the side panel">Launch</a>'
        f'<a class="action-link" href="{window}" target="_blank" rel="noopener" '
        f'title="Run this agent in its own browser window">Own window</a>'
    )


def new_agent_card(personas: list[Persona]) -> str:
    """A card that starts a new browser agent by name and persona."""
    options = '<option value="">hub default</option>' + "".join(
        f'<option value="{html.escape(p.name)}">{html.escape(p.name)}</option>'
        for p in personas
        if not p.transcription
    )
    return f"""\
<section class="new-agent" aria-labelledby="new-agent-heading">
<div class="agent-card-grid"><form class="agent-card new-agent-card"
  hx-get="/dashboard/page-agent/run" hx-target="#conversation-host"
  hx-swap="innerHTML" data-conversation-open>
  <h4 id="new-agent-heading">+ New browser agent</h4>
  <p class="agent-muted">Runs in your browser with its microphone, speaker, and camera.
    The same name later is the same agent, with its history.</p>
  <label for="new-agent-name">Name</label>
  <input id="new-agent-name" name="name" maxlength="64" required autocomplete="off"
    data-1p-ignore data-lpignore="true" placeholder="e.g. toaster3000">
  <label for="new-agent-persona">Persona</label>
  <select id="new-agent-persona" name="persona">{options}</select>
  <div class="agent-card-actions"><button type="submit" class="action-link primary">Launch</button>
    <button type="button" class="action-link" data-own-window>Own window</button></div>
</form></div></section>"""


def make_page_run_router() -> APIRouter:
    """Build the side-panel run route; the parent dashboard supplies authentication."""
    router = APIRouter()

    @router.get("/dashboard/page-agent/run", response_class=HTMLResponse)
    async def run(request: Request, name: str = "", persona: str = "") -> HTMLResponse:
        """Run a browser agent in the side panel."""
        if getattr(request.state, "operator_role", "admin") == "viewer":
            raise HTTPException(403, "Viewers cannot run agents.")
        name, persona = name.strip(), persona.strip()
        title = html.escape(name or "New browser agent")
        frame = html.escape(page_url(name, persona, embed=True))
        window = html.escape(page_url(name, persona, handoff=bool(name)))
        return HTMLResponse(f"""
<aside id="conversation-panel" class="page-run" aria-labelledby="conversation-title"
  tabindex="-1" data-running-agent="{html.escape(name)}">
  <header><div><p class="doc-muted">Running here</p><h2 id="conversation-title">{title}</h2></div>
    <button type="button" data-close-conversation aria-label="Stop and close">✕</button>
  </header>
  <p class="page-run-note">Closing this panel or leaving this page stops the agent.
    <a href="{window}" target="_blank" rel="noopener" data-own-window-move>Open in own window</a>
    to keep it running on its own.</p>
  <iframe src="{frame}" title="{title}" allow="microphone; camera; autoplay"></iframe>
</aside>""")

    return router
