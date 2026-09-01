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

Working alpha. Roughly 6k lines across `src/agent_hub/`, ~93 tests, all
quality gates green and enforced by CI on every push and PR.

Implemented and exercised against real hardware: check-in, the WebSocket
voice session (streaming ASR → LLM → TTS), device-side MCP tool discovery
and routing, server-side skills, the persona/registry model, camera capture
with background vision inference, and the dashboard.

When asked to implement something, **read the existing code first**. This
file describes intent and constraints; it is not a substitute for the code,
and where the two disagree the code wins.

## Architecture in two sentences

The check-in endpoint (`/checkin/`, aliased to `/xiaozhi/ota/` for firmware
compatibility) is the device's first contact and creates a registry entry
on first sight, auto-binding to a `hub-default` persona so the device is
functional immediately. The WebSocket session endpoint (`/xiaozhi/v1/`)
streams ASR/LLM/TTS for live conversations using the persona configured
on the registry row for that device.

## Repo layout (actual)

```
agent-hub/
├── README.md
├── AGENTS.md                ← you are here
├── pyproject.toml           ← uv, hatchling, ruff, pytest
├── justfile                 ← all dev commands
├── .github/workflows/ci.yml ← runs just lint / typecheck / test
├── .github/workflows/nightly-live-scenarios.yml ← daily: tests/scenarios/ llm:live cases
├── docker-compose.yml
├── docker-compose.fubar.yml ← class-night override (laptop on FUBAR wifi)
├── .config.example.yaml
├── src/agent_hub/
│   ├── config.py            ← loads .config.yaml + env overrides
│   ├── server/
│   │   ├── checkin.py       ← `/checkin/` and `/xiaozhi/ota/` alias
│   │   ├── ws_session.py    ← `/xiaozhi/v1/` voice loop (large; see note)
│   │   ├── protocol.py      ← message types and JSON schemas
│   │   ├── audio.py         ← Opus encode/decode, VAD, rate control
│   │   ├── session_state.py ← per-connection state
│   │   ├── mcp_bridge.py    ← page-agent MCP bridge (SSE-down / POST-up JSON-RPC)
│   │   ├── page_agent.py    ← page-agent register/heartbeat + the page route
│   │   ├── _page_html.py    ← the page-agent browser page (E501-suppressed)
│   │   ├── mcp_client.py    ← device-side MCP-over-WS JSON-RPC client
│   │   ├── tool_policy.py   ← which tools a persona may call
│   │   ├── image_explain.py ← camera upload + background vision inference
│   │   ├── transcript_log.py
│   │   └── emotion.py
│   ├── providers/
│   │   ├── llm/             ← openai_provider (OpenAI-compatible, incl. OpenRouter)
│   │   ├── tts/             ← edge, kitten
│   │   └── asr/             ← funasr, funasr_onnx, openai_whisper
│   ├── registry/
│   │   ├── models.py        ← Agent, Device, Persona, AgentKind enum
│   │   └── store.py         ← SQLite-backed persistence
│   ├── skills/              ← server-side LLM tools (NOT the `skills/` below)
│   │   ├── get_weather.py
│   │   ├── get_current_time.py
│   │   └── web_search.py
│   └── dashboard/
│       └── app.py           ← FastAPI + HTMX (large; see note)
├── scripts/                 ← smoke.py, test_features.py, model downloads
├── tests/
├── skills/                  ← focused coding-agent instruction files
└── docs/
```

Two naming traps:

- `skills/` (repo root) holds **instructions for coding agents** and is
  currently empty. `src/agent_hub/skills/` holds **runtime tools the LLM
  can call** and is real code. They are unrelated.
- `mcp_bridge.py` and `mcp_client.py` are different layers. The client
  speaks JSON-RPC to one xiaozhi device over the voice WebSocket; the bridge
  speaks JSON-RPC to a browser page agent over SSE-down / POST-up.

`ws_session.py` and `dashboard/app.py` are each ~1000 lines and together are
about a third of the codebase. Prefer extracting into a sibling module over
growing either further.

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
| `just download-models`     | Fetch Silero VAD + SenseVoiceSmall into `models/`|
| `just reset-data`          | Wipe transcripts/images between public sessions; keeps the registry |
| `just lint`                | `ruff check src/ tests/ && ruff format --check`  |
| `just format`              | `ruff format src/ tests/`                        |
| `just typecheck`           | `mypy --strict src/agent_hub/`                   |
| `just test`                | `pytest -xvs`                                    |
| `just test-watch`          | `pytest-watch`                                   |
| `just smoke`               | `scripts/smoke.py` — quick server sanity check   |
| `just bench-asr`           | Moonshine vs SenseVoice WER on LibriSpeech (needs net) |
| `just compare-asr [dir]`   | Replay `debug_audio_dir` captures through ASR providers |
| `just test-features`       | Drive every feature against a live device        |
| `just run`                 | `uv run python -m agent_hub.server`              |
| `just docker-build`        | `docker compose build`                           |
| `just docker-up`           | `docker compose up`                              |
| `just deploy-fubar`        | Class-night laptop override                      |

The test suite is hermetic — ASR providers are monkeypatched, so `just test`
needs no models and no network. `just download-models` is only required to
actually run the server.

Two recipes are currently broken; fix them before relying on them:

- `just dashboard` silently does nothing. `dashboard/app.py` has no
  `if __name__ == "__main__"` guard, so `python -m agent_hub.dashboard.app`
  imports the module and exits. The dashboard is mounted by `just run`.
- `just deploy-edge` calls `ansible-playbook deploy-agent-hub.yml`, and no
  such playbook exists in this repo.

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
   CI runs the same three recipes on every push and PR, so a dirty push
   fails in public rather than quietly.
8. **Do not commit directly to `main`.** Every change — code *and* docs —
   lands through a branch and a PR. Squash-merge, so the commit subject on
   `main` reads `<subject> (#N)`.
9. **Do not `git add -A`.** Stage explicit paths; this repo sits next to
   gitignored config and model files.

## Hard rules — what agents must do

1. **Cross-check the upstream protocol before touching
   `server/protocol.py`.** It is implemented and field-proven; verify
   against upstream rather than redesigning. The shapes that matter:
   - Check-in request/response: device sends MAC + version, server returns
     WebSocket URL + per-device config
   - WS message types: `hello`, audio frames (Opus-encoded), tool calls,
     TTS responses
   - MCP-over-WS: device-side MCP server is reachable inside the voice
     session via JSON-RPC framing

   Known gaps against the current upstream spec, all deliberate: the
   `Protocol-Version` request header is ignored, binary protocol v2/v3
   (timestamped frames for server-side AEC) is unimplemented, and the
   `aec` / `glyph_push` feature flags are unhandled. Firmware defaults to
   binary v1, so none of these break real devices today.
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

Skills with an existing `skills/<name>/SKILL.md` are available; the others
below are planned. Create planned skills as the corresponding code areas are touched, following the
[Anthropic skill format](https://www.anthropic.com/news/agent-skills) used
elsewhere in Rick's projects.

| Skill                       | Triggers when…                                    | Covers                                                                 |
| --------------------------- | ------------------------------------------------- | ---------------------------------------------------------------------- |
| `xiaozhi-protocol`          | working in `src/agent_hub/server/protocol.py` or any file that reads/writes the device wire protocol | Message shapes for check-in, WS hello, audio frames, MCP-over-WS, tool calls; firmware compatibility constraints; known gotchas (Opus framing, sample rates, endianness) |
| `registry-model`            | working in `src/agent_hub/registry/`              | The Agent / Device / Persona / Template data model; lifecycle states (DISCOVERED → CLAIMED → ACTIVE → IDLE → OFFLINE); memory scoping rules; SQLite schema migrations |
| `providers`                 | adding or modifying an LLM/TTS/ASR provider       | Abstract base classes; auth conventions (env var naming); streaming vs blocking patterns; cost tracking; how to add a new provider in <100 lines |
| `mcp-bridge`                | working in `src/agent_hub/server/mcp_bridge.py`, `server/page_agent.py`, `_page_html.py`, or the `page_speak`/`page_see` skills | SSE-down / POST-up JSON-RPC bridge for browser page agents; page-agent registration (AgentKind.PAGE, no activation gate); how `call_page_tool` resolves via `/mcp/v1/respond`; port placement (mcp_bridge_port defaults to dashboard port); do not poll `request.is_disconnected()` |
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
  — protocol docs live in `docs/websocket.md` and `docs/mcp-protocol.md`

Everything in `docs/lessons-learned.md` was learned against **firmware
2.2.6**. Upstream has since shipped **2.4.0**, which migrated to ESP-IDF
6.0. Treat hardware-behaviour lessons as unverified on 2.4.0 until
re-tested on a flashed device.
- Rick's homelab ansible patterns (private):
  github.com/ricklon/ansible-homelab
- Coachable-robots project (uses the same toolchain conventions):
  https://github.com/ricklon/coachable-robots
