"""Dashboard: agent list + OpenRouter model picker.

Server-rendered with HTMX — no SPA build step.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from loguru import logger

from agent_hub.registry.store import RegistryStore
from agent_hub.server import session_state

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_CSS = """\
body{font-family:monospace;padding:2rem;background:#0d1117;color:#c9d1d9;margin:0}
h1{color:#58a6ff;margin-bottom:0.25rem}
nav{margin-bottom:2rem}
nav a{color:#58a6ff;margin-right:1.5rem;text-decoration:none}
nav a:hover{text-decoration:underline}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid #30363d;padding:0.5rem 0.75rem;text-align:left;vertical-align:top}
th{background:#161b22;white-space:nowrap}
tr:hover td{background:#161b22}
.badge{font-size:0.68rem;padding:0.1rem 0.35rem;border-radius:3px;margin:0.1rem 0.1rem 0 0;display:inline-block}
.badge-multi{background:#1f4a2e;color:#3fb950}
.badge-free{background:#2d1f6e;color:#a5a0ff}
.badge-tool{background:#1a2a3a;color:#79c0ff}
.badge-skill{background:#2a1a3a;color:#d2a8ff}
.status-active{color:#3fb950}
.status-idle{color:#d29922}
.status-offline{color:#6e7681}
.status-discovered{color:#58a6ff}
.lat{font-size:0.75rem;color:#8b949e}
.lat span{color:#c9d1d9}
.model{font-size:0.75rem;color:#8b949e;display:block;margin-top:0.15rem}
input,select{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:0.4rem 0.6rem;border-radius:4px;margin-right:0.5rem}
button{background:#238636;color:#fff;border:none;padding:0.4rem 0.9rem;border-radius:4px;cursor:pointer}
button:hover{background:#2ea043}
button.selected{background:#1f4a2e;color:#3fb950;border:1px solid #3fb950}
.msg{color:#3fb950;margin-top:0.5rem}
.controls{display:flex;align-items:center;flex-wrap:wrap;gap:0.5rem;margin-bottom:1rem}
"""

_CSS_EXTRA = """\
textarea{background:#161b22;color:#c9d1d9;border:1px solid #30363d;padding:0.4rem 0.6rem;
  border-radius:4px;width:100%;box-sizing:border-box;font-family:monospace;resize:vertical}
label{display:block;color:#8b949e;font-size:0.8rem;margin-top:0.75rem;margin-bottom:0.2rem}
.field-row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.form-section{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:1.25rem;margin-bottom:1.5rem}
.form-section h3{margin:0 0 1rem;color:#58a6ff}
input[type=number]{width:6rem}
"""

_PAGE = """\
<!doctype html><html><head>
<meta charset="utf-8"><title>agent-hub</title>
<style>{css}</style>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
</head><body>
<h1>agent-hub</h1>
<nav>
  <a href="/dashboard/">Agents</a>
  <a href="/dashboard/personas">Personas</a>
  <a href="/dashboard/models">Models</a>
</nav>
{body}
</body></html>
"""


def make_router(store: RegistryStore, config: dict[str, Any]) -> APIRouter:
    router = APIRouter()
    api_key: str = config.get("llm", {}).get("openai", {}).get("api_key", "")

    # ── Agents ───────────────────────────────────────────────────────────────

    _full_css = _CSS + _CSS_EXTRA

    @router.get("/dashboard/", response_class=HTMLResponse)
    async def dashboard_index(request: Request) -> HTMLResponse:
        rows = await _render_agent_rows(store)
        body = _agent_table(rows)
        return HTMLResponse(_PAGE.format(css=_full_css, body=body))

    @router.get("/dashboard/agents", response_class=HTMLResponse)
    async def dashboard_agents_partial(request: Request) -> HTMLResponse:
        rows = await _render_agent_rows(store)
        return HTMLResponse(_agent_table(rows))

    # ── Agent detail ─────────────────────────────────────────────────────────

    @router.get("/dashboard/agents/{device_id}/history", response_class=HTMLResponse)
    async def agent_history_partial(device_id: str) -> HTMLResponse:
        turns = await store.load_history(device_id, limit=60)
        if not turns:
            return HTMLResponse('<p style="color:#6e7681">No history yet.</p>')
        rows = "".join(
            f'<tr>'
            f'<td style="color:#8b949e;white-space:nowrap;font-size:0.75rem">'
            f'{t.get("created_at","")[:19].replace("T"," ")}</td>'
            f'<td style="color:{"#79c0ff" if t["role"]=="user" else "#3fb950"};'
            f'white-space:nowrap">{t["role"]}</td>'
            f'<td style="white-space:pre-wrap;max-width:600px">{t["content"]}</td></tr>'
            for t in turns
        )
        return HTMLResponse(
            f'<table style="width:100%"><thead><tr><th>time</th><th>role</th><th>content</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<p style="color:#8b949e;font-size:0.8rem">{len(turns)} messages</p>'
        )

    @router.post("/dashboard/agents/{device_id}/assign_persona", response_class=HTMLResponse)
    async def agent_assign_persona(device_id: str, persona_name: str = Form(...)) -> HTMLResponse:
        ok = await store.assign_persona(device_id, persona_name)
        if not ok:
            return HTMLResponse(f'<p style="color:#f85149">Assignment failed — persona or device not found.</p>')
        return HTMLResponse(f'<p class="msg">✓ Assigned <strong>{persona_name}</strong>. Takes effect on next voice session.</p>')

    @router.get("/dashboard/agents/{device_id}", response_class=HTMLResponse)
    async def agent_detail(device_id: str, request: Request) -> HTMLResponse:
        agent = await store.get_agent(device_id)
        if agent is None:
            return HTMLResponse(_PAGE.format(css=_full_css, body="<p>Agent not found.</p>"))
        persona = await store.get_persona_for_device(device_id)
        all_personas = await store.list_personas()
        dev = session_state.get_state(device_id)
        connected = session_state.is_connected(device_id)

        status_class = f"status-{agent.status}"
        conn_badge = (
            '<span style="color:#3fb950">● connected</span>'
            if connected else
            '<span style="color:#6e7681">○ offline</span>'
        )

        # Persona section
        persona_options = "".join(
            f'<option value="{p.name}" {"selected" if persona and p.name == persona.name else ""}>'
            f'{p.name}</option>'
            for p in all_personas
        )
        assign_form = f"""\
<form hx-post="/dashboard/agents/{device_id}/assign_persona"
      hx-target="#assign-result" hx-swap="innerHTML" style="display:inline-flex;gap:0.5rem;align-items:center">
  <select name="persona_name">{persona_options}</select>
  <button type="submit">Assign</button>
</form>
<span id="assign-result" style="margin-left:0.5rem"></span>"""
        if persona:
            model_str = (
                persona.llm_model
                or config.get("llm", {}).get("openai", {}).get("model", "")
                or f"{persona.llm_provider} default"
            )
            base_url = config.get("llm", {}).get("openai", {}).get("base_url", "")
            provider_detail = f"{persona.llm_provider}"
            if base_url:
                provider_detail += f' <span style="color:#8b949e;font-size:0.75rem">({base_url})</span>'
            persona_html = f"""\
<h3>Persona</h3>
{assign_form}
<table style="width:auto;margin-top:0.75rem">
  <tr><th>name</th><td>{persona.name} &nbsp;<a href="/dashboard/personas/{persona.name}" style="color:#58a6ff;font-size:0.8rem">edit →</a></td></tr>
  <tr><th>model</th><td>{model_str}</td></tr>
  <tr><th>LLM provider</th><td>{provider_detail}</td></tr>
  <tr><th>TTS provider</th><td>{persona.tts_provider}{f" / {persona.tts_voice}" if persona.tts_voice else ""}</td></tr>
  <tr><th>ASR provider</th><td>{persona.asr_provider}</td></tr>
  <tr><th>system prompt</th><td style="white-space:pre-wrap;max-width:600px">{persona.system_prompt or "—"}</td></tr>
</table>"""
        else:
            persona_html = f"<h3>Persona</h3><p>No persona assigned.</p>{assign_form}"

        # Tools section
        import agent_hub.skills as _skills
        device_tool_badges = "".join(
            f'<span class="badge badge-tool">{t}</span>' for t in dev.mcp_tools
        ) or '<span style="color:#6e7681">none discovered yet</span>'
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
  <tr><td><strong>total</strong></td><td><strong>{L.total_ms} ms</strong></td><td><strong>{A.total_ms} ms</strong></td></tr>
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
<div id="capture-result" style="margin-top:0.75rem"></div>"""

        # Reboot + send message
        speak_form = f"""\
<form hx-post="/dashboard/agents/{device_id}/reboot"
      hx-target="#reboot-result" hx-swap="innerHTML" style="display:inline">
  <button type="submit" style="background:#6e3a1e">↺ Reboot device</button>
</form>
{camera_btn}
<span id="reboot-result" style="margin-left:0.75rem"></span>
<h3>Send message to device</h3>
<form hx-post="/dashboard/agents/{device_id}/speak"
      hx-target="#speak-result" hx-swap="innerHTML">
  <input type="text" name="text" placeholder="Say something..." style="width:400px">
  <button type="submit">Speak</button>
</form>
<div id="speak-result"></div>
<h3>Conversation history</h3>
<div hx-get="/dashboard/agents/{device_id}/history"
     hx-trigger="load, every 5s"
     hx-swap="innerHTML"
     id="history-view">Loading…</div>
<form hx-post="/dashboard/agents/{device_id}/clear_history"
      hx-target="#history-view" hx-swap="innerHTML"
      hx-confirm="Clear all conversation history for this device?"
      style="margin-top:0.5rem">
  <button type="submit" style="background:#b62324">Clear history</button>
</form>"""

        body = f"""\
<p><a href="/dashboard/" style="color:#58a6ff">← agents</a></p>
<h2>{device_id} <span class="{status_class}" style="font-size:1rem">{agent.status}</span>
  &nbsp;{conn_badge}</h2>
<p style="color:#8b949e">
  IP: {agent.ip_address or "—"} &nbsp;·&nbsp;
  Firmware: {agent.firmware_version or "—"} &nbsp;·&nbsp;
  Last seen: {agent.last_seen.strftime("%H:%M:%S") if agent.last_seen else "—"}
</p>
{persona_html}
<h3>Device MCP tools</h3>
<div>{device_tool_badges}</div>
<h3>Server skills</h3>
<div>{skill_badges}</div>
<h3>Latency</h3>
{lat_html}
{speak_form}"""
        return HTMLResponse(_PAGE.format(css=_full_css, body=body))

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
        import glob
        import asyncio as _asyncio
        ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
        if not ports:
            return HTMLResponse('<p style="color:#f85149">No serial port found and device not connected.</p>')
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
            return HTMLResponse('<p style="color:#f85149">Device not connected or MCP not ready.</p>')
        if not any("camera" in t or "photo" in t for t in mcp_client.tools):
            return HTMLResponse('<p style="color:#f85149">No camera tool available on this device.</p>')
        try:
            result = await mcp_client.call_tool(
                "self_camera_take_photo",
                {"question": "Describe what you see in detail."},
                timeout=60.0,
            )
            if isinstance(result, str) and result.startswith("data:"):
                return HTMLResponse(
                    f'<img src="{result}" style="max-width:100%;border-radius:6px;margin-top:0.5rem">'
                    f'<p style="color:#8b949e;font-size:0.8rem">Captured</p>'
                )
            return HTMLResponse(f'<p style="color:#c9d1d9">{result}</p>')
        except Exception as exc:
            return HTMLResponse(f'<p style="color:#f85149">Capture failed: {exc}</p>')

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
        rows = "".join(
            f'<tr><td><a href="/dashboard/personas/{p.name}" style="color:#58a6ff">{p.name}</a></td>'
            f'<td>{p.llm_provider} / {p.llm_model or "default"}</td>'
            f'<td>{p.tts_provider}{f" / {p.tts_voice}" if p.tts_voice else ""}</td>'
            f'<td>{p.asr_provider}</td>'
            f'<td>{p.memory_window}</td>'
            f'<td><a href="/dashboard/personas/{p.name}" style="color:#58a6ff">edit</a></td></tr>'
            for p in personas
        ) or "<tr><td colspan=6>no personas</td></tr>"
        body = f"""\
<h2>Personas</h2>
<table>
<thead><tr>
  <th>name</th><th>LLM</th><th>TTS</th><th>ASR</th><th>memory</th><th></th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<h3 style="margin-top:2rem">New persona</h3>
<div id="new-persona-result"></div>
<form hx-post="/dashboard/personas"
      hx-target="#new-persona-result" hx-swap="innerHTML">
  <div class="field-row">
    <div>
      <label>Name</label>
      <input type="text" name="name" required placeholder="e.g. grumpy-pirate">
    </div>
    <div>
      <label>TTS voice</label>
      <input type="text" name="tts_voice" placeholder="e.g. en-GB-RyanNeural">
    </div>
    <div>
      <label>LLM model (blank = default)</label>
      <input type="text" name="llm_model" placeholder="">
    </div>
  </div>
  <label>System prompt</label>
  <textarea name="system_prompt" rows="4"
    placeholder="You are a helpful voice assistant..."></textarea>
  <button type="submit">Create</button>
</form>"""
        return HTMLResponse(_PAGE.format(css=_full_css, body=body))

    @router.post("/dashboard/personas", response_class=HTMLResponse)
    async def persona_create(
        name: str = Form(...),
        system_prompt: str = Form(default=""),
        tts_voice: str = Form(default=""),
        llm_model: str = Form(default=""),
    ) -> HTMLResponse:
        persona = await store.create_persona(
            name,
            system_prompt=system_prompt,
            tts_voice=tts_voice or None,
            llm_model=llm_model or None,
        )
        if persona is None:
            return HTMLResponse(f'<p style="color:#f85149">Name \'{name}\' already taken.</p>')
        return HTMLResponse(
            f'<p class="msg">✓ Created. <a href="/dashboard/personas/{persona.name}" '
            f'style="color:#58a6ff">Edit {persona.name} →</a></p>'
        )

    @router.get("/dashboard/personas/{name}", response_class=HTMLResponse)
    async def persona_edit_page(name: str, request: Request) -> HTMLResponse:
        import agent_hub.skills as _skills
        persona = await store.get_persona_by_name(name)
        if persona is None:
            return HTMLResponse(_PAGE.format(css=_full_css, body="<p>Persona not found.</p>"))

        all_skills = [d["function"]["name"] for d in _skills.get_definitions()]
        enabled = persona.server_skills_list  # None = all
        skills_val = ", ".join(enabled) if enabled is not None else ""

        allowed_tools = persona.mcp_tools_allowlist_list  # None = all
        tools_val = ", ".join(allowed_tools) if allowed_tools is not None else ""

        body = f"""\
<p><a href="/dashboard/personas" style="color:#58a6ff">← personas</a></p>
<h2>Edit persona: {name}</h2>
<div id="save-result"></div>
<form hx-post="/dashboard/personas/{name}"
      hx-target="#save-result" hx-swap="innerHTML">

  <div class="form-section">
    <h3>Prompt</h3>
    <label>System prompt</label>
    <textarea name="system_prompt" rows="6">{persona.system_prompt or ""}</textarea>
  </div>

  <div class="form-section">
    <h3>Providers</h3>
    <div class="field-row">
      <div>
        <label>LLM provider</label>
        <input type="text" name="llm_provider" value="{persona.llm_provider}">
      </div>
      <div>
        <label>LLM model (blank = config default)</label>
        <input type="text" name="llm_model" value="{persona.llm_model or ""}" style="width:300px">
      </div>
    </div>
    <div class="field-row">
      <div>
        <label>TTS provider</label>
        <input type="text" name="tts_provider" value="{persona.tts_provider}">
      </div>
      <div>
        <label>TTS voice (blank = provider default)</label>
        <input type="text" name="tts_voice" value="{persona.tts_voice or ""}" style="width:300px">
      </div>
    </div>
    <div class="field-row">
      <div>
        <label>ASR provider</label>
        <input type="text" name="asr_provider" value="{persona.asr_provider}">
      </div>
    </div>
  </div>

  <div class="form-section">
    <h3>Skills &amp; tools</h3>
    <label>Server skills (comma-separated; blank = all enabled)
      <span style="color:#6e7681"> — available: {", ".join(all_skills) or "none"}</span>
    </label>
    <input type="text" name="server_skills" value="{skills_val}" style="width:100%">
    <label>Device MCP tool allowlist (comma-separated; blank = all allowed)</label>
    <input type="text" name="mcp_tools_allowlist" value="{tools_val}" style="width:100%">
  </div>

  <div class="form-section">
    <h3>Memory</h3>
    <label>Conversation window (turns kept in LLM context)</label>
    <input type="number" name="memory_window" value="{persona.memory_window}" min="1" max="200">
  </div>

  <button type="submit">Save</button>
</form>"""
        return HTMLResponse(_PAGE.format(css=_full_css, body=body))

    @router.post("/dashboard/personas/{name}", response_class=HTMLResponse)
    async def persona_save(
        name: str,
        system_prompt: str = Form(default=""),
        llm_provider: str = Form(default=""),
        llm_model: str = Form(default=""),
        tts_provider: str = Form(default=""),
        tts_voice: str = Form(default=""),
        asr_provider: str = Form(default=""),
        server_skills: str = Form(default=""),
        mcp_tools_allowlist: str = Form(default=""),
        memory_window: int = Form(default=20),
    ) -> HTMLResponse:
        import json as _json

        def _to_json_list(raw: str) -> str | None:
            parts = [s.strip() for s in raw.split(",") if s.strip()]
            return _json.dumps(parts) if parts else None

        ok = await store.update_persona(
            name,
            system_prompt=system_prompt,
            llm_provider=llm_provider or None,
            llm_model=llm_model,
            tts_provider=tts_provider or None,
            tts_voice=tts_voice,
            asr_provider=asr_provider or None,
            server_skills=_to_json_list(server_skills),
            mcp_tools_allowlist=_to_json_list(mcp_tools_allowlist),
            memory_window=max(1, memory_window),
        )
        if ok:
            logger.info(f"Persona '{name}' updated via dashboard")
            return HTMLResponse('<p class="msg">✓ Saved.</p>')
        return HTMLResponse(f'<p style="color:#f85149">Persona \'{name}\' not found.</p>')

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
        return HTMLResponse(_PAGE.format(css=_full_css, body=body))

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
            m for m in models
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

def _agent_table(rows: str) -> str:
    return f"""\
<div hx-get="/dashboard/agents" hx-trigger="every 5s" hx-swap="outerHTML">
<table>
<thead><tr>
  <th>device-id</th><th>status</th><th>persona / model</th>
  <th>tools</th><th>latency (last / avg)</th>
  <th>ip</th><th>fw</th><th>last seen</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</div>"""


async def _render_agent_rows(store: RegistryStore) -> str:
    try:
        rows_data = await store.list_agents_with_personas()
    except Exception as exc:
        logger.error(f"Dashboard agent query failed: {exc}")
        return "<tr><td colspan=8>error loading agents</td></tr>"

    if not rows_data:
        return "<tr><td colspan=8>no agents registered yet</td></tr>"

    import agent_hub.skills as server_skills  # local import avoids circular at module level

    skill_names = [d["function"]["name"] for d in server_skills.get_definitions()]

    rows = []
    for agent, persona in rows_data:
        last_seen = agent.last_seen.strftime("%H:%M:%S") if agent.last_seen else "—"
        dev = session_state.get_state(agent.device_id)

        # Persona / model cell
        persona_name = persona.name if persona else "—"
        model = (persona.llm_model or "") if persona else ""
        if not model and persona:
            model = persona.llm_provider or ""
        model_line = f'<span class="model">{model}</span>' if model else ""

        # Tools cell — device MCP badges + skill badges
        tool_badges = "".join(
            f'<span class="badge badge-tool">{t}</span>'
            for t in dev.mcp_tools
        )
        skill_badges = "".join(
            f'<span class="badge badge-skill">{s}</span>'
            for s in skill_names
        )
        tools_cell = (tool_badges + skill_badges) or '<span style="color:#6e7681">—</span>'

        # Latency cell
        if dev.turns > 0:
            L, A = dev.last, dev.avg
            lat_cell = (
                f'<div class="lat">ASR <span>{L.asr_ms}ms</span> / '
                f'LLM <span>{L.llm_ms}ms</span> / '
                f'TTS <span>{L.tts_ms}ms</span></div>'
                f'<div class="lat">avg <span>{A.asr_ms}</span>/'
                f'<span>{A.llm_ms}</span>/<span>{A.tts_ms}</span>ms '
                f'· {dev.turns} turns</div>'
            )
        else:
            lat_cell = '<span style="color:#6e7681">—</span>'

        rows.append(f"""\
<tr>
  <td><a href="/dashboard/agents/{agent.device_id}" style="color:#58a6ff">{agent.device_id}</a></td>
  <td class="status-{agent.status}">{agent.status}</td>
  <td>{persona_name}{model_line}</td>
  <td>{tools_cell}</td>
  <td>{lat_cell}</td>
  <td>{agent.ip_address or "—"}</td>
  <td>{agent.firmware_version or "—"}</td>
  <td>{last_seen}</td>
</tr>""")
    return "".join(rows)


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
        out.append({
            "id": m.get("id", ""),
            "name": m.get("name", ""),
            "context_k": ctx // 1000 if ctx else "—",
            "price_in": price_str,
            "multimodal": multimodal,
            "free": free,
        })

    out.sort(key=lambda x: (not x["multimodal"], x["id"]))
    return out
