"""Capability-aware conversations opened alongside the agent fleet."""

from __future__ import annotations

import asyncio
import html
from typing import Any
from urllib.parse import quote
from weakref import WeakValueDictionary

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from agent_hub.providers.tts import get_provider
from agent_hub.registry.models import Agent, AgentKind, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge, session_state
from agent_hub.server.agent_turn import call_one_tool, run_turn
from agent_hub.server.audio import pcm_to_wav
from agent_hub.server.persona_voice import VOICE_TEST_TEXT, synthesize_persona

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def speech_tool(device_id: str) -> str | None:
    """Find an advertised speak tool accepting text without other required inputs."""
    handle = mcp_bridge.get_page_agent(device_id)
    if not handle:
        return None
    for name, tool in sorted(handle.tools.items()):
        schema = tool.get("inputSchema", {})
        text = schema.get("properties", {}).get("text", {})
        if (
            name.endswith(".speak")
            and text.get("type") == "string"
            and set(schema.get("required", [])) <= {"text"}
        ):
            return name
    return None


def connected(agent: Agent) -> bool:
    """Report the transport used by this agent, independently of heartbeat health."""
    if agent.kind == AgentKind.XIAOZHI.value:
        return session_state.is_connected(agent.device_id)
    handle = mcp_bridge.get_page_agent(agent.device_id)
    return bool(handle and handle.connected)


def destination(agent: Agent) -> str:
    """Name the actual output rather than assuming audio plays on the dashboard."""
    if agent.kind == AgentKind.XIAOZHI.value:
        return "Device speaker"
    if agent.kind == AgentKind.PAGE.value:
        return "Agent browser tab"
    return "Agent speech tool (voice managed externally)"


async def _speak(agent: Agent, text: str) -> str:
    if agent.kind == AgentKind.XIAOZHI.value:
        speak = session_state.get_speak(agent.device_id)
        if speak is None:
            raise RuntimeError("Device is disconnected. Wake it and try again.")
        await speak(text)
        return "Audio sent to the device speaker."
    tool = speech_tool(agent.device_id)
    if not tool:
        raise RuntimeError("This agent does not advertise a compatible speech tool.")
    args: dict[str, Any] = {"text": text}
    if agent.kind == AgentKind.PAGE.value:
        handle = mcp_bridge.get_page_agent(agent.device_id)
        schema = handle.tools[tool]["inputSchema"] if handle else {}
        if "voice_mode" not in schema.get("properties", {}):
            raise RuntimeError("Reload the agent browser tab to enable persona voice routing.")
        args["voice_mode"] = "hub"
    return await call_one_tool(agent.device_id, tool, args)


def _feedback(message: str, *, error: bool = False) -> HTMLResponse:
    css = "voice-warning" if error else "msg"
    return HTMLResponse(f'<p class="{css}" role="status">{html.escape(message)}</p>')


def make_conversation_router(store: RegistryStore, config: dict[str, Any]) -> APIRouter:
    """Build panel routes; the parent dashboard supplies authentication and CSRF checks."""
    router = APIRouter()

    async def lookup(device_id: str) -> tuple[Agent, Persona | None]:
        agent = await store.get_agent(device_id)
        if agent is None:
            raise HTTPException(404, "Agent not found")
        return agent, await store.get_persona_for_device(device_id)

    @router.get("/dashboard/agents/{device_id}/conversation", response_class=HTMLResponse)
    async def panel(device_id: str, request: Request) -> HTMLResponse:
        """Open an agent without navigating away from the fleet."""
        agent, persona = await lookup(device_id)
        url = f"/dashboard/agents/{quote(device_id, safe='')}"
        label = html.escape(agent.label or device_id)
        viewer = getattr(request.state, "operator_role", "admin") == "viewer"
        transcriber = bool(persona and persona.transcription)
        device = agent.kind == AgentKind.XIAOZHI.value
        can_speak = device or speech_tool(device_id) is not None
        controls = ""
        if not viewer and persona and not transcriber:
            output = html.escape(destination(agent))
            speak_option = (
                f'<label><input type="checkbox" name="spoken" value="1" '
                f"{'checked' if agent.kind == AgentKind.PAGE.value else ''}> "
                f"Speak reply · {output}</label>"
                if can_speak and not device
                else ""
            )
            controls = f"""
<form hx-post="{url}/conversation/send" hx-target="#conversation-result"
      hx-disabled-elt="find button" hx-sync="this:drop">
  <label for="conversation-message">Message to {label}</label>
  <textarea id="conversation-message" name="text" rows="3" required maxlength="4000"
    placeholder="Ask this agent…"></textarea>{speak_option}
  <button type="submit">{"Send · device speaker" if device else "Send message"}</button>
</form>
<div class="conversation-tests">
  <p class="doc-muted">Voice comparison · same sentence, same persona</p>
  <button type="button" data-voice-preview="{url}/conversation/preview">
    Test in this browser</button>
"""
            if can_speak:
                controls += (
                    f'<button hx-post="{url}/conversation/voice-test" '
                    f'hx-target="#conversation-result">Test on {output}</button>'
                )
            controls += '<div id="voice-preview" role="status"></div></div>'
        elif transcriber:
            controls = (
                "<p>This persona transcribes only; replies and speech tests are disabled.</p>"
            )
        elif viewer:
            controls = "<p>Read-only access. An operator can send messages and test voice.</p>"
        guidance = (
            "Talk using the device’s onboard microphone. Replies play on its speaker. "
            "If disconnected, wake the device before sending."
            if device
            else "Use Listen in the agent tab. Its microphone and speaker stay in that tab."
            if agent.kind == AgentKind.PAGE.value
            else "MCP tools determine this agent’s capabilities. Its microphone and voice, if any, "
            "are managed by the agent."
        )
        return HTMLResponse(f"""
<aside id="conversation-panel" aria-labelledby="conversation-title" tabindex="-1">
  <header><div><p class="doc-muted">Conversation</p><h2 id="conversation-title">{label}</h2></div>
    <button type="button" data-close-conversation aria-label="Close conversation">✕</button>
  </header>
  <p>{guidance}</p><a href="{url}">Manage agent</a>
  <div id="conversation-live" hx-get="{url}/conversation/live"
    hx-trigger="load, every 2s" hx-swap="innerHTML"></div>
  {controls}
  <div id="conversation-result" role="status" aria-live="polite"></div>
</aside>""")

    @router.get("/dashboard/agents/{device_id}/conversation/live", response_class=HTMLResponse)
    async def live(device_id: str) -> HTMLResponse:
        """Refresh status and transcript without replacing an unsent message."""
        agent, persona = await lookup(device_id)
        state = session_state.get_state(device_id)
        activity = session_state.get_device_activity(device_id, agent.reported_activity)
        voice = (
            f"{persona.tts_provider} · {persona.tts_voice or 'provider default'}"
            if persona
            else "No persona"
        )
        notice = (
            f'<p class="voice-warning">{html.escape(state.voice_notice)}</p>'
            if state.voice_notice
            else ""
        )
        first = (
            f"First audio sent: {state.first_audio_ms}ms"
            if state.first_audio_ms is not None
            else "First audio: not measured yet"
        )
        timing = (
            f"ASR {state.last.asr_ms}ms · LLM {state.last.llm_ms}ms · TTS {state.last.tts_ms}ms"
            if state.turns
            else "No completed turn timings yet"
        )
        history = await store.load_history(device_id, limit=30)
        transcript = (
            "".join(
                '<div class="conversation-message"><strong>'
                f"{html.escape(str(row['role']))}</strong>"
                f"<p>{html.escape(str(row['content']))}</p></div>"
                for row in history
            )
            or '<p class="doc-muted">No conversation yet.</p>'
        )
        return HTMLResponse(f"""
<div class="conversation-status">
  <strong>{"Connected" if connected(agent) else "Disconnected"} ·
    {html.escape(activity.title())}</strong>
  <p>Persona: {html.escape(persona.name) if persona else "None"}<br>
  Voice: {html.escape(voice)}</p>{notice}
  <details data-live-detail="timing"><summary>Response timing</summary>
    <small>{first}<br>{timing}<br>Server timings exclude network and speaker delay.
    Streaming stages may overlap.</small></details>
</div>
<details open data-live-detail="history"><summary>Recent conversation</summary>
  <div class="conversation-history">{transcript}</div></details>""")

    async def perform(device_id: str, text: str, *, test: bool, spoken: bool) -> HTMLResponse:
        agent, persona = await lookup(device_id)
        if not persona or persona.transcription:
            return _feedback("This agent has no conversational persona.", error=True)
        if not connected(agent):
            return _feedback(
                "Agent disconnected. Wake the device or open its agent tab.", error=True
            )
        if not text.strip():
            return _feedback("Enter a message first.", error=True)
        if len(text) > 4000:
            return _feedback("Keep messages under 4,000 characters.", error=True)
        lock = _locks.setdefault(device_id, asyncio.Lock())
        phase, _ = session_state.get_pipeline_status(device_id)
        activity = session_state.get_device_activity(device_id, agent.reported_activity)
        if (
            lock.locked()
            or phase in {"thinking", "speaking", "transcribing"}
            or activity == "speaking"
        ):
            return _feedback("Agent is busy. Wait for the current reply.", error=True)
        async with lock:
            try:
                async with asyncio.timeout(90):
                    if test:
                        result = await _speak(agent, VOICE_TEST_TEXT)
                        return _feedback(f"{destination(agent)}: {result}")
                    if agent.kind == AgentKind.XIAOZHI.value:
                        injector = session_state.get_injector(device_id)
                        if injector is None:
                            raise RuntimeError(
                                "Device conversation is not ready. Wake it and retry."
                            )
                        reply, _ = await injector(text.strip())
                    else:
                        result_turn = await run_turn(store, config, device_id, text.strip())
                        reply = result_turn.reply
                        if (
                            spoken
                            and reply
                            and speech_tool(device_id) not in result_turn.tools_called
                        ):
                            try:
                                await _speak(agent, reply)
                            except Exception as exc:
                                return _feedback(f"{reply}\nSpeech failed: {exc}", error=True)
                    return _feedback(reply or "No reply was returned.")
            except TimeoutError:
                return _feedback(
                    "The agent timed out. Check its connection before retrying.", error=True
                )
            except Exception as exc:
                return _feedback(str(exc), error=True)

    @router.post("/dashboard/agents/{device_id}/conversation/send", response_class=HTMLResponse)
    async def send(device_id: str, text: str = Form(""), spoken: str = Form("")) -> HTMLResponse:
        """Send text through the selected agent and optionally speak on that agent."""
        return await perform(device_id, text, test=False, spoken=spoken == "1")

    @router.post(
        "/dashboard/agents/{device_id}/conversation/voice-test", response_class=HTMLResponse
    )
    async def voice_test(device_id: str) -> HTMLResponse:
        """Play the comparison sentence on the selected agent."""
        return await perform(device_id, VOICE_TEST_TEXT, test=True, spoken=True)

    @router.post("/dashboard/agents/{device_id}/conversation/preview")
    async def preview(device_id: str) -> Response:
        """Render the selected persona in the operator browser without rerouting the agent."""
        _, persona = await lookup(device_id)
        if not persona or persona.transcription:
            raise HTTPException(409, "No conversational voice is assigned")
        try:
            async with asyncio.timeout(30):
                pcm, rate = await synthesize_persona(
                    get_provider(persona.tts_provider, config), VOICE_TEST_TEXT, persona, device_id
                )
        except Exception as exc:
            raise HTTPException(502, "Voice preview failed. Check the persona provider.") from exc
        return Response(
            pcm_to_wav(pcm, rate),
            media_type="audio/wav",
            headers={
                "X-Voice-Notice": session_state.get_state(device_id).voice_notice,
                "Cache-Control": "no-store",
            },
        )

    return router
