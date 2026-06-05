"""WebSocket voice session handler: /xiaozhi/v1/

Each device connection runs through:
  1. Auth — extract device-id from headers or query params; reject if missing
  2. Persona load — look up the device's assigned persona from the registry
  3. Hello — receive ClientHello, send ServerWelcome
  4. Audio loop — SilenceVAD accumulates Opus frames; on speech-end fires:
       Opus decode → WAV → Whisper ASR → OpenAI LLM → TTS → PCM → Opus → device
  5. Cleanup — mark agent IDLE, dispose VAD on disconnect

Concurrency: a per-connection asyncio.Lock prevents overlapping pipeline runs
if the device sends audio while TTS is still streaming back.
"""

from __future__ import annotations

import asyncio
import json
import re as _re
import socket
import time
import uuid
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

import agent_hub.skills as server_skills
from agent_hub.providers.asr import get_provider as get_asr
from agent_hub.providers.llm import get_provider as get_llm
from agent_hub.providers.tts import get_provider as get_tts
from agent_hub.registry.models import AgentStatus, Persona
from agent_hub.registry.store import RegistryStore
from agent_hub.server import emotion as emotion_utils
from agent_hub.server import session_state, tool_policy, transcript_log
from agent_hub.server.audio import (
    AudioRateController,
    OpusDecoder,
    OpusEncoder,
    SilenceVAD,
    SileroVAD,
    pcm_resample,
    pcm_to_wav,
)
from agent_hub.server.mcp_client import MCPClient
from agent_hub.server.protocol import SERVER_TTS_AUDIO_PARAMS, ClientHello, ServerWelcome

_TAG = "ws_session"

_GREETING = "Agent hub connected. Ready."


JsonDict = dict[str, Any]


def _parse_ctrl(text: str) -> JsonDict:
    """Parse a WebSocket text control message, repairing known firmware JSON bugs.

    Some firmware versions emit ) instead of } as a JSON object closing delimiter.
    We repair that pattern before parsing so MCP handshake succeeds.
    """
    if not text:
        return {}
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        # Firmware bug: stray ) inserted before } in JSON object close sequence
        repaired = _re.sub(r"\)([,}\]\s])", r"\1", text)
        result = json.loads(repaired)
        logger.bind(tag=_TAG).debug("Repaired malformed firmware JSON (')' → '}')")
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError) as _e2:
        logger.bind(tag=_TAG).warning(f"JSON repair also failed ({_e2})\nFull text: {text!r}")
        return {}


def _vision_url(config: JsonDict) -> str:
    """Derive the image-explain HTTP URL from the server config."""
    srv = config.get("server") or {}
    ws_port = int(srv.get("ws_port", 8000))
    ws_override = str(srv.get("websocket", ""))
    if ws_override:
        http_base = ws_override.replace("ws://", "http://").replace("wss://", "https://")
    else:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
        except Exception:
            local_ip = "127.0.0.1"
        http_base = f"http://{local_ip}:{ws_port}"
    p = urlparse(http_base)
    return urlunparse(p._replace(path="/xiaozhi/v1/image/", query="", fragment=""))


async def _speak(
    websocket: WebSocket,
    text: str,
    persona: Persona,
    config: dict[str, Any],
    session_id: str,
) -> None:
    """Synthesise text to speech and stream it to the device."""
    tts = get_tts(persona.tts_provider, config)
    pcm_bytes, tts_rate = await tts.synthesize_pcm(text, voice=persona.tts_voice)
    target_rate = SERVER_TTS_AUDIO_PARAMS.sample_rate
    if tts_rate != target_rate:
        pcm_bytes = await pcm_resample(pcm_bytes, tts_rate, target_rate)

    await websocket.send_text(
        json.dumps(
            {
                "type": "tts",
                "session_id": session_id,
                "state": "start",
                "text": text,
            }
        )
    )

    encoder = OpusEncoder(target_rate, frame_duration_ms=SERVER_TTS_AUDIO_PARAMS.frame_duration)
    rate_ctrl = AudioRateController(frame_duration_ms=SERVER_TTS_AUDIO_PARAMS.frame_duration)
    packets = encoder.encode(pcm_bytes)

    for i, packet in enumerate(packets):
        if i < AudioRateController.PRE_BUFFER_COUNT:
            await websocket.send_bytes(packet)
        else:
            rate_ctrl.add_audio(packet)

    if rate_ctrl._queue:
        rate_ctrl.start(websocket.send_bytes)
        await rate_ctrl.wait_until_done()
        rate_ctrl.stop()

    await websocket.send_text(
        json.dumps(
            {
                "type": "tts",
                "session_id": session_id,
                "state": "stop",
            }
        )
    )


async def _run_llm_turn(
    websocket: WebSocket,
    transcript: str,
    session_id: str,
    persona: Persona,
    history: list[dict[str, str]],
    config: dict[str, Any],
    mcp_client: MCPClient | None,
    device_id: str,
    supports_emoji: bool,
    emotion: str = "",
) -> tuple[int, int, str]:
    """LLM + TTS half of a voice turn. Mutates history. Returns (llm_ms, tts_ms, reply)."""
    history.append({"role": "user", "content": transcript})
    window = (persona.memory_window or 20) * 2
    if len(history) > window:
        del history[:-window]
    llm = get_llm(persona.llm_provider, config, model_override=persona.llm_model or None)

    # Permission gating. None/[] allowlist → safe defaults (risky device tools
    # excluded); a non-empty list is an explicit admin/custom allowlist. The
    # same policy is applied here (LLM tool list) and in _exec_tool (execution).
    tool_allowlist = persona.mcp_tools_allowlist_list
    enabled_skills = persona.server_skills_list  # None/[] → all skills enabled
    allowed_device_names = set(
        tool_policy.allowed_device_tools(
            list(mcp_client.tools.keys()) if (mcp_client and mcp_client.ready) else [],
            tool_allowlist,
        )
    )

    def _skill_enabled(name: str) -> bool:
        return not enabled_skills or name in enabled_skills

    base_prompt = persona.system_prompt or ""
    tool_lines: list[str] = []
    for defn in server_skills.get_definitions():
        fn = defn["function"]
        if not _skill_enabled(fn["name"]):
            continue
        tool_lines.append(f"- {fn['name']}: {fn['description']}")
    if mcp_client and mcp_client.ready:
        for name, data in mcp_client.tools.items():
            if name not in allowed_device_names:
                continue
            desc = data.get("description", "")
            extra = ""
            if "photo" in name or "camera" in name or "image" in name:
                extra = " Always pass a 'question' arg describing what to look for."
            tool_lines.append(f"- {name}: {desc}{extra}")
    if tool_lines:
        tools_section = "Available tools you MUST use when relevant:\n" + "\n".join(tool_lines)
        base_prompt = f"{base_prompt}\n\n{tools_section}".strip()

    if emotion and emotion != "NEUTRAL":
        system_prompt = f"{base_prompt}\n[User tone: {emotion.lower()}]".strip()
    else:
        system_prompt = base_prompt

    device_tools = [
        d
        for d in (mcp_client.get_tool_definitions() if (mcp_client and mcp_client.ready) else [])
        if d["function"]["name"] in allowed_device_names
    ]
    skill_tools = [
        d for d in server_skills.get_definitions() if _skill_enabled(d["function"]["name"])
    ]
    tools = device_tools + skill_tools

    session_state.set_pipeline_status(device_id, "thinking", transcript)
    t1 = time.monotonic()
    captured_images: list[str] = []

    if tools:

        async def _exec_tool(name: str, args: JsonDict) -> str:
            logger.bind(tag=_TAG).info(f"Tool call: {name!r} args={args}")
            try:
                if mcp_client and mcp_client.ready and name in mcp_client.tools:
                    if not tool_policy.is_tool_allowed(name, tool_allowlist):
                        logger.bind(tag=_TAG).warning(
                            f"Blocked disallowed device tool {name!r} (allowlist={tool_allowlist})"
                        )
                        return f"tool {name!r} is not permitted for this device"
                    if ("camera" in name or "photo" in name) and "question" not in args:
                        args = {**args, "question": "What do you see?"}
                    if "camera" in name or "photo" in name:
                        await _speak(
                            websocket, "Hold on, let me take a look.", persona, config, session_id
                        )
                    result = await mcp_client.call_tool(
                        name,
                        args,
                        timeout=60.0 if ("camera" in name or "photo" in name) else 30.0,
                    )
                    if "camera" in name or "photo" in name:
                        img = session_state.get_latest_image(device_id)
                        if img:
                            captured_images.append(img)
                elif server_skills.has_skill(name):
                    if not _skill_enabled(name):
                        logger.bind(tag=_TAG).warning(
                            f"Blocked disabled skill {name!r} (enabled={enabled_skills})"
                        )
                        return f"skill {name!r} is not enabled for this persona"
                    result = await server_skills.run(name, args)
                else:
                    result = f"unknown tool: {name!r}"
                logger.bind(tag=_TAG).info(f"Tool result {name!r}: {str(result)[:200]!r}")
                return result
            except Exception as exc:
                logger.bind(tag=_TAG).error(f"Tool {name!r} failed: {exc}")
                return f"error: {exc}"

        reply = await llm.complete_with_tools(
            history,
            tools,
            _exec_tool,
            system_prompt=system_prompt,
        )
    else:
        reply = await llm.complete(history, system_prompt=system_prompt)
    llm_ms = int((time.monotonic() - t1) * 1000)

    if not reply:
        history.pop()
        return 0, 0, ""
    # Embed captured image path as a marker so the history view can render it
    history_content = reply
    if captured_images:
        history_content = f"{reply}\n[image:{captured_images[0]}]"
    history.append({"role": "assistant", "content": history_content})
    logger.bind(tag=_TAG).info(f"LLM ({llm_ms}ms): {reply!r}")

    tts_text = reply
    if supports_emoji:
        face = emotion_utils.extract_reply_emotion(reply)
        if face:
            emoji, em_name = face
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "llm",
                        "session_id": session_id,
                        "text": emoji,
                        "emotion": em_name,
                    }
                )
            )
            tts_text = emotion_utils.strip_emoji(reply)

    session_state.set_pipeline_status(device_id, "speaking", reply)
    t2 = time.monotonic()
    await _speak(websocket, tts_text, persona, config, session_id)
    tts_ms = int((time.monotonic() - t2) * 1000)

    return llm_ms, tts_ms, reply


async def _run_voice_turn(
    websocket: WebSocket,
    opus_frames: list[bytes],
    session_id: str,
    audio_params_sample_rate: int,
    audio_params_frame_duration: int,
    persona: Persona,
    history: list[dict[str, str]],
    config: dict[str, Any],
    mcp_client: MCPClient | None = None,
    device_id: str = "",
    supports_emoji: bool = False,
) -> None:
    """Run one ASR → LLM → TTS cycle."""
    # 1 — decode Opus frames to WAV for Whisper
    decoder = OpusDecoder(audio_params_sample_rate, audio_params_frame_duration)
    pcm_chunks = [decoder.decode(f) for f in opus_frames]
    wav_bytes = pcm_to_wav(b"".join(pcm_chunks), audio_params_sample_rate)

    # 2 — ASR
    t0 = time.monotonic()
    asr = get_asr(persona.asr_provider, config)
    result = await asr.transcribe(wav_bytes)
    asr_ms = int((time.monotonic() - t0) * 1000)
    if not result.is_speech:
        logger.bind(tag=_TAG).debug("Non-speech event, skipping turn")
        return
    transcript = result.text
    if not transcript or len(transcript.split()) < 2:
        logger.bind(tag=_TAG).debug(f"Transcript too short, skipping: {transcript!r}")
        return
    logger.bind(tag=_TAG).info(
        f"ASR ({asr_ms}ms): {transcript!r} [emotion={result.emotion} lang={result.language or '?'}]"
    )
    session_state.set_pipeline_status(device_id, "thinking", transcript)

    await websocket.send_text(
        json.dumps(
            {
                "type": "stt",
                "session_id": session_id,
                "text": transcript,
            }
        )
    )

    # Reactive emotion: mirror user's detected tone on device face immediately
    if supports_emoji:
        face = emotion_utils.user_emotion_face(result.emotion)
        if face:
            emoji, em_name = face
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "llm",
                        "session_id": session_id,
                        "text": emoji,
                        "emotion": em_name,
                    }
                )
            )

    # 3+4 — LLM + TTS
    llm_ms, tts_ms, reply = await _run_llm_turn(
        websocket,
        transcript,
        session_id,
        persona,
        history,
        config,
        mcp_client,
        device_id,
        supports_emoji,
        emotion=result.emotion,
    )

    if not reply:
        return

    if device_id:
        session_state.record_turn(device_id, asr_ms, llm_ms, tts_ms)
        transcript_log.log_turn(
            device_id=device_id,
            text=transcript,
            emotion=result.emotion,
            language=result.language or "",
            reply=reply,
            asr_ms=asr_ms,
            llm_ms=llm_ms,
            tts_ms=tts_ms,
        )
    logger.bind(tag=_TAG).debug(
        f"Turn latency — ASR:{asr_ms}ms LLM:{llm_ms}ms TTS:{tts_ms}ms "
        f"total:{asr_ms + llm_ms + tts_ms}ms"
    )


async def _run_text_turn(
    websocket: WebSocket,
    transcript: str,
    session_id: str,
    persona: Persona,
    history: list[dict[str, str]],
    config: dict[str, Any],
    mcp_client: MCPClient | None,
    device_id: str,
    supports_emoji: bool,
) -> str:
    """Run one LLM → TTS cycle from an injected text utterance, bypassing ASR.
    Returns the LLM reply text (empty string if no reply).
    """
    logger.bind(tag=_TAG).info(f"{device_id!r} injected utterance: {transcript!r}")
    await websocket.send_text(
        json.dumps(
            {
                "type": "stt",
                "session_id": session_id,
                "text": transcript,
            }
        )
    )
    # Show thinking face on device while pipeline runs
    if supports_emoji:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "llm",
                    "session_id": session_id,
                    "text": "🤔",
                    "emotion": "thinking",
                }
            )
        )

    llm_ms, tts_ms, reply = await _run_llm_turn(
        websocket,
        transcript,
        session_id,
        persona,
        history,
        config,
        mcp_client,
        device_id,
        supports_emoji,
        emotion="",
    )

    if reply and device_id:
        session_state.record_turn(device_id, 0, llm_ms, tts_ms)
        transcript_log.log_turn(
            device_id=device_id,
            text=transcript,
            emotion="injected",
            language="",
            reply=reply,
            asr_ms=0,
            llm_ms=llm_ms,
            tts_ms=tts_ms,
        )
    return reply


def make_router(store: RegistryStore, config: dict[str, Any]) -> APIRouter:
    """Build and return the WebSocket session APIRouter.

    Args:
        store: Registry store for persona lookup and lifecycle tracking.
        config: Raw application config dict passed to provider factories.

    Returns:
        FastAPI router exposing ws://.../xiaozhi/v1/.
    """
    router = APIRouter()
    enrollment_required = bool((config.get("server") or {}).get("enrollment_token", ""))

    def _websocket_token(websocket: WebSocket) -> str:
        auth = websocket.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return websocket.query_params.get("token", "").strip()

    @router.websocket("/xiaozhi/v1/")
    async def voice_session(websocket: WebSocket) -> None:
        """Handle one device voice session end-to-end."""
        device_id = websocket.headers.get("device-id") or websocket.query_params.get("device-id")
        if not device_id:
            await websocket.close(code=1008, reason="missing device-id")
            return
        if enrollment_required and not await store.validate_websocket_token(
            device_id,
            _websocket_token(websocket),
        ):
            await websocket.close(code=1008, reason="invalid token")
            return

        await websocket.accept()
        logger.bind(tag=_TAG).info(f"WS connected: {device_id!r}")

        session_id = uuid.uuid4().hex
        pipeline_lock = asyncio.Lock()
        mcp_client: MCPClient | None = None
        active_pipeline: asyncio.Task[None] | None = None

        try:
            # 1. Hello / welcome handshake
            raw = await websocket.receive_text()
            hello = ClientHello.from_json(json.loads(raw))
            logger.bind(tag=_TAG).debug(
                f"{device_id!r} hello: format={hello.audio_params.format} "
                f"rate={hello.audio_params.sample_rate} "
                f"frame={hello.audio_params.frame_duration}ms "
                f"mcp={hello.supports_mcp}"
            )
            await websocket.send_text(json.dumps(ServerWelcome(session_id=session_id).to_json()))

            # 2. MCP registration — complete before persona assignment so the
            #    server knows the device's exact capabilities when picking a persona.
            if hello.supports_mcp:
                mcp_client = MCPClient(
                    websocket,
                    device_id,
                    on_ready=lambda tools: session_state.set_tools(device_id, tools),
                )
                session_state.register_mcp_client(device_id, mcp_client)
                image_token = (config.get("server") or {}).get("image_token", "")
                base_vision_url = _vision_url(config)
                sep = "&" if "?" in base_vision_url else "?"
                vision_url_with_id = f"{base_vision_url}{sep}device_id={device_id}"
                await mcp_client.initialize(
                    vision_url=vision_url_with_id,
                    vision_token=image_token,
                )

                # Drain incoming messages until the tools/list handshake completes.
                # Audio bytes arriving now are dropped — the device hasn't been
                # asked to listen yet so this is safe.
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 5.0
                while not mcp_client.ready:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        logger.bind(tag=_TAG).warning(
                            f"{device_id!r} MCP handshake timed out — "
                            "proceeding without device tools"
                        )
                        break
                    try:
                        msg = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                    except TimeoutError:
                        break
                    except (WebSocketDisconnect, RuntimeError):
                        logger.bind(tag=_TAG).debug(
                            f"{device_id!r} disconnected during MCP handshake"
                        )
                        return
                    if msg.get("type") == "websocket.disconnect":
                        logger.bind(tag=_TAG).debug(
                            f"{device_id!r} disconnect frame during MCP handshake"
                        )
                        return
                    if "text" in msg:
                        ctrl = _parse_ctrl(msg["text"] or "")
                        if ctrl.get("type") == "mcp":
                            await mcp_client.handle_message(ctrl.get("payload", {}))
                        # other ctrl messages (listen:start etc.) handled in audio loop

            # 3. Assign persona now that MCP capabilities are known.
            #    Prefer a persona whose mcp_tools_allowlist matches the device's
            #    tools (most specific wins); fall back to the device's stored assignment.
            persona = None
            if mcp_client and mcp_client.ready and mcp_client.tools:
                persona = await store.find_best_persona_for_tools(list(mcp_client.tools.keys()))
                if persona:
                    logger.bind(tag=_TAG).info(
                        f"{device_id!r} matched persona {persona.name!r} "
                        f"via tools {list(mcp_client.tools.keys())}"
                    )
            if persona is None:
                persona = await store.get_persona_for_device(device_id)
            if persona is None:
                logger.bind(tag=_TAG).warning(
                    f"{device_id!r} has no persona — device may not have checked in yet"
                )
                await websocket.close(code=1008, reason="device not registered")
                return

            await store.set_agent_status(device_id, AgentStatus.ACTIVE)

            # Load persisted history; trim to memory_window on reconnect
            window = (persona.memory_window or 20) * 2
            conversation: list[dict[str, str]] = await store.load_history(device_id, limit=window)
            if conversation:
                logger.bind(tag=_TAG).debug(
                    f"{device_id!r} resumed {len(conversation)} messages from history"
                )

            pending_frames: list[bytes] | None = None

            async def _dispatch_pipeline(frames: list[bytes]) -> None:
                nonlocal pending_frames
                if pipeline_lock.locked():
                    logger.bind(tag=_TAG).debug(
                        f"{device_id!r} pipeline busy — dropping {len(frames)} frames"
                    )
                    return
                # Signal device that we're thinking
                if hello.supports_emoji:
                    with suppress(Exception):
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "type": "llm",
                                    "session_id": session_id,
                                    "text": "🤔",
                                    "emotion": "thinking",
                                }
                            )
                        )
                session_state.set_pipeline_status(device_id, "transcribing")
                prev_len = len(conversation)
                async with pipeline_lock:
                    try:
                        await _run_voice_turn(
                            websocket,
                            frames,
                            session_id,
                            hello.audio_params.sample_rate,
                            hello.audio_params.frame_duration,
                            persona,
                            conversation,
                            config,
                            mcp_client,
                            device_id,
                            supports_emoji=hello.supports_emoji,
                        )
                    except Exception as exc:
                        import traceback as _tb

                        logger.bind(tag=_TAG).error(
                            f"Pipeline error for {device_id!r}: {exc}\n" + _tb.format_exc()
                        )
                session_state.set_pipeline_status(device_id, "idle")
                new_msgs = conversation[prev_len:]
                for msg in new_msgs:
                    await store.append_history(device_id, msg["role"], msg["content"])
                # Run the most recent turn that arrived while we were busy
                if pending_frames is not None:
                    queued = pending_frames
                    pending_frames = None
                    _fire_pipeline(queued)

            def _fire_pipeline(frames: list[bytes]) -> None:
                nonlocal active_pipeline, pending_frames
                if active_pipeline and not active_pipeline.done():
                    logger.bind(tag=_TAG).debug(
                        f"{device_id!r} pipeline busy — queuing {len(frames)} frames"
                    )
                    pending_frames = frames
                    return
                active_pipeline = asyncio.create_task(_dispatch_pipeline(frames))

            session_state.register_session(
                device_id,
                speak=lambda text: _speak(websocket, text, persona, config, session_id),
                send_json=lambda payload: websocket.send_text(json.dumps(payload)),
            )

            async def _dispatch_text_pipeline(transcript: str) -> tuple[str, str | None]:
                if pipeline_lock.locked():
                    logger.bind(tag=_TAG).debug(
                        f"{device_id!r} pipeline busy — dropping injected turn"
                    )
                    return "", None
                reply = ""
                prev_len = len(conversation)
                async with pipeline_lock:
                    try:
                        reply = await _run_text_turn(
                            websocket,
                            transcript,
                            session_id,
                            persona,
                            conversation,
                            config,
                            mcp_client,
                            device_id,
                            supports_emoji=hello.supports_emoji,
                        )
                    except Exception as exc:
                        import traceback as _tb

                        logger.bind(tag=_TAG).error(
                            f"Text pipeline error for {device_id!r}: {exc}\n" + _tb.format_exc()
                        )
                new_msgs = conversation[prev_len:]
                for m in new_msgs:
                    await store.append_history(device_id, m["role"], m["content"])
                # Only report an image captured *during this turn* (embedded as an
                # [image:PATH] marker by _run_llm_turn), not the stale latest-image
                # cache — otherwise a non-camera reply renders a prior photo.
                fresh_image: str | None = None
                for m in new_msgs:
                    match = _re.search(r"\[image:([^\]]+)\]", m.get("content", ""))
                    if match:
                        fresh_image = match.group(1)
                return reply, fresh_image

            session_state.register_injector(device_id, _dispatch_text_pipeline)

            vad_model = (
                config.get("vad", {}).get("silero", {}).get("model_path", "models/silero_vad.onnx")
            )
            try:
                vad = SileroVAD(
                    model_path=vad_model,
                    sample_rate=hello.audio_params.sample_rate,
                    frame_duration_ms=hello.audio_params.frame_duration,
                )
                logger.bind(tag=_TAG).debug(f"{device_id!r} using SileroVAD")
            except Exception as exc:
                logger.bind(tag=_TAG).warning(
                    f"{device_id!r} SileroVAD unavailable ({exc}), falling back to RMS VAD"
                )
                vad = SilenceVAD(  # type: ignore[assignment]
                    sample_rate=hello.audio_params.sample_rate,
                    frame_duration_ms=hello.audio_params.frame_duration,
                )

            # 4. Greeting — speak once before entering the audio loop so it
            #    always precedes any voice turn regardless of device timing.
            if not session_state.has_greeted(device_id):
                session_state.mark_greeted(device_id)
                await _speak(websocket, _GREETING, persona, config, session_id)

            # 5. Main audio loop
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    raise WebSocketDisconnect()

                if "bytes" in msg:
                    if vad.push(msg["bytes"]):
                        _fire_pipeline(vad.take())

                elif "text" in msg:
                    ctrl = _parse_ctrl(msg["text"] or "")
                    ctrl_type = ctrl.get("type")
                    if ctrl_type == "listen" and ctrl.get("state") == "stop":
                        frames = vad.take()
                        if frames:
                            await _dispatch_pipeline(frames)
                    elif ctrl_type == "mcp" and mcp_client is not None:
                        payload = ctrl.get("payload", {})
                        asyncio.create_task(mcp_client.handle_message(payload))
                    else:
                        logger.bind(tag=_TAG).info(f"{device_id!r} ctrl: {msg['text'][:400]}")

        except (WebSocketDisconnect, RuntimeError) as exc:
            if isinstance(exc, RuntimeError):
                logger.bind(tag=_TAG).debug(f"WS disconnected (RuntimeError): {device_id!r}")
            else:
                logger.bind(tag=_TAG).info(f"WS disconnected: {device_id!r}")
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"WS session error for {device_id!r}: {exc}")
        finally:
            if active_pipeline and not active_pipeline.done():
                active_pipeline.cancel()
            if mcp_client is not None:
                mcp_client.cancel_pending()
            session_state.set_pipeline_status(device_id, "offline")
            session_state.unregister_session(device_id)
            await store.set_agent_status(device_id, AgentStatus.IDLE)

    return router
