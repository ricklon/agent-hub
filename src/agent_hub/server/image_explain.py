"""Image explain endpoint: POST /xiaozhi/v1/image/

The xiaozhi firmware captures a JPEG via the camera MCP tool, then POSTs it
here with a text question. We call OpenRouter's vision model and return the
description as JSON so the firmware can relay it as the MCP tool result.

Request (multipart/form-data OR raw binary):
  - file / image field: JPEG bytes  (multipart), or raw body (content-type image/*)
  - question field: optional text prompt (multipart); falls back to query param

Response JSON:
  {"text": "<description>"}

Auth: Bearer token in Authorization header must match server.image_token config.
Empty/missing token config disables auth entirely (dev mode).
"""

from __future__ import annotations

import base64
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.requests import ClientDisconnect

from agent_hub.server import session_state as _session_state

_TAG = "image_explain"

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


def make_router(config: dict[str, Any]) -> APIRouter:
    router = APIRouter()

    @router.options("/xiaozhi/v1/image/")
    async def image_explain_options() -> JSONResponse:
        return JSONResponse({}, headers=_CORS_HEADERS)

    @router.post("/xiaozhi/v1/image/")
    async def image_explain(request: Request) -> JSONResponse:
        # Auth check
        image_token = (config.get("server") or {}).get("image_token", "")
        if image_token:
            auth = request.headers.get("authorization", "")
            provided = auth.removeprefix("Bearer ").strip()
            if provided != image_token:
                return JSONResponse(
                    {"error": "unauthorized"}, status_code=401, headers=_CORS_HEADERS
                )

        # Parse image bytes + question
        content_type = request.headers.get("content-type", "")
        question = request.query_params.get("question", "What do you see?")
        jpeg_bytes: bytes = b""

        try:
            if "multipart/form-data" in content_type:
                form = await request.form()
                for key in ("file", "image", "photo"):
                    if key in form:
                        field_val = form[key]
                        if hasattr(field_val, "read"):
                            jpeg_bytes = await field_val.read()
                        else:
                            jpeg_bytes = str(field_val).encode()
                        break
                q = form.get("question")
                if q:
                    question = str(q)
            else:
                jpeg_bytes = await request.body()
        except ClientDisconnect:
            logger.bind(tag=_TAG).debug("Client disconnected before sending image body")
            return JSONResponse(
                {"error": "client disconnected"}, status_code=499, headers=_CORS_HEADERS
            )

        if not jpeg_bytes:
            return JSONResponse(
                {"error": "no image data"}, status_code=400, headers=_CORS_HEADERS
            )

        device_id = request.query_params.get("device_id", "")
        logger.bind(tag=_TAG).info(
            f"Image explain: {len(jpeg_bytes)} bytes, question={question!r}"
            + (f", device={device_id!r}" if device_id else "")
        )

        # Save the JPEG so the dashboard can display it
        if device_id:
            img_dir = Path("data/images") / device_id.replace(":", "-")
            img_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            img_path = img_dir / f"{ts}.jpg"
            img_path.write_bytes(jpeg_bytes)
            _session_state.set_latest_image(device_id, str(img_path))

        # Build data URL for the vision model
        b64 = base64.b64encode(jpeg_bytes).decode()
        data_url = f"data:image/jpeg;base64,{b64}"

        # Call OpenRouter vision model
        llm_cfg = (config.get("llm") or {}).get("openai") or {}
        api_key = str(llm_cfg.get("api_key", ""))
        base_url = str(llm_cfg.get("base_url") or "https://openrouter.ai/api/v1")
        vision_model = str(
            (config.get("llm") or {}).get("vision_model")
            or llm_cfg.get("model")
            or "google/gemma-3-27b-it"
        )

        payload = {
            "model": vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_url}},
                        {"type": "text", "text": question},
                    ],
                }
            ],
            "max_tokens": 512,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            body = resp.json()
            if not resp.is_success:
                logger.bind(tag=_TAG).error(
                    f"Vision API {resp.status_code}: {body}"
                )
                return JSONResponse(
                    {"error": f"API error {resp.status_code}", "text": "I couldn't describe the image."},
                    status_code=502,
                    headers=_CORS_HEADERS,
                )
            choices = body.get("choices") or []
            if not choices:
                logger.bind(tag=_TAG).error(f"Vision API empty choices: {body}")
                return JSONResponse(
                    {"error": "no choices in response", "text": "I couldn't describe the image."},
                    status_code=502,
                    headers=_CORS_HEADERS,
                )
            text = choices[0]["message"].get("content") or ""
            if not text:
                logger.bind(tag=_TAG).warning(f"Vision model returned empty content: {body}")
                text = "I can see the image but couldn't generate a description."
            logger.bind(tag=_TAG).info(f"Vision result: {text[:200]!r}")
            return JSONResponse({"text": text}, headers=_CORS_HEADERS)
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"Vision call failed: {exc}")
            return JSONResponse(
                {"error": str(exc), "text": "I couldn't describe the image."},
                status_code=502,
                headers=_CORS_HEADERS,
            )

    return router
