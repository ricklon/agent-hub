"""Shared pytest fixtures for agent-hub tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from agent_hub.config import Settings
from agent_hub.registry.store import RegistryStore
from agent_hub.server.checkin import make_router as make_checkin_router
from agent_hub.server.ws_session import make_router as make_ws_router

_TEST_CONFIG: dict = {}  # empty config; providers are not called in unit tests


@pytest.fixture()
async def store(tmp_path) -> RegistryStore:
    """In-memory SQLite registry store, initialised and torn down per test."""
    s = RegistryStore(db_path=tmp_path / "test.db")
    await s.initialize()
    yield s
    await s._engine.dispose()


@pytest.fixture()
def settings() -> Settings:
    """Default Settings with PST timezone."""
    return Settings()


@pytest.fixture()
def app(store: RegistryStore, settings: Settings) -> FastAPI:
    """FastAPI test app with checkin + WS routers mounted."""
    a = FastAPI()
    a.include_router(make_checkin_router(store, settings))
    a.include_router(make_ws_router(store, _TEST_CONFIG))
    return a


@pytest.fixture()
async def client(app: FastAPI) -> AsyncClient:
    """Async HTTP test client for the FastAPI app."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
