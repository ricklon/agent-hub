"""Tests for opt-in ASR audio capture."""

from __future__ import annotations

import json

from agent_hub.server import debug_audio


class TestCaptureDir:
    def test_disabled_by_default(self):
        """It records everything said to a device, so silence must be the default."""
        assert debug_audio.capture_dir({}) is None
        assert debug_audio.capture_dir({"server": {}}) is None
        assert debug_audio.capture_dir({"server": {"debug_audio_dir": ""}}) is None
        assert debug_audio.capture_dir({"server": {"debug_audio_dir": "   "}}) is None

    def test_enabled_when_set(self, tmp_path):
        cfg = {"server": {"debug_audio_dir": str(tmp_path)}}

        assert debug_audio.capture_dir(cfg) == tmp_path


class TestSave:
    def test_writes_audio_and_sidecar(self, tmp_path):
        debug_audio.save(
            tmp_path / "caps",
            "aa:bb:cc",
            b"RIFFfake",
            transcript="hello there",
            provider="moonshine",
            asr_ms=123,
            is_speech=True,
        )

        wavs = list((tmp_path / "caps").glob("*.wav"))
        sidecars = list((tmp_path / "caps").glob("*.json"))
        assert len(wavs) == 1 and len(sidecars) == 1
        assert wavs[0].read_bytes() == b"RIFFfake"
        meta = json.loads(sidecars[0].read_text())
        assert meta["transcript"] == "hello there"
        assert meta["provider"] == "moonshine"
        assert meta["asr_ms"] == 123

    def test_device_id_is_sanitised_into_the_filename(self, tmp_path):
        """MAC addresses contain colons, which are not portable in filenames."""
        debug_audio.save(
            tmp_path,
            "aa:bb:cc:dd:ee:ff",
            b"x",
            transcript="",
            provider="moonshine",
            asr_ms=1,
            is_speech=False,
        )

        name = next(tmp_path.glob("*.wav")).name
        assert ":" not in name
        assert "aa-bb-cc-dd-ee-ff" in name

    def test_failure_never_raises(self, tmp_path):
        """Diagnostics must not be able to kill a voice turn."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("i am a file")

        debug_audio.save(
            blocker / "nested",
            "dev",
            b"x",
            transcript="",
            provider="moonshine",
            asr_ms=1,
            is_speech=True,
        )

    def test_captures_do_not_collide(self, tmp_path):
        for _ in range(3):
            debug_audio.save(
                tmp_path,
                "dev",
                b"x",
                transcript="",
                provider="moonshine",
                asr_ms=1,
                is_speech=True,
            )

        assert len(list(tmp_path.glob("*.wav"))) == 3
