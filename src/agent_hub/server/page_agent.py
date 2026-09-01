"""Page agent: a browser page that acts as a talking + seeing MCP agent.

The page is served from the dashboard port at ``/dashboard/page-agent``. On
load it POSTs its tool list to ``/page-agent/register``, which creates a
registry row (``AgentKind.PAGE``, auto-bound to the ``hub-default`` persona —
no activation gate, same rule as xiaozhi devices) and issues a token. The page
then opens the MCP bridge SSE channel (``server.mcp_bridge``) to receive
``tools/call`` requests and posts results back.

The voice WebSocket at ``/page-agent/voice`` streams 16kHz PCM from the
browser through the hub's Silero VAD + FunASR + LLM + KittenTTS pipeline,
with an optional wake word ("computer"). This reuses the same ASR/TTS/VAD
providers as xiaozhi devices rather than browser-only speech APIs.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from loguru import logger

from agent_hub import skills as server_skills
from agent_hub.config import Settings
from agent_hub.dashboard.authorization import DashboardAuthorization
from agent_hub.providers.llm import get_provider
from agent_hub.registry.models import AgentKind, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import mcp_bridge
from agent_hub.server._page_html import PAGE_HTML as _PAGE_AGENT_HTML
from agent_hub.server.tool_policy import is_risky_tool

_TAG = "page_agent"

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

_VALID_ACTIVITIES = {"idle", "listening", "thinking", "speaking", "paused"}


def _bridge_base(request: Request, settings: Settings) -> str:
    """Base URL for the MCP bridge.

    When the bridge shares the dashboard port (the default), returns an empty
    string so the browser uses its own origin (handles proxies/tailscale/Funnel
    without leaking the container's internal address). When the bridge has a
    dedicated port, constructs the URL from the request's visible host.
    """
    if settings.server.mcp_bridge_port == settings.server.dashboard_port:
        return ""
    parsed = urlsplit(str(request.url))
    scheme = parsed.scheme or "https"
    host = parsed.hostname or settings.server.host or "127.0.0.1"
    return f"{scheme}://{host}:{settings.server.mcp_bridge_port}"


def classify_utterance(raw: str, wake_word: str) -> tuple[str, str]:
    """Decide what one ASR result means for the voice loop.

    Returns (kind, text) where kind is:
      "ignore"     — drop it silently; almost certainly ASR noise
      "command"    — run an LLM turn with `text`
      "transcript" — heard, but not addressed to us; show it and do nothing

    One-word results are usually noise, so they are dropped — except a bare
    wake word, which is precisely how someone asks for attention. That case
    used to be filtered out before the wake-word check ran, which made the
    "yes?" prompt unreachable: saying just "computer" did nothing at all.

    An empty wake word means open-mic: every utterance is addressed to the
    agent. The noise filter still applies, so stray one-word ASR artefacts do
    not each cost an LLM call.

    Args:
        raw: Raw ASR transcript.
        wake_word: Configured wake word; empty means open-mic.

    Returns:
        (kind, text) as described above.
    """
    transcript = raw.strip()
    if not transcript:
        return ("ignore", "")

    lower = transcript.lower()
    has_wake = bool(wake_word) and wake_word in lower

    if len(transcript.split()) < 2 and not has_wake:
        return ("ignore", transcript)

    if not wake_word:
        return ("command", transcript)

    if not has_wake:
        return ("transcript", transcript)

    idx = lower.index(wake_word)
    command = transcript[idx + len(wake_word) :].strip().lstrip(",.?!").strip()
    # Bare wake word: answer it rather than dropping it, so the obvious first
    # thing a user tries gets a response.
    return ("command", command or "yes?")


def _new_device_id() -> str:
    return "page-" + secrets.token_hex(8)


# ── Linked-agent tools ───────────────────────────────────────────────────────
#
# A persona may borrow the *non-destructive* MCP tools of other connected
# agents (persona.linked_agents). Borrowed tools are namespaced with the source
# agent's id (``robot-01.grip``) so calls route back to it through the bridge.
# Destructive tools — by MCP annotation, else the name heuristic — are excluded;
# borrowing crosses an agent boundary.

_LINKED_SEP = "."


def linked_tool_defs(persona: Persona) -> list[dict[str, Any]]:
    """OpenAI tool defs for the borrowable tools of every linked agent."""
    out: list[dict[str, Any]] = []
    for linked_id in persona.linked_agents_list:
        for d in mcp_bridge.list_page_tool_definitions(linked_id):
            fn = d["function"]
            if is_risky_tool(fn["name"], mcp_bridge.tool_annotations(linked_id, fn["name"])):
                continue
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{linked_id}{_LINKED_SEP}{fn['name']}",
                        "description": f"[{linked_id}] {fn['description']}",
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
            )
    return out


def resolve_linked_call(persona: Persona, name: str) -> tuple[str, str] | None:
    """Split a namespaced linked-tool name into ``(agent_id, tool)``, or None."""
    for linked_id in persona.linked_agents_list:
        prefix = f"{linked_id}{_LINKED_SEP}"
        if name.startswith(prefix):
            return linked_id, name[len(prefix) :]
    return None


async def call_linked_tool(linked_id: str, tool: str, args: dict[str, Any]) -> str:
    """Run a borrowed tool on a linked agent; never raises."""
    handle = mcp_bridge.get_page_agent(linked_id)
    if handle is None or not handle.connected:
        return f"{linked_id} is not connected — cannot run {tool!r}."
    try:
        return await mcp_bridge.call_page_tool(linked_id, tool, args, timeout=30.0)
    except Exception as exc:  # noqa: BLE001 - surface any bridge failure to the model
        return f"{linked_id}{_LINKED_SEP}{tool} failed: {exc}"


def make_router(
    store: RegistryStore,
    settings: Settings,
    config: dict[str, Any],
    authorization: DashboardAuthorization | None = None,
) -> APIRouter:
    """Build the page-agent router (mounted on the dashboard port).

    Args:
        store: Registry store used to create the page-agent row and issue tokens.
        settings: Server settings for URL construction and heartbeat cadence.
        config: Raw config dict (for LLM provider instantiation).
        authorization: Shared dashboard policy. Constructed locally for tests
            and embedding when omitted.

    Returns:
        FastAPI router serving the page HTML plus register/heartbeat/ask endpoints.
    """
    router = APIRouter()
    auth = authorization or DashboardAuthorization(store, config)

    @router.get(
        "/dashboard/page-agent",
        response_class=HTMLResponse,
        dependencies=[Depends(auth.authenticate), Depends(auth.require_operator)],
    )
    async def page_agent_page(request: Request) -> HTMLResponse:
        # ?persona=<name> pre-selects the persona the page registers with.
        persona = request.query_params.get("persona", "").strip()
        return HTMLResponse(_PAGE_AGENT_HTML.replace("%%PERSONA%%", json.dumps(persona)))

    @router.options("/page-agent/register")
    async def register_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post(
        "/page-agent/register",
        dependencies=[
            Depends(auth.authenticate),
            Depends(auth.require_same_origin),
            Depends(auth.require_operator),
        ],
    )
    async def register(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "message": "expected object"}, status_code=400)
        device_id = str(payload.get("device_id") or "").strip() or _new_device_id()
        label = str(payload.get("label") or "").strip() or None
        raw_tools = payload.get("tools") or []
        tools: list[dict[str, Any]] = (
            [t for t in raw_tools if isinstance(t, dict)] if isinstance(raw_tools, list) else []
        )

        client_host = request.client.host if request.client else None
        await store.get_or_create_agent(
            device_id=device_id,
            kind=AgentKind.PAGE,
            label=label,
            ip_address=client_host,
            firmware_version="page-1.0",
        )

        persona = str(payload.get("persona") or "").strip()
        if persona and await store.get_persona_by_name(persona):
            await store.assign_persona(device_id, persona)
            logger.bind(tag=_TAG).info(f"Page agent {device_id!r} → persona {persona!r}")

        token = await store.issue_websocket_token(device_id)
        mcp_bridge.register_page_agent(device_id, token, tools)
        logger.bind(tag=_TAG).info(f"Page agent registered {device_id!r} ({label or 'unlabelled'})")

        base = _bridge_base(request, settings)
        return JSONResponse(
            {
                "ok": True,
                "device_id": device_id,
                "token": token,
                "mcp_event_url": f"{base}/mcp/v1/events",
                "mcp_respond_url": f"{base}/mcp/v1/respond",
                "heartbeat_url": "/page-agent/heartbeat",
                "heartbeat_interval_seconds": settings.server.heartbeat_interval_seconds,
            },
            headers=_CORS,
        )

    @router.post("/page-agent/heartbeat")
    async def heartbeat(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            return JSONResponse({"ok": False, "message": "expected object"}, status_code=400)
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not device_id or not token:
            return JSONResponse(
                {"ok": False, "message": "device_id and token required"}, status_code=400
            )
        activity = str(payload.get("activity") or "idle").strip().lower()
        if activity not in _VALID_ACTIVITIES:
            activity = "idle"
        raw_tools = payload.get("mcp_tools") or []
        mcp_tools: list[str] = (
            [t for t in raw_tools if isinstance(t, str)] if isinstance(raw_tools, list) else []
        )
        accepted = await store.record_authenticated_heartbeat(
            device_id, token, None, activity, mcp_tools
        )
        if not accepted:
            return JSONResponse({"ok": False, "message": "invalid token"}, status_code=401)
        return JSONResponse({"ok": True, "server_time": int(time.time() * 1000)}, headers=_CORS)

    @router.options("/page-agent/heartbeat")
    async def heartbeat_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.options("/page-agent/ask")
    async def ask_preflight() -> JSONResponse:
        return JSONResponse({}, headers=_CORS)

    @router.post(
        "/page-agent/ask",
        dependencies=[
            Depends(auth.authenticate),
            Depends(auth.require_same_origin),
            Depends(auth.require_operator),
        ],
    )
    async def ask(request: Request) -> JSONResponse:
        """Run a text LLM turn for a page agent, routing tool calls to the page.

        Mirrors the voice session's LLM loop: collects the page's MCP tools plus
        server skills, calls the LLM with function-calling, and when the LLM
        calls a page tool (e.g. page.camera.take_photo) routes it through the
        MCP bridge. Returns the final text reply; the page speaks it locally.
        """
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "message": "expected object"}, status_code=400, headers=_CORS
            )
        if not isinstance(payload, dict):
            return JSONResponse(
                {"ok": False, "message": "expected object"}, status_code=400, headers=_CORS
            )
        device_id = str(payload.get("device_id") or "").strip()
        token = str(payload.get("token") or "").strip()
        text = str(payload.get("text") or "").strip()
        if not device_id or not token:
            return JSONResponse(
                {"ok": False, "message": "device_id and token required"},
                status_code=400,
                headers=_CORS,
            )
        if not text:
            return JSONResponse(
                {"ok": False, "message": "text required"}, status_code=400, headers=_CORS
            )
        if not await store.validate_websocket_token(device_id, token):
            return JSONResponse(
                {"ok": False, "message": "invalid token"}, status_code=401, headers=_CORS
            )

        persona = await store.get_persona_for_device(device_id)
        if persona is None:
            return JSONResponse(
                {"ok": False, "message": "no persona assigned"}, status_code=500, headers=_CORS
            )

        # Collect tools: page MCP tools + server skills (skip page_speak/page_see
        # which are wrappers for other agents — the page's own LLM calls its
        # tools directly via the bridge).
        page_tool_defs = mcp_bridge.list_page_tool_definitions(device_id)
        skill_defs = [
            d
            for d in server_skills.get_definitions()
            if d["function"]["name"] not in {"page_speak", "page_see"}
        ]
        tools = page_tool_defs + skill_defs + linked_tool_defs(persona)

        # Load conversation history.
        history = await store.load_history(device_id, limit=persona.memory_window * 2)
        history.append({"role": "user", "content": text})

        # Build system prompt with tool descriptions (same pattern as ws_session).
        tool_lines: list[str] = []
        for d in tools:
            fn = d["function"]
            extra = ""
            if "camera" in fn["name"] or "photo" in fn["name"]:
                extra = " Always pass a 'question' arg describing what to look for."
            tool_lines.append(f"- {fn['name']}: {fn['description']}{extra}")
        system_prompt = persona.system_prompt or ""
        if tool_lines:
            system_prompt = (
                f"{system_prompt}\n\nAvailable tools you MUST use when relevant:\n"
                + "\n".join(tool_lines)
            ).strip()

        # Tool executor: route page tools via the bridge, skills via skills.run_result.
        page_tool_names = {d["function"]["name"] for d in page_tool_defs}
        captured_images: list[str] = []

        async def _exec_tool(name: str, args: dict[str, Any]) -> str:
            linked = resolve_linked_call(persona, name)
            if linked is not None:
                return await call_linked_tool(linked[0], linked[1], args)
            if name in page_tool_names:
                timeout = 60.0 if ("camera" in name or "photo" in name) else 30.0
                page_result = await mcp_bridge.call_page_tool(
                    device_id, name, args, timeout=timeout
                )
                # Track captured images so we can show them in the dialogue
                if isinstance(page_result, str) and page_result.startswith("data:image"):
                    captured_images.append(page_result)
                return page_result
            if server_skills.has_skill(name):
                skill_result = await server_skills.run_result(name, args)
                return skill_result.text
            return f"unknown tool: {name!r}"

        # Run the LLM turn (non-streaming — the page displays the full text).
        llm = get_provider(persona.llm_provider, config, model_override=persona.llm_model or None)
        try:
            reply = await llm.complete_with_tools(
                history, tools, _exec_tool, system_prompt=system_prompt
            )
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"Page agent LLM turn failed: {exc}")
            return JSONResponse(
                {"ok": False, "message": f"LLM error: {exc}"}, status_code=500, headers=_CORS
            )

        reply = (reply or "").strip()
        if reply:
            await store.append_history(device_id, "user", text)
            history_content = reply
            if captured_images:
                history_content = f"{reply}\n[image:captured]"
            await store.append_history(device_id, "assistant", history_content)

        logger.bind(tag=_TAG).info(
            f"Page agent {device_id!r} ask: {text!r} → {reply[:80]!r} "
            f"({len(captured_images)} images)"
        )
        return JSONResponse(
            {"ok": True, "reply": reply, "images": captured_images},
            headers=_CORS,
        )

    # ── Voice WebSocket: browser mic → hub VAD + ASR + LLM + TTS ──────────

    @router.websocket("/page-agent/voice")
    async def page_voice_session(websocket: WebSocket) -> None:
        """Stream 16kHz PCM from the browser through the full hub pipeline.

        The browser sends raw int16 LE PCM as binary frames and JSON control
        messages as text frames. The hub runs SileroVAD on the PCM, transcribes
        with the persona's ASR provider, runs the LLM+tools turn, and streams
        TTS PCM back as binary frames (preceded by ``{"type":"tts","state":
        "start"}`` and followed by ``{"type":"tts","state":"stop"}``).

        An optional wake word ("computer") gates which utterances trigger a
        full LLM turn; non-wake utterances are shown as transcripts but not
        sent to the LLM.
        """
        try:
            await auth.authorize_websocket(websocket)
        except HTTPException:
            await websocket.close(code=1008, reason="operator authorization required")
            return

        device_id = websocket.query_params.get("device_id", "")
        token = websocket.query_params.get("token", "")
        if not device_id or not await store.validate_websocket_token(device_id, token):
            await websocket.close(code=1008, reason="invalid device_id or token")
            return

        await websocket.accept()
        logger.bind(tag=_TAG).info(f"Page voice WS connected: {device_id!r}")

        persona = await store.get_persona_for_device(device_id)
        if persona is None:
            await websocket.close(code=1008, reason="no persona")
            return

        from agent_hub.providers.asr import get_provider as get_asr
        from agent_hub.providers.llm import get_provider as get_llm
        from agent_hub.providers.tts import get_provider as get_tts
        from agent_hub.server.audio import PcmSileroVAD, pcm_to_wav

        vad_model_path = (
            config.get("vad", {}).get("silero", {}).get("model_path", "models/silero_vad.onnx")
        )
        try:
            vad = PcmSileroVAD(model_path=vad_model_path, sample_rate=16000)
        except Exception as exc:
            logger.bind(tag=_TAG).warning(
                f"Page voice {device_id!r}: PcmSileroVAD unavailable ({exc}), closing"
            )
            await websocket.close(code=1011, reason="VAD model unavailable")
            return

        conversation: list[dict[str, str]] = await store.load_history(
            device_id, limit=(persona.memory_window or 20) * 2
        )
        pipeline_lock = asyncio.Lock()
        wake_word = "computer"

        # Tool setup: page MCP tools + server skills (same as /page-agent/ask).
        page_tool_defs = mcp_bridge.list_page_tool_definitions(device_id)
        skill_defs = [
            d
            for d in server_skills.get_definitions()
            if d["function"]["name"] not in {"page_speak", "page_see"}
        ]
        tools = page_tool_defs + skill_defs + linked_tool_defs(persona)
        page_tool_names = {d["function"]["name"] for d in page_tool_defs}

        tool_lines: list[str] = []
        for d in tools:
            fn = d["function"]
            extra = ""
            if "camera" in fn["name"] or "photo" in fn["name"]:
                extra = " Always pass a 'question' arg describing what to look for."
            tool_lines.append(f"- {fn['name']}: {fn['description']}{extra}")
        system_prompt = persona.system_prompt or ""
        if tool_lines:
            system_prompt = (
                f"{system_prompt}\n\nAvailable tools you MUST use when relevant:\n"
                + "\n".join(tool_lines)
            ).strip()

        async def _exec_tool(name: str, args: dict[str, Any]) -> str:
            linked = resolve_linked_call(persona, name)
            if linked is not None:
                return await call_linked_tool(linked[0], linked[1], args)
            if name in page_tool_names:
                timeout = 60.0 if ("camera" in name or "photo" in name) else 30.0
                return await mcp_bridge.call_page_tool(device_id, name, args, timeout=timeout)
            if server_skills.has_skill(name):
                result = await server_skills.run_result(name, args)
                return result.text
            return f"unknown tool: {name!r}"

        async def _run_turn(transcript: str) -> None:
            async with pipeline_lock:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "stt",
                            "text": transcript,
                        }
                    )
                )
                await websocket.send_text(json.dumps({"type": "thinking"}))
                llm = get_llm(
                    persona.llm_provider, config, model_override=persona.llm_model or None
                )
                try:
                    reply = await llm.complete_with_tools(
                        conversation, tools, _exec_tool, system_prompt=system_prompt
                    )
                except Exception as exc:
                    logger.bind(tag=_TAG).error(f"Page voice LLM error: {exc}")
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "message": str(exc),
                            }
                        )
                    )
                    return
                reply = (reply or "").strip()
                if not reply:
                    return
                conversation.append({"role": "user", "content": transcript})
                conversation.append({"role": "assistant", "content": reply})
                await store.append_history(device_id, "user", transcript)
                await store.append_history(device_id, "assistant", reply)

                # TTS: synthesize and stream PCM back
                tts = get_tts(persona.tts_provider, config)
                try:
                    pcm_bytes, tts_rate = await tts.synthesize_pcm(reply, voice=persona.tts_voice)
                except Exception as exc:
                    logger.bind(tag=_TAG).error(f"Page voice TTS error: {exc}")
                    return
                # Resample if needed (browser plays 24kHz or we send 16kHz raw)
                if tts_rate != 16000:
                    from agent_hub.server.audio import pcm_resample

                    pcm_bytes = await pcm_resample(pcm_bytes, tts_rate, 16000)

                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "tts",
                            "state": "start",
                            "text": reply,
                        }
                    )
                )
                # Send PCM in ~60ms chunks (1920 bytes = 960 samples * 2)
                chunk_size = 1920
                for i in range(0, len(pcm_bytes), chunk_size):
                    await websocket.send_bytes(pcm_bytes[i : i + chunk_size])
                await websocket.send_text(json.dumps({"type": "tts", "state": "stop"}))
                logger.bind(tag=_TAG).info(
                    f"Page voice {device_id!r}: {transcript!r} → {reply[:60]!r}"
                )

        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg:
                    if pipeline_lock.locked():
                        continue  # drop audio while thinking/speaking
                    pcm = msg["bytes"]
                    if vad.push(pcm):
                        pcm_all = vad.take_pcm()
                        wav_bytes = pcm_to_wav(pcm_all, 16000)
                        asr = get_asr(persona.asr_provider, config)
                        result = await asr.transcribe(wav_bytes)
                        if not result.is_speech or not result.text:
                            continue
                        kind, text = classify_utterance(result.text, wake_word)
                        if kind == "ignore":
                            continue
                        if kind == "command":
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "wake",
                                        "word": wake_word,
                                        "command": text,
                                    }
                                )
                            )
                            await _run_turn(text)
                        else:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "transcript",
                                        "text": text,
                                    }
                                )
                            )
                elif "text" in msg:
                    ctrl = json.loads(msg["text"])
                    if ctrl.get("type") == "wake_word":
                        # Browser can set/clear the wake word at runtime
                        wake_word = ctrl.get("word", "")
                    elif ctrl.get("type") == "stop":
                        vad.reset()
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"Page voice error for {device_id!r}: {exc}")
        finally:
            logger.bind(tag=_TAG).info(f"Page voice WS disconnected: {device_id!r}")

    return router
