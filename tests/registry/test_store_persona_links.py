"""Tests for persona.linked_agents storage + migration."""

from __future__ import annotations

from agent_hub.registry.store import RegistryStore


async def test_linked_agents_defaults_to_empty_list(store: RegistryStore) -> None:
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.linked_agents_list == []


async def test_update_and_read_back_linked_agents(store: RegistryStore) -> None:
    ok = await store.update_persona("hub-default", linked_agents='["robot-01", "page-abc"]')
    assert ok is True
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.linked_agents_list == ["robot-01", "page-abc"]


async def test_empty_string_clears_linked_agents(store: RegistryStore) -> None:
    await store.update_persona("hub-default", linked_agents='["robot-01"]')
    await store.update_persona("hub-default", linked_agents="")
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.linked_agents_list == []
    assert persona.linked_agents is None


async def test_migration_adds_the_column_to_a_pre_existing_db(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    first = RegistryStore(db_path=db_path)
    await first.initialize()
    await first._engine.dispose()

    second = RegistryStore(db_path=db_path)
    await second.initialize()
    try:
        ok = await second.update_persona("hub-default", linked_agents='["robot-01"]')
        assert ok is True
        persona = await second.get_persona_by_name("hub-default")
        assert persona is not None
        assert persona.linked_agents_list == ["robot-01"]
    finally:
        await second._engine.dispose()
