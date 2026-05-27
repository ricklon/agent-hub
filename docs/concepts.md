# Concepts

This page explains the ideas behind agent-hub for people who are new to
voice AI, ESP32 devices, or the xiaozhi ecosystem.

## The big picture

agent-hub is a server that gives voice to small hardware devices. You flash
a device with firmware, plug it in, and it can hold a spoken conversation —
powered by whatever AI model you choose. The server handles all the hard
parts: turning speech into text, sending it to the AI, turning the reply
back into speech, and streaming the audio back to the device.

```
You speak → device → agent-hub → AI model → agent-hub → device → you hear
```

The device is just a microphone, speaker, and WiFi radio. All the
intelligence lives on the server.

## What is an ESP32?

The ESP32 is a small, cheap microcontroller with built-in WiFi. It costs
a few dollars, runs on USB power, and fits in your hand. The boards
supported here (XIAO ESP32-S3 Sense, Waveshare ESP32-S3) add a microphone,
speaker, and sometimes a camera or touchscreen.

These boards run firmware — low-level software burned onto the chip. The
firmware used here is [ricklon/xiaozhi-esp32](https://github.com/ricklon/xiaozhi-esp32),
a fork of the open-source xiaozhi-esp32 project that adds a local-server
workflow, per-board build tooling (`switch-board.sh`), and serial commands
for configuration (`!server`, `!wifi`, `!status`). The firmware handles
wake-word detection, audio encoding, and WiFi; it does not do any AI itself.

## The voice pipeline

Every time you speak to a device, agent-hub runs three steps in sequence:

**ASR — Automatic Speech Recognition**
Converts the audio recorded by the device's microphone into text. agent-hub
uses [SenseVoiceSmall](https://github.com/FunAudioLLM/SenseVoice) by
default, a small model that runs locally on your server. It also detects
the language and the speaker's emotion.

**LLM — Large Language Model**
Takes the transcript and produces a reply. agent-hub uses any
OpenAI-compatible API — OpenRouter, OpenAI directly, a local Ollama server,
etc. The model never sees audio; it only sees text and tool results.

**TTS — Text-to-Speech**
Converts the LLM's reply back to audio and streams it to the device.
agent-hub uses Microsoft Edge TTS by default (free, no key required, many
voices and languages). KittenTTS is also supported for higher-quality
local synthesis.

## What is a persona?

A persona is a named configuration bundle: which AI model to use, which
voice, what system prompt to give the AI, and which tools to enable. Every
device is assigned a persona. Multiple devices can share a persona, or each
device can have its own.

The default persona is called `hub-default`. You can create new ones from
the dashboard and assign them to specific devices. Examples of things you
might do with personas:

- Give one device a British accent and a formal tone, another a casual tone
- Point a classroom device at a free model, a demo device at a better one
- Restrict one device to only use time and weather tools, not web search

## What are MCP tools?

MCP (Model Context Protocol) is a standard way for AI models to call
external functions. In agent-hub, tools come from two places:

**Server-side skills** are Python functions running on the server —
`get_current_time`, `get_weather`, `web_search`. They're always available
to every device.

**Device-side MCP tools** are functions the device firmware exposes —
things like `self.audio_speaker.set_volume`, `self.screen.set_brightness`,
or `self_camera_take_photo` (on boards with a camera). The device
advertises these tools when it connects, and the server passes them to the
LLM so it knows what actions it can take on the device.

When you ask the assistant "turn the volume up", the LLM calls the
device's `set_volume` tool rather than just describing how to do it.

## What is check-in?

When a device boots and connects to WiFi, the first thing it does is send
an HTTP POST to agent-hub's check-in endpoint
(`/xiaozhi/ota/` — the name comes from the original firmware where this
was used for over-the-air firmware updates). agent-hub uses this request to:

1. Register the device if it's new (auto-assigns the `hub-default` persona)
2. Tell the device where to connect for voice sessions (the WebSocket URL)
3. Tell the device the current time and timezone

No account creation, no pairing code, no activation step. First contact
is enough.

## What is the registry?

The registry is a SQLite database (`data/registry.db`) that keeps track of
every device that has ever checked in — its ID, IP address, assigned
persona, and connection status. The dashboard reads from this registry to
show you what's connected and what's happening.

## What is the dashboard?

The dashboard is a web UI built into agent-hub. Open it at
`http://YOUR_SERVER_IP:8000/dashboard/`. It shows:

- All registered devices and their status (discovered / active / idle)
- Live conversation history per device
- Latency stats (how long ASR, LLM, and TTS each took)
- Controls to send a message to a device, reboot it, or clear its history
- A personas page to create and edit personas
- A model picker to browse OpenRouter models and switch the active model

## How agent-hub relates to the upstream project

The [xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)
is the original server for xiaozhi firmware devices. It has two modes:

- **Simplified mode** — one Python container, one device, no web UI
- **Full-module mode** — Java backend, Vue frontend, MySQL, Redis, multi-tenant

agent-hub is a clean reimplementation for a third use case: a small
classroom or makerspace with a handful of devices, where you want per-device
personas and a simple dashboard, but not the full enterprise stack.
