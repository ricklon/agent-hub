"""Prefetch the Moonshine ASR model into models/moonshine.

Run at image build time so the container is self-contained — otherwise the
first transcription request pays a ~70MB download and needs network access.

Arch and language default to the DigitalOcean deployment values and can be
overridden with AGENT_HUB_ASR_MOONSHINE_MODEL_ARCH / _LANGUAGE.
"""

import os
from pathlib import Path

import moonshine_voice as mv

_ARCH_MAP = {
    "tiny": "TINY",
    "tiny_streaming": "TINY_STREAMING",
    "base": "BASE",
    "base_streaming": "BASE_STREAMING",
    "small_streaming": "SMALL_STREAMING",
    "medium_streaming": "MEDIUM_STREAMING",
}

language = os.environ.get("AGENT_HUB_ASR_MOONSHINE_LANGUAGE", "en")
arch_name = _ARCH_MAP.get(
    os.environ.get("AGENT_HUB_ASR_MOONSHINE_MODEL_ARCH", "tiny").lower(), "TINY"
)
cache_root = Path("models/moonshine")
cache_root.mkdir(parents=True, exist_ok=True)

print(f"Downloading Moonshine {arch_name} model for '{language}'...")
model_path, actual_arch = mv.get_model_for_language(
    wanted_language=language,
    wanted_model_arch=getattr(mv.ModelArch, arch_name),
    cache_root=cache_root,
)
print(f"Moonshine model ready at {model_path} (arch: {actual_arch})")
