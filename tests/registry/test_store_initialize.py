"""Tests for concurrent and repeated RegistryStore.initialize()."""

from __future__ import annotations

import asyncio

from agent_hub.registry.store import RegistryStore


class TestConcurrentInitialize:
    async def test_parallel_initialize_on_fresh_db_succeeds(self, tmp_path):
        """The server binds one app per port and each startup hook initializes
        the shared store. On a fresh database they all pass create_all's
        existence check together, so the losers used to fail with
        "table personas already exists" and abort startup."""
        store = RegistryStore(db_path=tmp_path / "registry.db")

        await asyncio.gather(*(store.initialize() for _ in range(3)))

        personas = await store.list_personas()
        assert sorted(p.name for p in personas) == ["hub-default", "transcriber"]

    async def test_repeated_initialize_does_not_duplicate_the_default_persona(self, tmp_path):
        store = RegistryStore(db_path=tmp_path / "registry.db")

        await store.initialize()
        await store.initialize()

        personas = await store.list_personas()
        assert sorted(p.name for p in personas) == ["hub-default", "transcriber"]
