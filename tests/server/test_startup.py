"""Tests for server startup behavior."""

from __future__ import annotations

import asyncio

from agent_hub.config import RegistryConfig, Settings
from agent_hub.server import __main__ as server_main


async def test_startup_retains_provider_prewarm_task(monkeypatch, tmp_path):
    async def fake_prewarm(_config):
        await asyncio.sleep(60)

    settings = Settings(registry=RegistryConfig(db_path=str(tmp_path / "registry.db")))
    monkeypatch.setattr(server_main, "load_config", lambda: {})
    monkeypatch.setattr(server_main, "load_settings", lambda: settings)
    monkeypatch.setattr(server_main, "_prewarm_providers", fake_prewarm)

    app = server_main.build_app()
    for handler in app.router.on_startup:
        await handler()

    task = server_main.prewarm_task(app)
    assert task is not None
    assert not task.done()

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
