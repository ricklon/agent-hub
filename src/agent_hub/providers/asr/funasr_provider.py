"""FunASR local ASR provider using SenseVoiceSmall.

Requires: funasr, modelscope, torch (CPU build is sufficient).
Model is downloaded from HuggingFace on first use and cached in model_dir.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

from agent_hub.providers.asr import ASRProvider, Transcript

_TAG = "funasr"

_TAG_RE = re.compile(r"<\|([^|]+)\|>")

_SPEECH_EVENTS = {"Speech"}
_NON_SPEECH_EVENTS = {"BGM", "Applause", "Laughter", "Cry", "Sneeze", "Cough"}
_EMOTIONS = {"NEUTRAL", "HAPPY", "SAD", "ANGRY", "FEARFUL", "DISGUSTED", "SURPRISED"}
_LANGUAGES = {
    "zh",
    "en",
    "ja",
    "ko",
    "yue",
    "ar",
    "de",
    "es",
    "fr",
    "id",
    "it",
    "ms",
    "pt",
    "ru",
    "th",
    "tr",
    "vi",
}


def parse_sensevoice_tags(raw: str) -> Transcript:
    """Extract language/emotion/event tags then return a Transcript.

    SenseVoiceSmall output format:
      <|lang|><|EMOTION|><|EventType|><|woitn|>transcript text
    """
    tags = _TAG_RE.findall(raw)
    text = _TAG_RE.sub("", raw).strip()
    logger.bind(tag=_TAG).debug(f"SenseVoice raw={raw!r} tags={tags} text={text!r}")

    language = ""
    emotion = "NEUTRAL"
    is_speech = True  # default; explicit non-speech event flips this

    for tag in tags:
        if tag in _EMOTIONS:
            emotion = tag
        elif tag in _LANGUAGES:
            language = tag
        elif tag in _NON_SPEECH_EVENTS:
            is_speech = False
        # Speech, woitn, and unknown tags left as-is

    return Transcript(text=text, emotion=emotion, language=language, is_speech=is_speech)


class FunASRProvider(ASRProvider):
    """Local ASR using FunASR AutoModel (SenseVoiceSmall by default).

    Model is loaded lazily on first transcription call to avoid blocking
    startup. Inference runs in a thread pool via asyncio.to_thread.
    """

    def __init__(self, model_dir: str = "models/SenseVoiceSmall", language: str = "en") -> None:
        self._model_dir = model_dir
        self._language = language
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            from funasr import AutoModel  # type: ignore[import-untyped]

            logger.bind(tag=_TAG).info(f"Loading FunASR model from {self._model_dir!r}")
            self._model = AutoModel(
                model=self._model_dir,
                vad_kwargs={"max_single_segment_time": 30000},
                disable_update=True,
                hub="hf",
            )
            logger.bind(tag=_TAG).info("FunASR model ready")
        return self._model

    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> Transcript:
        lang = language or self._language

        def _run() -> Transcript:
            model = self._ensure_model()
            result = model.generate(
                input=audio_bytes,
                cache={},
                language=lang,
                use_itn=True,
                batch_size_s=60,
            )
            if not result:
                return Transcript(text="", is_speech=False)
            raw = result[0].get("text", "")
            if isinstance(raw, dict):
                raw = raw.get("content", "")
            return parse_sensevoice_tags(str(raw))

        try:
            result = await asyncio.to_thread(_run)
            logger.bind(tag=_TAG).debug(
                f"ASR: {result.text!r} "
                f"[event={'speech' if result.is_speech else 'non-speech'} "
                f"emotion={result.emotion} lang={result.language or '?'}]"
            )
            return result
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"FunASR transcription failed: {exc}")
            return Transcript(text="", is_speech=False)
