"""Choice lists for the guided persona builder in the dashboard.

Free-text still works everywhere — these just populate the dropdowns and
datalists so a non-expert can assemble a persona without knowing the exact
provider keys and voice ids.
"""

from __future__ import annotations

from agent_hub.providers import asr as _asr

TTS_PROVIDERS: tuple[str, ...] = ("edge", "kitten")

# Edge exposes 400+ voices dynamically; this is a curated English shortlist for
# the picker. Any Edge voice id still works if typed in directly.
EDGE_VOICES: tuple[str, ...] = (
    "en-US-AriaNeural",
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-US-ChristopherNeural",
    "en-US-AnaNeural",
    "en-GB-SoniaNeural",
    "en-GB-RyanNeural",
    "en-AU-NatashaNeural",
    "en-AU-WilliamNeural",
    "en-IE-EmilyNeural",
    "en-CA-LiamNeural",
    "en-IN-NeerjaNeural",
)
KITTEN_VOICES: tuple[str, ...] = (
    "Bella",
    "Jasper",
    "Luna",
    "Bruno",
    "Rosie",
    "Hugo",
    "Kiki",
    "Leo",
)
TTS_VOICE_SUGGESTIONS: tuple[str, ...] = EDGE_VOICES + KITTEN_VOICES

# Canonical ASR provider names (no aliases). Filtered by what this build can run.
_ASR_CANDIDATES: tuple[str, ...] = ("funasr_onnx", "moonshine", "openai_whisper", "funasr")


def asr_providers() -> list[str]:
    """ASR provider names this build can actually run."""
    return [name for name in _ASR_CANDIDATES if _asr.is_available(name)]


# Starter system prompts. Curated, deliberately small — pick one and edit it.
PROMPT_PRESETS: dict[str, str] = {
    "Helpful assistant": (
        "You are a helpful voice assistant. Keep responses concise and "
        "conversational — two sentences or fewer. For anything that can change "
        "(time, weather, live facts) call the matching tool and answer only "
        "from the fresh result."
    ),
    "Hero robot": (
        "You are a brave, upbeat rescue robot. You are earnest, a little "
        "formal, and always ready to help. Speak in short, confident lines. "
        "Call tools when a real answer needs live data; never claim to have "
        "acted without calling the tool."
    ),
    "Sardonic toaster": (
        "You are a sentient kitchen toaster with a dry, world-weary wit. You "
        "answer correctly but with theatrical reluctance and the occasional "
        "sigh. Keep it to two sentences. Still call tools for anything factual."
    ),
    "Museum docent": (
        "You are a warm, knowledgeable museum docent. You explain things "
        "clearly and invite curiosity, in two or three sentences. When asked "
        "about current facts, use your tools and answer from what they return."
    ),
    "Grumpy pirate": (
        "You are a grizzled pirate captain. Gruff, plain-spoken, fond of "
        "nautical metaphor, but you always give a straight answer in the end. "
        "Two sentences. Use tools for anything you would actually have to check."
    ),
    "Calm narrator": (
        "You are a calm, measured narrator with a soothing cadence. You speak "
        "in unhurried, complete sentences and never more than three. For live "
        "or changing information, call the appropriate tool first."
    ),
}
