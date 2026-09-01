"""End-to-end tests for the `scripts/reset_data.py` wipe used by `just reset-data`."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from agent_hub.registry.store import RegistryStore
from agent_hub.server import transcript_log

_SCRIPT = Path(__file__).parents[1] / "scripts" / "reset_data.py"
_DEVICE = "AA:BB:CC:DD:EE:FF"


def _load_script():
    spec = importlib.util.spec_from_file_location("reset_data", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
async def seeded(tmp_path, monkeypatch):
    """A populated data/ tree wired up via the config's env overrides."""
    data = tmp_path / "data"
    data.mkdir()
    db_path = data / "registry.db"
    image_root = data / "images"
    audio_dir = data / "debug_audio"
    transcript_path = data / "transcripts.jsonl"

    monkeypatch.setenv("AGENT_HUB_REGISTRY_DB_PATH", str(db_path))
    monkeypatch.setenv("AGENT_HUB_SERVER_DASHBOARD_IMAGE_ROOT", str(image_root))
    monkeypatch.setenv("AGENT_HUB_SERVER_DEBUG_AUDIO_DIR", str(audio_dir))
    monkeypatch.chdir(tmp_path)

    original_log_path = transcript_log.log_path()
    transcript_log.set_path(transcript_path)

    store = RegistryStore(db_path)
    await store.initialize()
    await store.get_or_create_agent(_DEVICE)
    await store.append_history(_DEVICE, "user", "what time is it")
    await store.append_history(_DEVICE, "assistant", "just past three")
    await store.record_llm_spend("gemma", 12, 4, 0.0003, False, device_id=_DEVICE)
    await store._engine.dispose()

    transcript_path.write_text('{"device_id": "AA:BB:CC:DD:EE:FF", "text": "hi"}\n')
    device_images = image_root / "AA-BB-CC-DD-EE-FF"
    device_images.mkdir(parents=True)
    (device_images / "capture-1.jpg").write_bytes(b"jpegdata")
    (device_images / "capture-2.jpg").write_bytes(b"jpegdata")
    audio_dir.mkdir()
    (audio_dir / "20260101T000000000_abc_dev.wav").write_bytes(b"wav")
    (audio_dir / "20260101T000000000_abc_dev.json").write_text("{}")

    yield {
        "module": _load_script(),
        "db_path": db_path,
        "image_root": image_root,
        "audio_dir": audio_dir,
        "transcript_path": transcript_path,
    }

    transcript_log.set_path(original_log_path)


async def _history_count(db_path: Path) -> int:
    store = RegistryStore(db_path)
    await store.initialize()
    try:
        return await store.conversation_turn_count()
    finally:
        await store._engine.dispose()


async def _spend_calls(db_path: Path) -> int:
    store = RegistryStore(db_path)
    await store.initialize()
    try:
        return int((await store.llm_spend_summary())["calls"])
    finally:
        await store._engine.dispose()


class TestDryRun:
    async def test_changes_nothing(self, seeded):
        rc = await seeded["module"]._run(["--dry-run"])

        assert rc == 0
        assert await _history_count(seeded["db_path"]) == 2
        assert seeded["transcript_path"].exists()
        assert list(seeded["image_root"].rglob("*.jpg"))
        assert list(seeded["audio_dir"].iterdir())


class TestNonInteractiveGuard:
    async def test_refuses_without_yes_and_keeps_data(self, seeded, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)

        rc = await seeded["module"]._run([])

        assert rc == 1
        assert await _history_count(seeded["db_path"]) == 2
        assert seeded["transcript_path"].exists()


class TestWipe:
    async def test_clears_session_data_but_keeps_the_registry(self, seeded):
        rc = await seeded["module"]._run(["--yes"])

        assert rc == 0
        assert await _history_count(seeded["db_path"]) == 0
        assert not seeded["transcript_path"].exists()
        assert not list(seeded["image_root"].rglob("*"))
        assert seeded["image_root"].is_dir()
        assert not list(seeded["audio_dir"].iterdir())

        # Registry survives: the device stays enrolled with its persona.
        store = RegistryStore(seeded["db_path"])
        await store.initialize()
        try:
            assert await store.get_agent(_DEVICE) is not None
            assert await store.get_persona_for_device(_DEVICE) is not None
        finally:
            await store._engine.dispose()

    async def test_keeps_the_spend_ledger_by_default(self, seeded):
        await seeded["module"]._run(["--yes"])

        assert await _spend_calls(seeded["db_path"]) == 1

    async def test_clear_spend_flag_also_resets_the_ledger(self, seeded):
        await seeded["module"]._run(["--yes", "--clear-spend"])

        assert await _spend_calls(seeded["db_path"]) == 0
