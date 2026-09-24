"""Choice lists for the guided persona builder in the dashboard.

Free-text still works everywhere — these just populate the dropdowns and
datalists so a non-expert can assemble a persona without knowing the exact
provider keys and voice ids.
"""

from __future__ import annotations

from agent_hub.providers import asr as _asr

TTS_PROVIDERS: tuple[str, ...] = ("edge", "kitten", "openrouter")

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
# OpenRouter voices depend on the model; these are the 30 prebuilt voices of
# Gemini 3.8 Flash Lite TTS (the default) and Flash TTS. Other models' voice
# ids, or Gemini voice_... ids from Voice Design, can be typed in; a voice the
# model rejects falls back to the configured one with a visible notice.
OPENROUTER_VOICES: tuple[str, ...] = (
    "Kore",
    "Puck",
    "Zephyr",
    "Charon",
    "Fenrir",
    "Leda",
    "Orus",
    "Aoede",
    "Callirrhoe",
    "Autonoe",
    "Enceladus",
    "Iapetus",
    "Umbriel",
    "Algieba",
    "Despina",
    "Erinome",
    "Algenib",
    "Rasalgethi",
    "Laomedeia",
    "Achernar",
    "Alnilam",
    "Schedar",
    "Gacrux",
    "Pulcherrima",
    "Achird",
    "Zubenelgenubi",
    "Vindemiatrix",
    "Sadachbia",
    "Sadaltager",
    "Sulafat",
)
TTS_VOICE_SUGGESTIONS: tuple[str, ...] = EDGE_VOICES + KITTEN_VOICES + OPENROUTER_VOICES

# Which voices belong to which voice system. Edge accepts any of its 400+
# voice ids if typed in, so its list is a suggestion; Kitten ships exactly
# these eight, so anything else is a save-time error rather than a runtime one.
VOICES_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "edge": EDGE_VOICES,
    "kitten": KITTEN_VOICES,
    "openrouter": OPENROUTER_VOICES,
}
FIXED_VOICE_PROVIDERS: frozenset[str] = frozenset({"kitten"})

# Models a persona can pick within each voice system (blank = the hub's
# configured default). OpenRouter takes any TTS model id it serves; these are
# the suggestions. Kitten models are downloaded on first use, so only the
# known ones are accepted. Edge has no model choice.
TTS_MODELS_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "openrouter": ("google/gemini-3.8-flash-lite-tts", "google/gemini-3.8-flash-tts"),
    "kitten": (
        "KittenML/kitten-tts-nano-0.8",
        "KittenML/kitten-tts-nano-0.8-int8",
        "KittenML/kitten-tts-micro-0.8",
        "KittenML/kitten-tts-mini-0.8",
    ),
}
FIXED_MODEL_PROVIDERS: frozenset[str] = frozenset({"kitten"})


def tts_models_for(tts_provider: str) -> tuple[str, ...]:
    """TTS model ids to offer for one voice system (empty when it has none)."""
    return TTS_MODELS_BY_PROVIDER.get(tts_provider, ())


def tts_model_problem(tts_provider: str, model: str) -> str | None:
    """Why ``model`` cannot be used with ``tts_provider``, or None if it can.

    Blank always passes (it means the configured default).
    """
    if not model:
        return None
    if tts_provider not in TTS_MODELS_BY_PROVIDER:
        return f"{tts_provider} has no model choice; leave the TTS model blank."
    if tts_provider in FIXED_MODEL_PROVIDERS and model not in TTS_MODELS_BY_PROVIDER[tts_provider]:
        options = ", ".join(TTS_MODELS_BY_PROVIDER[tts_provider])
        return f"{model!r} is not a {tts_provider} model. Choose one of: {options}."
    return None


def voices_for(tts_provider: str) -> tuple[str, ...]:
    """Voice ids to offer for one TTS system (empty for an unknown system)."""
    return VOICES_BY_PROVIDER.get(tts_provider, ())


def voice_problem(tts_provider: str, voice: str) -> str | None:
    """Why ``voice`` cannot be used with ``tts_provider``, or None if it can.

    Only systems with a fixed voice set are checked. A blank voice always
    passes (it means "the system default").
    """
    voice = voice.strip()
    if not voice or tts_provider not in FIXED_VOICE_PROVIDERS:
        return None
    if voice in VOICES_BY_PROVIDER.get(tts_provider, ()):
        return None
    options = ", ".join(VOICES_BY_PROVIDER.get(tts_provider, ()))
    return f"{voice!r} is not a {tts_provider} voice. Choose one of: {options}."


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
