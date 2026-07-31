"""Opt-in capture of the audio actually fed to ASR, for accuracy debugging.

Field ASR accuracy is much worse than benchmark accuracy, and the usual
question — is the model wrong, or is the audio bad before it ever reaches the
model? — cannot be answered from transcripts alone. This writes the exact WAV
the provider received, alongside what it returned, so real utterances can be
replayed through different providers and compared (see scripts/bench_asr.py).

Off unless server.debug_audio_dir is set. It records everything said to a
device, so it is a privacy decision, not a debug flag to leave on.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

_TAG = "debug_audio"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def capture_dir(config: dict[str, Any]) -> Path | None:
    """Configured capture directory, or None when capture is disabled."""
    raw = str((config.get("server") or {}).get("debug_audio_dir") or "").strip()
    return Path(raw) if raw else None


def save(
    directory: Path,
    device_id: str,
    wav_bytes: bytes,
    *,
    transcript: str,
    provider: str,
    asr_ms: int,
    is_speech: bool,
) -> None:
    """Write one ASR input and its result. Never raises — this is diagnostics.

    Args:
        directory: Destination directory; created if absent.
        device_id: Device the audio came from; used in the filename.
        wav_bytes: Exact bytes handed to the ASR provider.
        transcript: What the provider returned.
        provider: Provider name, so mixed captures stay attributable.
        asr_ms: Transcription wall clock.
        is_speech: Provider's own speech/non-speech verdict.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3]
        # Millisecond stamps are not unique enough on their own — two turns in
        # the same millisecond would overwrite each other and silently lose a
        # capture, which is the one thing this must not do.
        stem = f"{stamp}_{secrets.token_hex(3)}_{_UNSAFE.sub('-', device_id) or 'unknown'}"
        (directory / f"{stem}.wav").write_bytes(wav_bytes)
        (directory / f"{stem}.json").write_text(
            json.dumps(
                {
                    "device_id": device_id,
                    "provider": provider,
                    "transcript": transcript,
                    "is_speech": is_speech,
                    "asr_ms": asr_ms,
                    "wav_bytes": len(wav_bytes),
                    "captured_at": stamp,
                },
                indent=2,
            )
        )
    except Exception as exc:  # pragma: no cover - diagnostics must not break a turn
        logger.bind(tag=_TAG).warning(f"Failed to capture ASR audio: {exc}")
