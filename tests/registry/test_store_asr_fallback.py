"""Tests that a persona never names an ASR provider this build cannot run.

A persona pointing at an absent provider is the worst kind of failure: the
microphone stays live, every transcription returns nothing, and the device
looks broken rather than misconfigured. It has now happened twice — once with
the native FunASR provider, once with funasr_onnx on the slim container image.
"""

from __future__ import annotations

from agent_hub.providers.asr import first_available, is_available
from agent_hub.registry.store import RegistryStore


class TestIsAvailable:
    def test_moonshine_is_available(self):
        assert is_available("moonshine") is True


class TestFirstAvailable:
    def test_returns_a_provider_this_build_can_run(self):
        name = first_available()
        assert name is not None
        assert is_available(name)

    def test_prefers_an_installed_preferred_name(self):
        assert first_available("moonshine") == "moonshine"

    def test_skips_an_absent_preferred_name(self):
        name = first_available("no_such_provider")
        assert name is not None and name != "no_such_provider"
        assert is_available(name)

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
    async def test_seed_never_names_an_absent_provider(self, tmp_path):
        """Even asked for a provider this build lacks, the seed picks a runnable
        one rather than leaving a dead microphone."""
        db = tmp_path / "registry.db"
        seeded = RegistryStore(db, default_asr_provider="no_such_provider")
        await seeded.initialize()
        assert is_available((await seeded.list_personas())[0].asr_provider)

    async def test_absent_provider_falls_back_on_next_start(self, tmp_path):
        """Simulates the droplet: a persona left on funasr_onnx, later opened by
        an image that ships only Moonshine."""
        db = tmp_path / "registry.db"
        seeded = RegistryStore(db, default_asr_provider="moonshine")
        await seeded.initialize()
        await seeded.update_persona("hub-default", asr_provider="funasr_onnx_gone")

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

    async def test_repair_falls_back_when_the_configured_default_is_also_absent(self, tmp_path):
        """The droplet case: a persona stuck on an absent provider, and the
        configured default this build was started with is absent too."""
        db = tmp_path / "registry.db"
        seeded = RegistryStore(db, default_asr_provider="moonshine")
        await seeded.initialize()
        await seeded.update_persona("transcriber", asr_provider="legacy_absent_provider")

        restarted = RegistryStore(db, default_asr_provider="still_not_installed")
        await restarted.initialize()

        fixed = await restarted.get_persona_by_name("transcriber")
        assert fixed is not None
        assert fixed.asr_provider not in {"legacy_absent_provider", "still_not_installed"}
        assert is_available(fixed.asr_provider)

    async def test_seeded_transcriber_inherits_a_runnable_provider(self, tmp_path):
        db = tmp_path / "registry.db"
        store = RegistryStore(db, default_asr_provider="no_such_provider")
        await store.initialize()

        transcriber = await store.get_persona_by_name("transcriber")
        assert transcriber is not None
        assert is_available(transcriber.asr_provider)
        # It matches hub-default's repaired provider.
        hub_default = await store.get_persona_by_name("hub-default")
        assert transcriber.asr_provider == hub_default.asr_provider
