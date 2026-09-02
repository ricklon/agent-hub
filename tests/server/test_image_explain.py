"""Tests for the image explain upload endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.server import image_explain, session_state


async def test_device_image_upload_returns_fast_and_completes_background_job(
    monkeypatch,
    tmp_path,
    store,
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
    app.include_router(image_explain.make_router({}, store))

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


async def test_manual_image_upload_without_device_id_remains_synchronous(
    monkeypatch, store
) -> None:
    async def fake_describe(
        config: dict[str, Any],
        jpeg_bytes: bytes,
        question: str,
    ) -> str:
        return f"{question}: {len(jpeg_bytes)} bytes"

    monkeypatch.setattr(image_explain, "_describe_image", fake_describe)

    app = FastAPI()
    app.include_router(image_explain.make_router({}, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/xiaozhi/v1/image/?question=describe",
            content=b"jpeg-data",
            headers={"content-type": "image/jpeg"},
        )

    assert resp.status_code == 200
    assert resp.json() == {"text": "describe: 9 bytes"}


async def _wait_history(store, device_id: str, timeout: float = 1.0) -> list[dict[str, str]]:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        history = await store.load_history(device_id)
        if history:
            return history
        await asyncio.sleep(0.02)
    return await store.load_history(device_id)


async def test_transcript_photo_is_captioned_and_appended(
    monkeypatch,
    tmp_path,
    store,
) -> None:
    monkeypatch.chdir(tmp_path)
    await store.get_or_create_agent("aa:bb")

    async def fake_describe(config, jpeg_bytes, question) -> str:
        assert jpeg_bytes == b"jpeg-data"
        return "Two people at a workbench with a soldering iron."

    monkeypatch.setattr(image_explain, "_describe_image", fake_describe)

    app = FastAPI()
    app.include_router(image_explain.make_router({}, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/xiaozhi/v1/image/?device_id=aa:bb",
            data={"purpose": "transcript"},
            files={"file": ("camera.jpg", b"jpeg-data", "image/jpeg")},
        )

    assert resp.status_code == 200
    assert resp.json() == {"text": "Photo added to transcript.", "status": "accepted"}
    history = await _wait_history(store, "aa:bb")
    assert len(history) == 1
    assert history[0]["role"] == "image"
    assert history[0]["content"].startswith("[image:data/images/aa-bb/")
    assert history[0]["content"].endswith(" Two people at a workbench with a soldering iron.")


async def test_transcript_photo_still_lands_when_vision_fails(
    monkeypatch,
    tmp_path,
    store,
) -> None:
    monkeypatch.chdir(tmp_path)
    await store.get_or_create_agent("aa:bb")

    async def boom(*_args, **_kwargs) -> str:
        raise RuntimeError("vision down")

    monkeypatch.setattr(image_explain, "_describe_image", boom)

    app = FastAPI()
    app.include_router(image_explain.make_router({}, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/xiaozhi/v1/image/?device_id=aa:bb&purpose=transcript",
            content=b"jpeg-data",
            headers={"content-type": "image/jpeg"},
        )

    assert resp.status_code == 200
    history = await _wait_history(store, "aa:bb")
    assert len(history) == 1
    assert history[0]["content"].startswith("[image:data/images/aa-bb/")
    assert history[0]["content"].endswith(".jpg]")  # bare marker, no caption


async def test_transcriber_persona_routes_photos_to_the_transcript(
    monkeypatch,
    tmp_path,
    store,
) -> None:
    """No ?purpose=transcript, but the assigned persona is a transcriber."""
    monkeypatch.chdir(tmp_path)
    await store.get_or_create_agent("cc:dd")
    await store.assign_persona("cc:dd", "transcriber")

    async def fake_describe(config, jpeg_bytes, question) -> str:
        return "A whiteboard covered in architecture boxes."

    monkeypatch.setattr(image_explain, "_describe_image", fake_describe)

    app = FastAPI()
    app.include_router(image_explain.make_router({}, store))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/xiaozhi/v1/image/?device_id=cc:dd",
            content=b"jpeg-data",
            headers={"content-type": "image/jpeg"},
        )

    assert resp.json()["status"] == "accepted"
    history = await _wait_history(store, "cc:dd")
    assert history[0]["role"] == "image"
    assert history[0]["content"].endswith(" A whiteboard covered in architecture boxes.")
