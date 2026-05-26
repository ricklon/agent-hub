# AGENTS.md

Instructions for coding agents (Claude Code, Codex, OpenCode, Cursor)
working in this repo.

## Project purpose (one paragraph)

`agent-hub` is a Python server that acts as the control plane for voice-
enabled ESP32 devices running xiaozhi firmware, plus a registry for other
voice / MCP / AG2 agents on the same network. It reimplements the
device-facing endpoints of the upstream `xiaozhi-esp32-server` project's
simplified mode in a single container, adds per-device persona management
without an activation gate, and surfaces all registered agents through one
dashboard.

## Project status

Pre-alpha. Scaffolding only. No `src/` code exists yet.

When asked to implement something, **read the existing code first** even if
it's a stub. Do not assume the design described here is the design that
exists.

## Architecture in two sentences

The check-in endpoint (`/checkin/`, aliased to `/xiaozhi/ota/` for firmware
compatibility) is the device's first contact and creates a registry entry
on first sight, auto-binding to a `hub-default` persona so the device is
functional immediately. The WebSocket session endpoint (`/xiaozhi/v1/`)
streams ASR/LLM/TTS for live conversations using the persona configured
on the registry row for that device.

## Repo layout (target)

```
agent-hub/
├── README.md
├── AGENTS.md                ← you are here
├── pyproject.toml           ← uv, hatchling, ruff, pytest
├── justfile                 ← all dev commands
├── docker-compose.yml
├── docker-compose.fubar.yml ← class-night override (laptop on FUBAR wifi)
├── .config.example.yaml
├── src/agent_hub/
│   ├── __init__.py
│   ├── server/
│   │   ├── checkin.py       ← `/checkin/` and `/xiaozhi/ota/` alias
│   │   ├── ws_session.py    ← `/xiaozhi/v1/` voice loop
│   │   ├── mcp_bridge.py    ← MCP tool routing between agents
│   │   └── protocol.py      ← message types and JSON schemas
│   ├── providers/
│   │   ├── llm/             ← OpenAI, Anthropic, Ollama, KVM@TACC, …
│   │   ├── tts/             ← Edge, Cartesia, ElevenLabs, …
│   │   └── asr/             ← cloud Whisper, Deepgram, FunASR, …
│   ├── registry/
│   │   ├── models.py        ← Agent, Device, Persona, AgentKind enum
│   │   └── store.py         ← SQLite-backed persistence
│   ├── dashboard/
│   │   └── app.py           ← FastAPI + HTMX (or React if scope grows)
│   └── config.py            ← loads .config.yaml + env overrides
├── tests/
├── skills/                  ← see "Skill catalogue" below
└── docs/
```

## Conventions

Match the homelab toolchain. Do not introduce alternatives without asking.

| Domain         | Choice                                                      |
| -------------- | ----------------------------------------------------------- |
| Package mgr    | `uv` (`uv add`, `uv sync`, `uv run`)                        |
| Build backend  | `hatchling` in `pyproject.toml`                             |
| Layout         | `src/` layout (`src/agent_hub/...`)                         |
| Lint / format  | `ruff` (lint and format both)                               |
| Type check     | `mypy --strict` on `src/`, tolerant on `tests/`             |
| Tests          | `pytest`, async via `pytest-asyncio`                        |
| Task runner    | `just` (not Make)                                           |
| Python         | 3.12+                                                       |
| Container      | Docker, single-container by default                         |
| Web framework  | FastAPI for HTTP, `websockets` (or FastAPI's) for WS        |
| Storage        | SQLite for v1. Postgres only if explicit Phase 3 ask.       |
| Dashboard      | FastAPI + HTMX. Plain Jinja templates. No SPA build step.   |
| Logging        | `loguru` (matches upstream `xiaozhi-server` log style)      |
| Config         | YAML at `data/.config.yaml`, env overrides via `AGENT_HUB_` |

### Code style specifics

- Type hints on every function signature
- Docstrings on every public function (Google style)
- No `from x import *`
- One class per file when the class is non-trivial (>50 lines)
- Async by default for any I/O — no sync HTTP / WS / DB calls in the
  server module
- Keep the check-in handler under 100 lines. If it grows past that,
  push logic into `registry/` not into the handler

## Common commands

All commands run from repo root via `just`. List with `just`.

| Command                    | What it does                                     |
| -------------------------- | ------------------------------------------------ |
| `just install`             | `uv sync --all-extras`                           |
| `just lint`                | `ruff check src/ tests/ && ruff format --check`  |
| `just format`              | `ruff format src/ tests/`                        |
| `just typecheck`           | `mypy --strict src/agent_hub/`                   |
| `just test`                | `pytest -xvs`                                    |
| `just test-watch`          | `pytest-watch`                                   |
| `just run`                 | `uv run python -m agent_hub.server`              |
| `just dashboard`           | `uv run python -m agent_hub.dashboard.app`       |
| `just docker-build`        | `docker compose build`                           |
| `just docker-up`           | `docker compose up`                              |
| `just deploy-edge`         | `ansible-playbook deploy-agent-hub.yml`          |
| `just deploy-fubar`        | Class-night laptop override                      |

## Hard rules — what agents must NOT do

1. **Do not fork or vendor `xinnan-tech/xiaozhi-esp32-server`.** This is
   a clean reimplementation. Reading the upstream code to understand
   protocol shape is fine; copying it is not.
2. **Do not introduce MySQL, Redis, or Java/SpringBoot dependencies.**
   The whole point of this project is avoiding the full-module stack.
3. **Do not break the `/xiaozhi/ota/` URL alias.** Already-flashed devices
   in the field depend on it. New code must continue to serve that path.
4. **Do not add an activation gate to check-in.** First-contact devices
   must work immediately with the `hub-default` persona.
5. **Do not edit `data/.config.yaml`** — it holds API keys. Edit
   `.config.example.yaml` instead and document the change in `README.md`.
6. **Do not introduce a frontend build step (webpack/vite) for v1.**
   HTMX-style server-rendered HTML keeps the deploy a single container.
7. **Do not push to remote without local `just lint typecheck test` clean.**

## Hard rules — what agents must do

1. **Read the upstream protocol shape before implementing
   `server/protocol.py`.** Specifically:
   - Check-in request/response: device sends MAC + version, server returns
     WebSocket URL + per-device config
   - WS message types: `hello`, audio frames (Opus-encoded), tool calls,
     TTS responses
   - MCP-over-WS: device-side MCP server is reachable inside the voice
     session via JSON-RPC framing
2. **Preserve backward compatibility** of the check-in response JSON.
   Adding fields is fine; removing/renaming is not.
3. **Add a regression test for every protocol change** in
   `tests/server/test_protocol.py`. Use recorded fixtures from a real
   device check-in where possible.

## Skill catalogue

The `skills/` directory holds focused agent instructions for areas where
having extra context materially improves output quality. Each skill is a
`skills/<name>/SKILL.md` file that gets loaded contextually when the work
is in that area.

The skills below are **planned**, not yet written. Create them as the
corresponding code areas are touched, following the
[Anthropic skill format](https://www.anthropic.com/news/agent-skills) used
elsewhere in Rick's projects.

| Skill                       | Triggers when…                                    | Covers                                                                 |
| --------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------- |
| `xiaozhi-protocol`          | working in `src/agent_hub/server/protocol.py` or any file that reads/writes the device wire protocol | Message shapes for check-in, WS hello, audio frames, MCP-over-WS, tool calls; firmware compatibility constraints; known gotchas (Opus framing, sample rates, endianness) |
| `registry-model`            | working in `src/agent_hub/registry/`              | The Agent / Device / Persona / Template data model; lifecycle states (DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE); memory scoping rules; SQLite schema migrations |
| `providers`                 | adding or modifying an LLM/TTS/ASR provider       | Abstract base classes; auth conventions (env var naming); streaming vs blocking patterns; cost tracking; how to add a new provider in <100 lines |
| `mcp-bridge`                | working in `src/agent_hub/server/mcp_bridge.py`   | MCP tool discovery on device connect; cross-agent tool routing rules; auth model (which agents can call which device's tools); error propagation |
| `dashboard-htmx`            | working in `src/agent_hub/dashboard/`             | HTMX patterns used in this repo; component conventions; how to add a new page without a build step; auth model |
| `deployment-edge`           | working in deployment playbooks or compose files  | Bind-mount layout (survives Docker wipe); Tailscale sidecar pattern; Cloudflare Tunnel pattern; secrets handling; NFS conventions from the homelab |
| `class-day`                 | preparing for a teaching session at FUBAR or 4-H  | Pre-class checklist; common builder mistakes; smoke test sequence; what "it worked" looks like; recovery playbook for flaky wifi |

### Skill creation rules

When asked to create one of these skills:

1. Use Anthropic's SKILL.md format with `name`, `description`, and trigger
   guidance in the YAML frontmatter.
2. The `description` field decides whether the skill loads — make it
   discriminating. "Use when working with the xiaozhi protocol" is bad.
   "Use when writing or modifying code that reads or writes the xiaozhi
   wire protocol — check-in JSON, WebSocket hello message, audio frame
   format, or MCP-over-WS bridge — including any change to
   `server/protocol.py`" is good.
3. Skills are reference material, not implementation. They describe
   conventions and constraints; they do not contain the production code.
4. Each skill stands alone. Do not assume the reader has loaded a sibling.
5. Update the table above when adding a skill.

## Provider keys and secrets

Live in `data/.config.yaml` (gitignored). Never commit. The template at
`.config.example.yaml` shows the structure with all values redacted.

Env var overrides follow the pattern `AGENT_HUB_<SECTION>_<KEY>`, e.g.
`AGENT_HUB_LLM_OPENAI_API_KEY` overrides
`llm.openai.api_key` in the yaml.

## When in doubt

- Ask before introducing a new dependency
- Ask before changing the data model
- Ask before touching `server/protocol.py`
- Default to fewer abstractions, not more
- A working 200-line module beats a clean 1000-line one for v1

## Reference material

- Upstream server (read for protocol understanding only):
  https://github.com/xinnan-tech/xiaozhi-esp32-server
- Upstream firmware (same):
  https://github.com/78/xiaozhi-esp32
- Rick's homelab ansible patterns (private):
  github.com/ricklon/ansible-homelab
- Coachable-robots project (uses the same toolchain conventions):
  https://github.com/ricklon/coachable-robots
