# agent-hub

A control plane for voice-enabled ESP32 devices and the agents that drive them.

## What this is

A self-hosted server that:

1. Accepts **check-in** requests from xiaozhi-firmware ESP32 devices and assigns
   each one a working persona (LLM + voice + prompt + tools) on first contact —
   no activation gate, no web-UI binding step.
2. Handles the live **voice session** WebSocket (ASR → LLM → TTS streaming).
3. Acts as a **registry** for all agents on the network — xiaozhi devices,
   voice agents like Talkbot, and custom AG2 agents — exposing them through a
   single dashboard.
4. Bridges **MCP tools** between agents so a tool exposed by one device can be
   called by another agent on the same hub.

It is **not** a fork of `xinnan-tech/xiaozhi-esp32-server`. It is a clean
Python reimplementation of just the parts of that project's "server-only" mode
that ESP32 devices actually need, plus the registry concept the upstream
doesn't have.

## What this is not (yet)

- Not running the FUBAR Labs Tuesday-night voice agent class. That class
  uses the upstream `xiaozhi-esp32-server` simplified-mode container,
  deployed on a laptop on FUBAR's LAN. This repo is the post-class
  successor project.
- Not a firmware project. ESP32 firmware lives in
  `78/xiaozhi-esp32` (upstream) or your fork. This repo is server-side only.
- Not a chatbot framework. Brains live in providers (OpenAI, Anthropic,
  local LLMs, Ollama, KVM@TACC, etc.). This repo orchestrates them.

## Why a new project instead of forking upstream

The upstream project's simplified mode is one Python container that does
exactly one thing: relay audio between an ESP32 and a configured LLM. There
is no per-device persona, no registry, no concept of multiple agents.

The full-module mode adds those things — but it does so with a Java
SpringBoot manager-api, a Vue admin frontend, MySQL, Redis, and a two-step
device activation flow designed for a multi-tenant cloud product. That is
the wrong shape for a homelab/classroom/maker-space hub.

This project picks the right pieces from both modes:

- From simplified: minimal dependencies, file-based config, runs in one
  container
- From full-module: the data model (templates + per-device personas), the
  check-in handler that doubles as device registration
- From neither: the activation gate (devices auto-bind to a default persona
  on first check-in)

## Naming conventions

The upstream uses some technical terms that confuse learners. This project
renames them where it makes pedagogical sense:

| Upstream / firmware                | This project              |
| ---------------------------------- | ------------------------- |
| OTA endpoint (`/xiaozhi/ota/`)     | check-in endpoint         |
| Agent / wisdom body (智能体)       | persona                   |
| Device activation                  | claiming a device         |
| Server-only mode                   | basic mode                |
| Bound to agent                     | assigned to a persona     |

The path `/xiaozhi/ota/` is preserved as a permanent alias for firmware
compatibility — once a device is flashed, its OTA URL is sticky and cannot
be changed remotely. New endpoints and documentation use `/checkin/`.

## Getting started

### Step 1 — Install the tools

You need two command-line tools before anything else.

**uv** — a fast Python package manager (replaces pip + virtualenv):

```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**just** — a command runner (like `make`, but simpler). All project tasks
are defined in `justfile`. Install it once, then use `just <target>`:

```sh
# macOS
brew install just

# Linux
curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to /usr/local/bin

# Windows (winget)
winget install Casey.Just
```

> **No `just`?** Every `just <target>` is just a shortcut. You can run the
> underlying command directly — e.g. `just run` is `uv run python -m agent_hub.server`.
> Check `justfile` in the repo root to see each command.

### Step 2 — Get an API key

agent-hub needs a language model to power conversation. The easiest option
is **OpenRouter**, which provides free access to many models and accepts a
single API key for all of them.

1. Go to [openrouter.ai](https://openrouter.ai) and sign in
2. Click your profile → **Keys** → **Create key**
3. Copy the key (starts with `sk-or-`)

You can also use OpenAI directly, a local Ollama server, or any other
OpenAI-compatible API. See `.env.example` for examples.

### Step 3 — Configure

```sh
cp .env.example .env
```

Open `.env` and set at minimum:

```
AGENT_HUB_LLM_OPENAI_API_KEY=sk-or-your-key-here
```

If your server and devices are on the same LAN (home, classroom, makerspace),
also set the WebSocket URL so devices know where to connect:

```
AGENT_HUB_SERVER_WEBSOCKET=ws://YOUR_LAN_IP:8000/xiaozhi/v1/
```

Replace `YOUR_LAN_IP` with the IP address of the machine running agent-hub
(find it with `ip route get 8.8.8.8` on Linux or `ipconfig` on Windows).

### Step 4 — Download models (first time only, ~1 GB)

agent-hub runs speech recognition locally using SenseVoiceSmall. Download
it once:

```sh
just download-models
# or without just:
uv run python scripts/download_models.py
```

This downloads to the `models/` folder and only needs to run once.

### Step 5 — Run

```sh
just run
# or without just:
uv run python -m agent_hub.server
```

Open **`http://localhost:8000/dashboard/`** in a browser. Devices will
appear there as they connect.

Three ports are used:

| Port | Purpose |
|------|---------|
| `8000` | WebSocket voice sessions + dashboard |
| `8001` | Dashboard (same app, alternate port) |
| `8003` | Device check-in / OTA endpoint |

### Docker (optional)

Docker lets you run agent-hub in a container without installing Python or
uv on your machine. It's most useful for always-on deployments (a home
server, a Raspberry Pi, a cloud VM) rather than day-to-day development.

```sh
just docker-build   # build the image (once, or after code changes)
just docker-up      # start the container
```

The container reads your `.env` file and mounts `./data` so the device
registry and transcripts persist between restarts.

### Configuration reference

All settings can be set via environment variables using the pattern
`AGENT_HUB_<SECTION>_<KEY>`. The `.env` file is the easiest place to put them.

| Variable | Default | Description |
|---|---|---|
| `AGENT_HUB_LLM_OPENAI_API_KEY` | — | **Required.** API key for the LLM |
| `AGENT_HUB_LLM_OPENAI_BASE_URL` | OpenAI | URL of the LLM API (OpenRouter, Ollama, etc.) |
| `AGENT_HUB_LLM_OPENAI_MODEL` | `gpt-4o-mini` | Model name to use |
| `AGENT_HUB_TTS_EDGE_VOICE` | `en-US-AriaNeural` | Edge TTS voice name |
| `AGENT_HUB_SERVER_WEBSOCKET` | auto-detected LAN IP | WS URL sent to devices on check-in |
| `AGENT_HUB_SERVER_WS_PORT` | `8000` | WebSocket / dashboard port |
| `AGENT_HUB_SERVER_HTTP_PORT` | `8003` | Device check-in port |

See `.env.example` for the full list with comments.

### `just` targets reference

| Target | Command | What it does |
|---|---|---|
| `just run` | `uv run python -m agent_hub.server` | Start server (all ports) |
| `just install` | `uv sync --all-extras` | Install / update dependencies |
| `just download-models` | `uv run python scripts/download_models.py` | Fetch local ASR models |
| `just docker-build` | `docker compose build` | Build Docker image |
| `just docker-up` | `docker compose up` | Run via Docker |
| `just test` | `pytest -xvs` | Run test suite |
| `just lint` | `ruff check src/ tests/` | Check code style |

## Status

Active development (Phase 1 — xiaozhi server parity). Core pipeline is
working end-to-end. See `AGENTS.md` for contribution conventions.

## Architecture (target)

```
┌───────────────────────────────────────────────────────────────┐
│                         agent-hub                              │
│                                                                │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐               │
│  │  check-in  │  │ WS session │  │ MCP bridge │               │
│  │   :8003    │  │   :8000    │  │   :8004    │               │
│  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘               │
│        │                │                │                      │
│        └────────┬───────┴────────┬───────┘                      │
│                 ▼                ▼                              │
│         ┌──────────────┐  ┌──────────────┐                     │
│         │   registry   │  │  providers   │                     │
│         │  (sqlite)    │  │ (LLM/TTS/ASR)│                     │
│         └──────────────┘  └──────────────┘                     │
│                 │                                               │
│                 ▼                                               │
│         ┌──────────────┐                                       │
│         │  dashboard   │  :8001                                │
│         └──────────────┘                                       │
└───────────────────────────────────────────────────────────────┘
       ▲                ▲                ▲
       │                │                │
   xiaozhi          Talkbot           AG2 agent
   ESP32              (voice           (custom)
   devices             agent)
```

## Roadmap

**Phase 1: xiaozhi server parity (week 1–3)**
- Check-in endpoint with auto-provisioning to a `hub-default` persona
- WebSocket session with all-API LLM/TTS/ASR
- Per-device persona instances with isolated memory
- Minimal dashboard (live device list, last-seen, current session)

**Phase 2: registry as first-class concept (week 4–5)**
- Generalize device → agent abstraction across kinds (xiaozhi, voice, mcp, ag2)
- HTTP registration endpoint for non-xiaozhi agents
- Per-kind dashboard views

**Phase 3: cross-agent features (later)**
- MCP tool calls routed between agents
- Hook to coachable-robots-bench for unified benchmarking
- Per-agent session history and token/cost accounting

## Deployment target

Designed to run as a single Docker container on the existing
`xiaozhi.local` LXC (CT 107 on edge.local, 192.168.5.6) with a Tailscale
sidecar for the dashboard at `agent-hub.panthera-hamlet.ts.net`.

For public exposure (FUBAR demos, 4-H families testing from home), swap
the Tailscale sidecar for a Cloudflare Tunnel at `agent-hub.foofab.net`.
The compose file is the same; only the sidecar changes.

## License

TBD.

## See also

- [`docs/device-setup.md`](docs/device-setup.md) — how to configure an ESP32 device to connect
- `AGENTS.md` — coding-agent instructions and skill catalogue
- Upstream xiaozhi-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- Upstream firmware: https://github.com/78/xiaozhi-esp32
