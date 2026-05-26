"""Append-only JSONL transcript log.

Each line is one completed voice turn:
  {"ts": "...", "device_id": "...", "text": "...", "emotion": "...",
   "language": "...", "reply": "...", "asr_ms": 0, "llm_ms": 0, "tts_ms": 0}

Written to data/transcripts.jsonl by default.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

_log_path: Path = Path("data/transcripts.jsonl")


def set_path(path: str | Path) -> None:
    global _log_path
    _log_path = Path(path)


def log_turn(
    *,
    device_id: str,
    text: str,
    emotion: str,
    language: str,
    reply: str,
    asr_ms: int,
    llm_ms: int,
    tts_ms: int,
) -> None:
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "device_id": device_id,
        "text": text,
        "emotion": emotion,
        "language": language,
        "reply": reply,
        "asr_ms": asr_ms,
        "llm_ms": llm_ms,
        "tts_ms": tts_ms,
    }
    with _log_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
