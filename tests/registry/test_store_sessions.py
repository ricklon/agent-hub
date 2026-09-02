"""Transcription-session grouping on conversation history."""

from __future__ import annotations

from agent_hub.registry.store import RegistryStore


async def test_append_and_load_a_single_session(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB")
    await store.append_history("AA:BB", "transcript", "line one", session_id="S1")
    await store.append_history("AA:BB", "image", "[image:x.jpg] a bench", session_id="S1")
    await store.append_history("AA:BB", "transcript", "line two", session_id="S1")

    turns = await store.load_session("AA:BB", "S1")
    assert [t["content"] for t in turns] == ["line one", "[image:x.jpg] a bench", "line two"]


async def test_load_session_defaults_to_the_latest(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB")
    await store.append_history("AA:BB", "transcript", "old session line", session_id="S1")
    await store.append_history("AA:BB", "transcript", "new session line", session_id="S2")

    assert await store.latest_session_id("AA:BB") == "S2"
    turns = await store.load_session("AA:BB")
    assert [t["content"] for t in turns] == ["new session line"]


async def test_load_session_is_uncapped(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB")
    for i in range(130):
        await store.append_history("AA:BB", "transcript", f"utterance {i}", session_id="S1")

    turns = await store.load_session("AA:BB", "S1")
    assert len(turns) == 130
    assert turns[0]["content"] == "utterance 0"
    assert turns[-1]["content"] == "utterance 129"


async def test_load_session_returns_empty_when_no_session_exists(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB")
    await store.append_history("AA:BB", "user", "hi")  # assistant turn, no session
    assert await store.latest_session_id("AA:BB") is None
    assert await store.load_session("AA:BB") == []


async def test_list_sessions_groups_newest_first(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB")
    await store.append_history("AA:BB", "transcript", "a", session_id="S1")
    await store.append_history("AA:BB", "transcript", "b", session_id="S1")
    await store.append_history("AA:BB", "transcript", "c", session_id="S2")

    sessions = await store.list_sessions("AA:BB")
    assert [s["session_id"] for s in sessions] == ["S2", "S1"]
    assert sessions[1]["turns"] == 2
    assert sessions[0]["started_at"] is not None


async def test_export_history_scoped_to_a_session(store: RegistryStore) -> None:
    await store.get_or_create_agent("AA:BB")
    await store.append_history("AA:BB", "transcript", "s1 line", session_id="S1")
    await store.append_history("AA:BB", "transcript", "s2 line", session_id="S2")

    assert [t["content"] for t in await store.export_history("AA:BB", session_id="S1")] == [
        "s1 line"
    ]
    assert len(await store.export_history("AA:BB")) == 2  # unscoped = everything


async def test_migration_adds_session_id_to_a_pre_existing_db(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    first = RegistryStore(db_path=db_path)
    await first.initialize()
    await first._engine.dispose()

    second = RegistryStore(db_path=db_path)
    await second.initialize()
    try:
        await second.get_or_create_agent("AA:BB")
        await second.append_history("AA:BB", "transcript", "x", session_id="S1")
        assert await second.latest_session_id("AA:BB") == "S1"
    finally:
        await second._engine.dispose()
