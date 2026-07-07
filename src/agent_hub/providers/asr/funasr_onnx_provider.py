"""FunASR ONNX local ASR provider using SenseVoiceSmall."""

from __future__ import annotations

import asyncio
import io
from typing import Any

import numpy as np
import numpy.typing as npt
import soundfile as sf
from loguru import logger

from agent_hub.providers.asr import ASRProvider, Transcript
from agent_hub.providers.asr.funasr_provider import parse_sensevoice_tags

_TAG = "funasr_onnx"


def _wav_bytes_to_mono_float(audio_bytes: bytes) -> npt.NDArray[np.float32]:
    audio, _sample_rate = sf.read(io.BytesIO(audio_bytes), dtype="float32", always_2d=True)
    if audio.size == 0:
        return np.array([], dtype=np.float32)
    mono = audio.mean(axis=1)
    return np.asarray(mono, dtype=np.float32)


class FunASRONNXProvider(ASRProvider):
    """Local ONNX ASR using funasr-onnx SenseVoiceSmall.

    The provider loads lazily. The default model directory is populated by
    ``scripts/download_models.py`` and contains ``model_quant.onnx``.
    """

    def __init__(
        self,
        model_dir: str = "models/SenseVoiceSmall-onnx",
        language: str = "en",
        intra_op_num_threads: int = 4,
        quantize: bool = True,
    ) -> None:
        self._model_dir = model_dir
        self._language = language
        self._intra_op_num_threads = intra_op_num_threads
        self._quantize = quantize
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is None:
            from funasr_onnx import SenseVoiceSmall  # type: ignore[import-untyped]

            logger.bind(tag=_TAG).info(f"Loading FunASR ONNX model from {self._model_dir!r}")
            self._model = SenseVoiceSmall(
                self._model_dir,
                batch_size=1,
                device_id="-1",
                intra_op_num_threads=self._intra_op_num_threads,
                quantize=self._quantize,
            )
            logger.bind(tag=_TAG).info("FunASR ONNX model ready")
        return self._model

    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> Transcript:
        lang = language or self._language

        def _run() -> Transcript:
            waveform = _wav_bytes_to_mono_float(audio_bytes)
            if waveform.size == 0:
                return Transcript(text="", is_speech=False)
            model = self._ensure_model()
            result = model(waveform, language=lang, textnorm="withitn")
            if not result:
                return Transcript(text="", is_speech=False)
            return parse_sensevoice_tags(str(result[0]))

        try:
            result = await asyncio.to_thread(_run)
            logger.bind(tag=_TAG).debug(
                f"ASR: {result.text!r} "
                f"[event={'speech' if result.is_speech else 'non-speech'} "
                f"emotion={result.emotion} lang={result.language or '?'}]"
            )
            return result
        except Exception as exc:
            logger.bind(tag=_TAG).error(f"FunASR ONNX transcription failed: {exc}")
            return Transcript(text="", is_speech=False)
