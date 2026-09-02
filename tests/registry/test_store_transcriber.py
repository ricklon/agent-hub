"""Tests for the transcriber persona: seeding, the transcription flag, migration."""

from __future__ import annotations

from agent_hub.registry.store import RegistryStore


async def test_transcriber_persona_is_seeded(store: RegistryStore) -> None:
    persona = await store.get_persona_by_name("transcriber")
    assert persona is not None
    assert persona.transcription is True
    # No LLM turn happens in this mode, so it carries no skills.
    assert persona.server_skills_list == []


async def test_hub_default_is_not_a_transcriber(store: RegistryStore) -> None:
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.transcription is False


async def test_update_and_read_back_transcription_flag(store: RegistryStore) -> None:
    ok = await store.update_persona("hub-default", transcription=True)
    assert ok is True
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.transcription is True

    await store.update_persona("hub-default", transcription=False)
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.transcription is False


async def test_omitting_transcription_leaves_it_unchanged(store: RegistryStore) -> None:
    await store.update_persona("hub-default", transcription=True)
    await store.update_persona("hub-default", system_prompt="unrelated edit")
    persona = await store.get_persona_by_name("hub-default")
    assert persona is not None
    assert persona.transcription is True


async def test_migration_adds_the_column_and_seeds_on_a_pre_existing_db(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    first = RegistryStore(db_path=db_path)
    await first.initialize()
    await first._engine.dispose()

    second = RegistryStore(db_path=db_path)
    await second.initialize()
    try:
        transcriber = await second.get_persona_by_name("transcriber")
        assert transcriber is not None and transcriber.transcription is True
        ok = await second.update_persona("hub-default", transcription=True)
        assert ok is True
    finally:
        await second._engine.dispose()
