# agent-hub

A control plane for voice-enabled ESP32 devices and the agents that drive them.

> **Working name.** `agent-hub` is a placeholder. Rename before public commits.

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

## Status

Pre-alpha. Repo scaffolded, no code written yet. See `AGENTS.md` for the
intended structure and conventions before contributing.

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

- `AGENTS.md` — coding-agent instructions and skill catalogue
- Upstream xiaozhi-server: https://github.com/xinnan-tech/xiaozhi-esp32-server
- Upstream firmware: https://github.com/78/xiaozhi-esp32
