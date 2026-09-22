"""Capability-oriented cards for the agent fleet dashboard."""

from __future__ import annotations

import html
from urllib.parse import quote

from agent_hub.dashboard._timefmt import fmt_ts
from agent_hub.registry.models import Agent, AgentKind, Persona
from agent_hub.server import mcp_bridge, session_state

AGENT_CARD_CSS = """\
.fleet-toolbar{display:flex;justify-content:space-between;align-items:flex-end;gap:1rem;
  flex-wrap:wrap;margin:.5rem 0 .9rem}.view-switch{display:flex;gap:.35rem}
.view-switch a{background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:4px;
  padding:.4rem .7rem;text-decoration:none}.view-switch a[aria-current=page]{background:#0369a1;
  border-color:#0369a1;color:#fff}.agent-owner-group{margin:0 0 1.5rem}
.agent-owner-heading{display:flex;align-items:baseline;gap:.55rem;margin-bottom:.55rem}
.agent-owner-heading h3{margin:0;color:#e2e8f0}
.agent-owner-heading span{color:#94a3b8;font-size:.75rem}
.agent-card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(285px,1fr));gap:.85rem}
.agent-card{position:relative;background:#0f172a;border:1px solid #334155;border-left-width:4px;
  border-radius:8px;padding:1rem;min-width:0}.agent-card.health-healthy{border-left-color:#3fb950}
.agent-card.health-degraded{border-left-color:#d29922}.agent-card.health-offline{border-left-color:#94a3b8}
.agent-card-top{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:.7rem;
  align-items:center}
.agent-kind-icon{display:grid;place-items:center;width:2.25rem;height:2.25rem;border-radius:50%;
  color:#79c0ff;background:#020617;border:1px solid #334155}.agent-kind-icon svg{width:1.25rem;
  height:1.25rem;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;
  stroke-linejoin:round}.agent-card-title h4{margin:0;font-size:1rem;overflow-wrap:anywhere}
.agent-card-title a{color:#38bdf8;text-decoration:none}
.agent-card-title a:hover{text-decoration:underline}
.agent-device-id,.agent-model{display:block;color:#94a3b8;font-size:.72rem;margin-top:.15rem;
  overflow-wrap:anywhere}.agent-health-dot{width:.65rem;height:.65rem;border-radius:50%;
  background:#94a3b8;box-shadow:0 0 0 3px #1e293b}
.health-healthy .agent-health-dot{background:#3fb950}
.health-degraded .agent-health-dot{background:#d29922}
.agent-state-row{display:flex;align-items:center;
  gap:.5rem;flex-wrap:wrap;margin:.85rem 0 .25rem;font-size:.78rem}.agent-transport,.agent-muted{
  color:#94a3b8;font-size:.75rem}.agent-card-fault{color:#d29922;font-size:.75rem;margin-top:.35rem}
.agent-card-meta{margin:.85rem 0;display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
.agent-card-meta div{min-width:0}.agent-card-meta dt{color:#94a3b8;font-size:.68rem;
  text-transform:uppercase;letter-spacing:.05em}
.agent-card-meta dd{margin:.18rem 0 0;overflow-wrap:anywhere}
.agent-capabilities{min-height:1.55rem}.agent-more-tools{color:#94a3b8;font-size:.7rem;margin-left:.25rem}
.agent-card-actions{display:flex;gap:.45rem;flex-wrap:wrap;margin-top:.85rem;padding-top:.8rem;
  border-top:1px solid #334155}
.agent-card-actions .action-link{padding:.32rem .58rem;font-size:.78rem}
.agent-card-empty{padding:1.5rem;text-align:center;color:#94a3b8;border:1px dashed #334155;
  border-radius:6px}@media (max-width:760px){
  .fleet-toolbar{align-items:stretch;flex-direction:column}
  .view-switch a{flex:1;text-align:center}.agent-card-grid{grid-template-columns:1fr}}
"""


AgentGroup = tuple[str, list[tuple[Agent, Persona | None]]]


def render_agent_cards(groups: list[AgentGroup], heartbeat_timeout_seconds: int) -> str:
    """Render identity-aware agent groups as capability-oriented cards."""
    if not any(rows for _title, rows in groups):
        return '<div class="agent-card-empty">No agents match this filter.</div>'
    return "".join(
        _owner_group(index, title, rows, heartbeat_timeout_seconds)
        for index, (title, rows) in enumerate(groups)
        if rows
    )


def _owner_group(
    index: int,
    title: str,
    agents: list[tuple[Agent, Persona | None]],
    heartbeat_timeout_seconds: int,
) -> str:
    group_id = f"agent-owner-group-{index}"
    cards = "".join(
        _agent_card(agent, persona, heartbeat_timeout_seconds) for agent, persona in agents
    )
    count = len(agents)
    return f"""\
<section class="agent-owner-group" aria-labelledby="{group_id}">
  <div class="agent-owner-heading"><h3 id="{group_id}">{html.escape(title)} <span>{count}
    {"agent" if count == 1 else "agents"}</span></h3></div>
  <div class="agent-card-grid">{cards}</div>
</section>"""


def _agent_card(
    agent: Agent,
    persona: Persona | None,
    heartbeat_timeout_seconds: int,
) -> str:
    label = html.escape(agent.label or agent.device_id)
    device_id = html.escape(agent.device_id)
    device_url = html.escape(quote(agent.device_id, safe=""))
    health = session_state.get_device_health(
        agent.device_id, agent.last_heartbeat, agent.health_fault, heartbeat_timeout_seconds
    )
    activity = session_state.get_device_activity(agent.device_id, agent.reported_activity)
    tools = _tool_names(agent)
    bridge = mcp_bridge.get_page_agent(agent.device_id)
    connected = (
        session_state.is_connected(agent.device_id)
        if agent.kind == AgentKind.XIAOZHI.value
        else bool(bridge and bridge.connected)
    )
    persona_name = html.escape(persona.name) if persona else "No persona"
    model = html.escape(persona.llm_model or persona.llm_provider or "") if persona else ""
    model_line = f'<span class="agent-model">{model}</span>' if model else ""
    identity_line = f'<span class="agent-device-id">{device_id}</span>' if agent.label else ""
    kept = (
        '<span class="badge badge-kind" title="Long-term: never pruned">kept</span>'
        if agent.pinned
        else ""
    )
    fault = (
        f'<div class="agent-card-fault">{html.escape(agent.health_fault)}</div>'
        if agent.health_fault and health == "degraded"
        else ""
    )
    badges = "".join(
        f'<span class="badge badge-tool">{html.escape(name)}</span>' for name in tools[:3]
    )
    more = f'<span class="agent-more-tools">+{len(tools) - 3} more</span>' if len(tools) > 3 else ""
    capabilities = badges + more or '<span class="agent-muted">No tools reported</span>'
    camera = (
        f'<a class="action-link" href="/dashboard/agents/{device_url}#device-actions">Camera</a>'
        if agent.kind == AgentKind.XIAOZHI.value
        and any("camera" in tool or "photo" in tool for tool in tools)
        else ""
    )
    interaction_label = (
        "Interact" if connected and not (persona and persona.transcription) else "Conversation"
    )
    interact = (
        f'<a class="action-link primary" href="/dashboard/agents/{device_url}/conversation" '
        f'hx-get="/dashboard/agents/{device_url}/conversation" '
        f'hx-target="#conversation-host" hx-swap="innerHTML" '
        f"data-conversation-open>{interaction_label}</a>"
    )
    state = session_state.get_state(agent.device_id)
    timing = (
        f'<div class="agent-transport">Last response prepared in '
        f"{state.last.total_ms / 1000:.1f}s "
        f"· ASR {state.last.asr_ms}ms · LLM {state.last.llm_ms}ms "
        f"· TTS {state.last.tts_ms}ms</div>"
        if state.turns
        else ""
    )
    last_seen = html.escape(fmt_ts(agent.last_seen, fmt="%H:%M:%S"))
    transport = _transport(agent, connected, len(tools))
    return f"""\
<article class="agent-card health-{health}" aria-label="{label}, {health}">
  <div class="agent-card-top">
    <span class="agent-kind-icon" aria-hidden="true">{_agent_icon(agent.kind)}</span>
    <div class="agent-card-title"><h4><a href="/dashboard/agents/{device_url}">{label}</a>
      {kept}</h4>{identity_line}</div>
    <span class="agent-health-dot" title="{health.title()}"></span>
  </div>
  <div class="agent-state-row"><strong class="status-{health}">{health.title()}</strong>
    <span>{html.escape(activity.title())}</span>
    <span class="badge badge-kind">{html.escape(_runtime_label(agent.kind))}</span></div>
  <div class="agent-transport">{html.escape(transport)}</div>{fault}
  <dl class="agent-card-meta">
    <div><dt>Persona</dt><dd>{persona_name}{model_line}</dd></div>
    <div><dt>Last seen</dt><dd>{last_seen}</dd></div>
  </dl>
  <div class="agent-capabilities">{capabilities}</div>{timing}
  <div class="agent-card-actions">{interact}{camera}
    <a class="action-link" href="/dashboard/agents/{device_url}">Manage</a></div>
</article>"""


def _tool_names(agent: Agent) -> list[str]:
    if agent.kind != AgentKind.XIAOZHI.value:
        bridge = mcp_bridge.get_page_agent(agent.device_id)
        if bridge is not None:
            return list(bridge.tools)
    state_tools = session_state.get_state(agent.device_id).mcp_tools
    return list(state_tools) if state_tools else agent.reported_mcp_tools_list


def _transport(agent: Agent, connected: bool, tool_count: int) -> str:
    if agent.kind == AgentKind.XIAOZHI.value:
        return f"Voice connected · {tool_count} MCP tools" if connected else "Wake-word standby"
    return f"Connected · {tool_count} tools" if connected else "Not connected"


def _runtime_label(kind: str) -> str:
    return {
        AgentKind.XIAOZHI.value: "Device runtime",
        AgentKind.PAGE.value: "Browser runtime",
        AgentKind.MCP.value: "MCP runtime",
        AgentKind.VOICE.value: "Voice runtime",
        AgentKind.AG2.value: "AG2 runtime",
    }.get(kind, "Agent runtime")


def _agent_icon(kind: str) -> str:
    if kind in {AgentKind.XIAOZHI.value, AgentKind.VOICE.value}:
        return (
            '<svg viewBox="0 0 24 24"><rect x="7" y="3" width="10" height="13" rx="5"/>'
            '<path d="M5 11a7 7 0 0 0 14 0M12 18v3M9 21h6"/></svg>'
        )
    return (
        '<svg viewBox="0 0 24 24"><rect x="4" y="7" width="16" height="13" rx="3"/>'
        '<path d="M9 7V4h6v3M8 12h.01M16 12h.01M8 16h8"/></svg>'
    )
