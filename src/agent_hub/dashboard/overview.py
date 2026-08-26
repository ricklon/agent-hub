"""Action-oriented fleet overview for the dashboard home page."""

from __future__ import annotations

import html
from collections import Counter
from urllib.parse import quote

from agent_hub.registry.models import Agent, Persona
from agent_hub.server import session_state


def render_fleet_overview(
    agents: list[tuple[Agent, Persona | None]],
    heartbeat_timeout_seconds: int,
) -> str:
    """Render fleet totals, an attention queue, or first-device guidance."""
    if not agents:
        return _empty_fleet_state()

    health_by_device: dict[str, session_state.DeviceHealth] = {
        agent.device_id: session_state.get_device_health(
            agent.device_id,
            agent.last_heartbeat,
            agent.health_fault,
            heartbeat_timeout_seconds,
        )
        for agent, _persona in agents
    }
    counts = Counter(health_by_device.values())
    attention = [
        agent
        for agent, _persona in agents
        if health_by_device[agent.device_id] in {"degraded", "offline"}
    ]
    cards = "".join(
        _summary_card(label, value, css_class)
        for label, value, css_class in (
            ("Total agents", len(agents), ""),
            ("Healthy", counts["healthy"], "overview-good"),
            ("Degraded", counts["degraded"], "overview-warn"),
            ("Offline", counts["offline"], "overview-muted"),
        )
    )
    return f"""\
<section aria-labelledby="fleet-health-heading">
  <div class="section-heading">
    <div><h2 id="fleet-health-heading">Fleet health</h2>
      <p class="doc-muted">Current heartbeat health across registered agents.</p></div>
    <a class="action-link" href="/dashboard/personas">Manage personas</a>
  </div>
  <div class="overview-grid">{cards}</div>
</section>
{_attention_panel(attention, health_by_device)}"""


def _summary_card(label: str, value: int, css_class: str) -> str:
    return f"""\
<div class="overview-card {css_class}">
  <span class="overview-value">{value}</span>
  <span class="overview-label">{label}</span>
</div>"""


def _attention_panel(
    agents: list[Agent],
    health_by_device: dict[str, session_state.DeviceHealth],
) -> str:
    if not agents:
        return """\
<section class="attention-clear" aria-label="Fleet attention status">
  <strong>✓ Nothing needs attention</strong>
  <span>All registered agents have a current, fault-free heartbeat.</span>
</section>"""

    rows = "".join(_attention_row(agent, health_by_device[agent.device_id]) for agent in agents)
    return f"""\
<section class="attention-panel" aria-labelledby="attention-heading">
  <div class="section-heading"><div>
    <h2 id="attention-heading">Needs attention
      <span class="attention-count">{len(agents)}</span></h2>
    <p class="doc-muted">Start here before troubleshooting individual conversations.</p>
  </div></div>
  <div class="attention-list">{rows}</div>
</section>"""


def _attention_row(agent: Agent, health: session_state.DeviceHealth) -> str:
    label = html.escape(agent.label or agent.device_id)
    device_id = html.escape(quote(agent.device_id, safe=""))
    if health == "degraded":
        detail = html.escape(agent.health_fault or "Device reported a fault")
    else:
        last_seen = agent.last_seen.strftime("%Y-%m-%d %H:%M") if agent.last_seen else "never"
        detail = f"No recent heartbeat · last seen {last_seen}"
    return f"""\
<div class="attention-item">
  <div><strong>{label}</strong>
    <span class="attention-status status-{health}">{health.title()}</span>
    <div class="attention-detail">{detail}</div>
  </div>
  <a class="action-link" href="/dashboard/agents/{device_id}">Inspect</a>
</div>"""


def _empty_fleet_state() -> str:
    return """\
<section class="empty-state" aria-labelledby="empty-fleet-heading">
  <div class="empty-icon">◎</div>
  <h2 id="empty-fleet-heading">Connect your first agent</h2>
  <p>Power on a configured device and let it check in. Agent Hub will register it,
  assign <code>hub-default</code>, and show it here automatically.</p>
  <div class="empty-actions">
    <a class="action-link primary" href="/dashboard/docs">Open setup guide</a>
    <a class="action-link" href="/dashboard/personas">Prepare a persona</a>
  </div>
</section>"""
