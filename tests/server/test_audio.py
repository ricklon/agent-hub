"""Tests for audio codec and VAD helpers in server/audio.py."""

from __future__ import annotations

import io
import struct
import wave

import numpy as np
import pytest

from agent_hub.server.audio import (
    OpusDecoder,
    OpusEncoder,
    SilenceVAD,
    pcm_to_wav,
    rms_energy,
)

# OpusEncoder default frame_duration_ms changed to 60ms to match device.
# Tests that rely on 20ms frame boundaries pass explicit frame_duration_ms=20.


def _silence_pcm(samples: int = 320) -> bytes:
    return b"\x00" * (samples * 2)


def _tone_pcm(
    samples: int = 320, amplitude: int = 8000, freq: int = 440, rate: int = 16000
) -> bytes:
    t = np.arange(samples, dtype=np.float32) / rate
    wave_data = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)
    return wave_data.tobytes()


class TestOpusRoundtrip:
    def test_encode_decode_silence(self):
        """Encode silence → decode → get silence-like PCM back (60ms frames)."""
        enc = OpusEncoder(sample_rate=16000, frame_duration_ms=60)
        dec = OpusDecoder(sample_rate=16000, frame_duration_ms=60)
        pcm_in = _silence_pcm(960)  # one 60ms frame at 16kHz
        packets = enc.encode(pcm_in)
        assert len(packets) == 1
        pcm_out = dec.decode(packets[0])
        assert len(pcm_out) == 960 * 2  # 960 int16 samples

    def test_encode_multi_frame(self):
        """Two full 60ms frames → two Opus packets."""
        enc = OpusEncoder(sample_rate=16000, frame_duration_ms=60)
        pcm = _silence_pcm(1920)  # 120ms at 16kHz
        packets = enc.encode(pcm)
        assert len(packets) == 2

    def test_encode_pads_partial_frame(self):
        """A partial frame is zero-padded to a full Opus packet."""
        enc = OpusEncoder(sample_rate=16000, frame_duration_ms=60)
        pcm = _silence_pcm(100)  # less than one 60ms frame
        packets = enc.encode(pcm)
        assert len(packets) == 1

    def test_roundtrip_tone(self):
        """Encode a tone at 16 kHz with 60ms frames, decode it back, check it's non-silent."""
        enc = OpusEncoder(sample_rate=16000, frame_duration_ms=60)
        dec = OpusDecoder(sample_rate=16000, frame_duration_ms=60)
        pcm_in = _tone_pcm(samples=960, rate=16000)  # 60ms at 16kHz
        packets = enc.encode(pcm_in)
        pcm_out = dec.decode(packets[0])
        samples = np.frombuffer(pcm_out, dtype=np.int16)
        assert np.abs(samples).max() > 0  # not silence after roundtrip


class TestPcmToWav:
    def test_wav_header_is_valid(self):
        pcm = _silence_pcm(3200)  # 200ms at 16 kHz
        wav = pcm_to_wav(pcm, sample_rate=16000)
        with wave.open(io.BytesIO(wav)) as w:
            assert w.getframerate() == 16000
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getnframes() == 3200

    def test_wav_contains_pcm_data(self):
        pcm = _tone_pcm(320)
        wav = pcm_to_wav(pcm, sample_rate=16000)
        with wave.open(io.BytesIO(wav)) as w:
            assert w.readframes(320) == pcm


class TestRmsEnergy:
    def test_silence_is_zero(self):
        assert rms_energy(_silence_pcm()) == pytest.approx(0.0)

    def test_tone_is_nonzero(self):
        assert rms_energy(_tone_pcm()) > 100.0

    def test_empty_is_zero(self):
        assert rms_energy(b"") == pytest.approx(0.0)

    def test_max_amplitude_approaches_32767(self):
        # all samples at max int16 value
        pcm = struct.pack("<320h", *([32767] * 320))
        assert rms_energy(pcm) == pytest.approx(32767.0, rel=1e-3)


class TestSilenceVAD:
    """Tests for the fallback RMS-based VAD using 20ms test packets."""

    def _enc(self) -> OpusEncoder:
        return OpusEncoder(16000, frame_duration_ms=20)

    def _make_speech_packet(self, enc: OpusEncoder) -> bytes:
        pcm = _tone_pcm(320, amplitude=8000)
        return enc.encode(pcm)[0]

    def _make_silence_packet(self, enc: OpusEncoder) -> bytes:
        return enc.encode(_silence_pcm(320))[0]

    def test_no_fire_without_speech(self):
        enc = self._enc()
        vad = SilenceVAD(16000, frame_duration_ms=20)
        for _ in range(50):
            assert vad.push(self._make_silence_packet(enc)) is False

    def test_fires_after_speech_then_silence(self):
        enc = self._enc()
        vad = SilenceVAD(16000, frame_duration_ms=20)
        for _ in range(10):
            vad.push(self._make_speech_packet(enc))
        fired = False
        for _ in range(SilenceVAD.SILENCE_FRAMES + 5):
            if vad.push(self._make_silence_packet(enc)):
                fired = True
                break
        assert fired

    def test_take_returns_all_frames_and_resets(self):
        enc = self._enc()
        vad = SilenceVAD(16000, frame_duration_ms=20)
        for _ in range(5):
            vad.push(self._make_speech_packet(enc))
        for _ in range(SilenceVAD.SILENCE_FRAMES + 1):
            vad.push(self._make_silence_packet(enc))
        frames = vad.take()
        assert len(frames) == 5 + SilenceVAD.SILENCE_FRAMES + 1
        assert vad.push(self._make_silence_packet(enc)) is False

    def test_fires_at_max_frames(self):
        enc = self._enc()
        vad = SilenceVAD(16000, frame_duration_ms=20)
        fired = False
        for _ in range(SilenceVAD.MAX_FRAMES + 5):
            if vad.push(self._make_speech_packet(enc)):
                fired = True
                break
        assert fired
