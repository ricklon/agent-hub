"""Prefetch the KittenTTS model into the Hugging Face cache.

Run at image build time so the container is self-contained — otherwise the
first synthesis pays a model download and needs network access.

Defaults to the model the provider uses; override with AGENT_HUB_TTS_KITTEN_MODEL.
"""

import os

from kittentts import KittenTTS  # type: ignore[import-untyped]

model_id = os.environ.get("AGENT_HUB_TTS_KITTEN_MODEL", "KittenML/kitten-tts-nano-0.8")

print(f"Downloading KittenTTS model {model_id}...")
KittenTTS(model_id)
print(f"KittenTTS model {model_id} cached")
