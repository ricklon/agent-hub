"""Moonshine ASR provider — lightweight on-device speech-to-text.

Uses the moonshine-voice Python package (ONNX-based, 34M-245M params) which
is dramatically smaller and faster than SenseVoice/Whisper while matching or
beating Whisper Large v3 accuracy at the medium tier.

Model sizes:
  - TINY:          34M params,  ~70MB, 12.0% WER, 69ms latency (Linux x86)
  - TINY_STREAMING: 34M params, streaming with caching
  - SMALL_STREAMING: 123M params, ~250MB, 7.8% WER, 165ms
  - MEDIUM_STREAMING: 245M params, ~500MB, 6.7% WER, 269ms (beats Whisper Large v3)

Languages: en, es, zh, ja, ko, vi, uk, ar

No API keys, no cloud calls — everything runs on-device via ONNX Runtime.
"""

from __future__ import annotations

import io
import wave
from typing import Any

from loguru import logger

from agent_hub.providers.asr import ASRProvider, Transcript

_TAG = "moonshine_asr"

# Map config string → moonshine ModelArch enum
_ARCH_MAP: dict[str, str] = {
    "tiny": "TINY",
    "tiny_streaming": "TINY_STREAMING",
    "base": "BASE",
    "base_streaming": "BASE_STREAMING",
    "small_streaming": "SMALL_STREAMING",
    "medium_streaming": "MEDIUM_STREAMING",
}


def _wav_to_float_samples(wav_bytes: bytes) -> tuple[list[float], int]:
    """Decode WAV bytes to mono float32 samples in [-1.0, 1.0] and sample rate."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        n_channels = w.getnchannels()
        sample_rate = w.getframerate()
        sample_width = w.getsampwidth()
        raw = w.readframes(w.getnframes())

    if sample_width != 2:
        raise ValueError(f"Moonshine ASR expects 16-bit PCM WAV, got {sample_width * 8}-bit")

    import struct

    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    if n_channels > 1:
        samples = samples[::n_channels]
    float_samples = [s / 32768.0 for s in samples]
    return float_samples, sample_rate


class MoonshineASRProvider(ASRProvider):
    """ASR provider using the Moonshine ONNX models via moonshine-voice."""

    def __init__(
        self,
        language: str = "en",
        model_arch: str = "tiny",
        cache_root: str = "models/moonshine",
    ) -> None:
        """Create a Moonshine ASR provider.

        Args:
            language: BCP-47 language code (en, es, zh, ja, ko, vi, uk, ar).
            model_arch: Model architecture — one of tiny, tiny_streaming,
                base, base_streaming, small_streaming, medium_streaming.
            cache_root: Directory for downloaded model files.
        """
        from pathlib import Path

        import moonshine_voice as mv

        self._language = language
        arch_name = _ARCH_MAP.get(model_arch.lower(), "TINY")
        self._model_arch = getattr(mv.ModelArch, arch_name)

        model_path, actual_arch = mv.get_model_for_language(
            wanted_language=language,
            wanted_model_arch=self._model_arch,
            cache_root=Path(cache_root),
        )
        self._model_path = str(model_path)
        logger.bind(tag=_TAG).info(
            f"Loaded Moonshine model: {language} / {arch_name} from {self._model_path}"
        )

        self._transcriber: Any | None = None

    def _get_transcriber(self) -> Any:
        """Lazy-init the transcriber on first use (avoids loading at import time)."""
        if self._transcriber is None:
            from moonshine_voice import Transcriber

            self._transcriber = Transcriber(
                model_path=self._model_path,
                model_arch=self._model_arch,
            )
        return self._transcriber

    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> Transcript:
        """Transcribe WAV audio to text using Moonshine.

        Args:
            audio_bytes: WAV-formatted audio bytes (16-bit PCM, any sample rate).
            language: BCP-47 language code hint. Ignored — Moonshine uses the
                language specified at init.

        Returns:
            Transcript dataclass with recognized text.
        """
        try:
            samples, sample_rate = _wav_to_float_samples(audio_bytes)
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"Failed to decode WAV: {exc}")
            return Transcript(text="", is_speech=False)

        if not samples:
            return Transcript(text="", is_speech=False)

        transcriber = self._get_transcriber()
        try:
            result = transcriber.transcribe_without_streaming(samples, sample_rate=sample_rate)
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"Moonshine transcription failed: {exc}")
            return Transcript(text="", is_speech=False)

        # Extract text from all completed lines
        lines = result.lines if hasattr(result, "lines") else []
        text = " ".join(line.text for line in lines if line.text).strip()

        if not text:
            return Transcript(text="", is_speech=False)

        logger.bind(tag=_TAG).debug(f"Moonshine: {text!r}")
        return Transcript(text=text, language=self._language, is_speech=True)
