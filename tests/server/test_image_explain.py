"""Tests for the image explain upload endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.server import image_explain, session_state


async def test_device_image_upload_returns_fast_and_completes_background_job(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.chdir(tmp_path)

    async def fake_describe(
        config: dict[str, Any],
        jpeg_bytes: bytes,
        question: str,
    ) -> str:
        assert jpeg_bytes == b"jpeg-data"
        assert question == "what is here?"
        return "A small robot is on a desk."

    monkeypatch.setattr(image_explain, "_describe_image", fake_describe)

    app = FastAPI()
    app.include_router(image_explain.make_router({}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/xiaozhi/v1/image/?device_id=aa:bb&question=what%20is%20here%3F",
            content=b"jpeg-data",
            headers={"content-type": "image/jpeg"},
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "text": "Image received; vision processing started.",
        "status": "accepted",
    }

    text = await session_state.wait_latest_image_description(
        "aa:bb",
        previous_path=None,
        timeout=1.0,
    )
    assert text == "A small robot is on a desk."
    assert session_state.get_latest_image("aa:bb") is not None


async def test_manual_image_upload_without_device_id_remains_synchronous(monkeypatch) -> None:
    async def fake_describe(
        config: dict[str, Any],
        jpeg_bytes: bytes,
        question: str,
    ) -> str:
        return f"{question}: {len(jpeg_bytes)} bytes"

    monkeypatch.setattr(image_explain, "_describe_image", fake_describe)

    app = FastAPI()
    app.include_router(image_explain.make_router({}))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/xiaozhi/v1/image/?question=describe",
            content=b"jpeg-data",
            headers={"content-type": "image/jpeg"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"text": "describe: 9 bytes"}
