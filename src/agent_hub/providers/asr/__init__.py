"""ASR provider base class and factory."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any


@dataclass
class Transcript:
    """Result from a single ASR transcription."""

    text: str
    emotion: str = "NEUTRAL"  # SenseVoice emotion tag, uppercase
    language: str = ""  # BCP-47 detected language, empty = unknown
    is_speech: bool = True  # False when event type is non-speech (BGM, noise, etc.)

    def __bool__(self) -> bool:
        return bool(self.text) and self.is_speech


class ASRProvider(abc.ABC):
    """Abstract base for speech-to-text providers."""

    @abc.abstractmethod
    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> Transcript:
        """Transcribe WAV audio to text.

        Args:
            audio_bytes: WAV-formatted audio bytes (16-bit PCM, any sample rate).
            language: BCP-47 language code hint (e.g. 'en'). None lets the
                provider auto-detect.

        Returns:
            Transcript dataclass. Falsy (empty text or non-speech event) when
            no usable speech was detected.
        """


_cache: dict[str, ASRProvider] = {}


def get_provider(name: str, config: dict[str, Any]) -> ASRProvider:
    """Instantiate an ASR provider by name from config.

    Args:
        name: Provider key matching .config.yaml asr.<name>.
        config: Full raw config dict.

    Returns:
        Configured ASRProvider instance.

    Raises:
        ValueError: If the provider name is unknown.
    """
    if name in _cache:
        return _cache[name]

    asr_cfg: dict[str, Any] = config.get("asr", {})
    provider: ASRProvider
    if name in ("funasr_onnx", "fun_local_onnx"):
        from agent_hub.providers.asr.funasr_onnx_provider import FunASRONNXProvider

        cfg = asr_cfg.get("funasr_onnx", asr_cfg.get("funasr", {}))
        provider = FunASRONNXProvider(
            model_dir=str(cfg.get("model_dir", "models/SenseVoiceSmall-onnx")),
            language=str(cfg.get("language", "en")),
            intra_op_num_threads=int(cfg.get("intra_op_num_threads", 4)),
            quantize=bool(cfg.get("quantize", True)),
        )
    elif name in ("funasr", "fun_local"):
        from agent_hub.providers.asr.funasr_provider import FunASRProvider

        cfg = asr_cfg.get("funasr", {})
        provider = FunASRProvider(
            model_dir=str(cfg.get("model_dir", "models/SenseVoiceSmall")),
            language=str(cfg.get("language", "en")),
        )
    elif name == "openai_whisper":
        from agent_hub.providers.asr.openai_whisper import OpenAIWhisperASRProvider

        cfg = asr_cfg.get("openai_whisper", {})
        api_key = cfg.get("api_key") or config.get("llm", {}).get("openai", {}).get("api_key", "")
        provider = OpenAIWhisperASRProvider(
            api_key=str(api_key),
            model=str(cfg.get("model", "whisper-1")),
            language=cfg.get("language") or None,
        )
    else:
        raise ValueError(f"Unknown ASR provider: {name!r}")

    _cache[name] = provider
    return provider
