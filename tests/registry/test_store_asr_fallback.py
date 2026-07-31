"""Tests that a persona never names an ASR provider this build cannot run.

A persona pointing at an absent provider is the worst kind of failure: the
microphone stays live, every transcription returns nothing, and the device
looks broken rather than misconfigured. It has now happened twice — once with
the native FunASR provider, once with funasr_onnx on the slim container image.
"""

from __future__ import annotations

from agent_hub.providers.asr import is_available
from agent_hub.registry.store import RegistryStore


class TestIsAvailable:
    def test_moonshine_is_available(self):
        assert is_available("moonshine") is True

    def test_unknown_provider_is_not_available(self):
        assert is_available("no_such_provider") is False


class TestSeeding:
    async def test_default_persona_uses_the_configured_provider(self, tmp_path):
        store = RegistryStore(tmp_path / "registry.db", default_asr_provider="moonshine")
        await store.initialize()

        personas = await store.list_personas()
        assert personas[0].asr_provider == "moonshine"

    async def test_default_provider_is_unchanged_when_not_specified(self, tmp_path):
        store = RegistryStore(tmp_path / "registry.db")
        await store.initialize()

        personas = await store.list_personas()
        assert personas[0].asr_provider == "funasr_onnx"


class TestFallback:
    async def test_absent_provider_falls_back_on_next_start(self, tmp_path):
        """Simulates the droplet: a database seeded by a full build, later
        opened by an image that ships only Moonshine."""
        db = tmp_path / "registry.db"
        seeded = RegistryStore(db, default_asr_provider="no_such_provider")
        await seeded.initialize()
        assert (await seeded.list_personas())[0].asr_provider == "no_such_provider"

        # Restart with a build whose default provider is actually installed.
        restarted = RegistryStore(db, default_asr_provider="moonshine")
        await restarted.initialize()

        assert (await restarted.list_personas())[0].asr_provider == "moonshine"

    async def test_available_provider_is_left_alone(self, tmp_path):
        db = tmp_path / "registry.db"
        store = RegistryStore(db, default_asr_provider="moonshine")
        await store.initialize()

        # A different but installed provider must not be rewritten.
        restarted = RegistryStore(db, default_asr_provider="funasr_onnx")
        await restarted.initialize()

        assert (await restarted.list_personas())[0].asr_provider == "moonshine"
