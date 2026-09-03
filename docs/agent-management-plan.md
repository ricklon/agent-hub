# Design note: managing agents as one fleet

Status: **proposal**, with the two blocking fixes landed in the same PR
(page agent identity/lifecycle, free mode). Everything under "Plan" is
sequenced so each step is a small PR that leaves the hub working.

## Why now

Three kinds of agent exist on the hub today and each is managed differently:

| Kind | Registers via | Live state lives in | Activity shown on dashboard | Can be spawned without hardware |
| --- | --- | --- | --- | --- |
| xiaozhi device | `/xiaozhi/ota/` check-in + heartbeat | `session_state` (voice socket, pipeline phase, latency) | health · listening/thinking/speaking · MCP tool count | no |
| page agent | `/page-agent/register` + heartbeat | `mcp_bridge._page_agents` (SSE handle, tools) | until this PR: always "idle", "wake-word standby" | only by opening a browser tab |
| transcriber | a device bound to a persona with `transcription=true` | `session_state` transcription sessions | same as device | no |

An "agent" is really two things that the code and the UI blur together: a
**persona** (config: model, voice, prompt, skills, allowlists) and an
**instance** (a registry row with a body: a board, a browser tab, a process).
The dashboard is organised around personas and around xiaozhi devices; the
other bodies are second-class, which is why "launch as page agent" opens a
page you cannot see from the fleet view and why two page agents could not
run at once.

## What tau and pi do that we do not

[huggingface/tau](https://github.com/huggingface/tau) is a Python port of
[pi](https://github.com/badlogic/pi-mono) (`pi-agent-core`), a minimalist
coding-agent harness. Neither is a voice or device platform, so most of their
surface (TUI, file/shell tools, coding sessions) is irrelevant here. Their
**core** is what matters:

| Idea | tau / pi | agent-hub today |
| --- | --- | --- |
| Layering | `tau_ai` (provider → neutral event stream) · `tau_agent` (messages, tools, events, loop, harness, sessions) · `tau_coding` (the app). "The core does not know about the TUI." | The loop lives inside `openai_provider.complete_with_tools(history, tools, exec, system_prompt, max_rounds=5)`. Tool assembly and the executor are copied three times: `ws_session`, `page_agent.ask`, `page_agent.voice`. |
| Events are the contract | `agent_start/end`, `turn_start/end`, `message_update` (deltas), `tool_execution_start/update/end`. Every frontend, logger and test consumes the same stream. | No event stream. The dashboard reads `session_state` phase flags; transcripts are written ad hoc; the test harness watches the bridge and cannot see server skills run (open question in `agent-test-harness.md`). |
| Tools | "ordinary typed functions": schema + async executor returning structured content; per-tool `executionMode`; `terminate` hint. | Four tool sources (server skills, device MCP, page MCP, linked agents) each with its own lookup path inside each `_exec_tool` closure; policy (`tool_policy`) applied in some paths, not all. |
| Sessions | Append-only JSONL tree per session, resumable, branchable, compacted without rewriting. | Flat `conversation_history` rows (device_id, role, content). Tool calls and results are not recorded; images are marked with a `[image:captured]` string. |
| Steering | pi: `prompt()` / `steer()` / `followUp()` queues so a running agent can be interrupted or given follow-up work. | Dashboard "send a message to a device" injects text through `session_state` injectors; no queue semantics, no barge-in model. |
| Config as a file | tiny-agents `agent.json` (model, provider, servers, prompt); pi/tau read `AGENTS.md` and `.tau/` resources. | Personas live only in SQLite, edited through forms. Nothing to copy between hubs or hand to a class. |

What **not** to borrow: the TUI, the coding toolset, multi-provider streaming
abstraction (the hub is OpenAI-compatible by design; see
`llm_provider_openai_compatible`), and automatic context compaction
(`memory_window` is the right tool for two-sentence voice turns).

## Plan

### 1. One agent harness (`agent_hub/harness.py`)

Extract the loop out of the provider and the three call sites into one
object:

```python
run = AgentRun(persona, tools=ToolSet.for_agent(device_id, persona), history=history)
async for ev in run.turn(user_text):        # or run.turn_from_audio(pcm)
    ...  # TurnStart, LlmDelta, ToolCall, ToolResult, Reply, TtsStart, TtsEnd, TurnEnd, Error
```

`ToolSet` is the single place that gathers server skills + device MCP + page
MCP + linked-agent tools and applies `tool_policy` once. `ws_session`,
`page_agent.ask` and `page_agent.voice` become adapters that feed input in
and map events to their transport (Opus frames, JSON, SSE). The event stream
feeds:

- `session_state` activity (`thinking`/`speaking`) — automatically, for every kind
- the transcript log and a new per-agent session JSONL (step 3)
- the test harness (`Turn.tool_calls` finally includes server skills)
- LLM spend attribution (page-agent calls are currently recorded with
  `device_id=NULL`, so per-agent spend cannot be shown for them)

Sizing: ~300 lines new, ~200 lines removed from the three call sites. No
protocol change, no schema change.

### 2. One fleet view (`dashboard/fleet.py`)

Replace the xiaozhi-shaped agent table with a kind-agnostic one built from a
single presence record:

```python
@dataclass
class AgentPresence:
    device_id: str; kind: str; label: str; persona: str | None
    health: DeviceHealth; activity: DeviceActivity
    transport: str          # "voice connected", "page open · bridge connected", "process running"
    tools: list[str]; last_turn: TurnLatency | None; last_seen: datetime | None
```

computed in one function from the registry row + `session_state` +
`mcp_bridge`. Both the HTML table and `/dashboard/api/status` use it. Rows get
kind-appropriate actions: **inspect**, **launch** (page: open a tab; runner:
start a process), **stop**, **remove**. Page-agent rows that have been
offline for more than a day are pruned automatically — the local dev registry
already holds eight dead `page-…` rows and the demo droplet will collect one
per visitor.

`transcriber` becomes visible as a kind badge derived from the persona flag so
the fleet view can filter "devices / pages / transcribers / runners" without
a schema change.

### 3. Durable sessions

Write every turn's events to `data/sessions/<device_id>/<session_id>.jsonl`
(append-only, one JSON object per event). Keep `conversation_history` as the
LLM context window; the JSONL is the inspectable record — tool calls,
results, images by path, latency, model, cost. The agent detail page reads it
instead of reconstructing from `conversation_history`. This is also the
export format for the transcriber (today's transcript download becomes one
case of it).

### 4. Headless runners: spawn page-style agents without a browser

The test harness already proves a page agent can be driven in-process
(`tests/harness/page_agent_client.py`). Promote that into a runtime
**runner**: a hub-hosted task that registers over the same bridge with a
tool list, heartbeats, and answers `tools/call`. Then browser pages, robots
over the bridge, and local MCP servers (tiny-agents-style `servers:` list —
stdio/sse/http) are all "agents that register with tools", and the fleet
page can **Launch** a persona as a runner with one click and talk to it from
a dashboard chat box. This is what makes "spawn them and treat them like our
robot MCP agents" true.

### 5. The user workflow for creating an agent

Three steps, in this order, each a page that exists or is one PR away:

1. **Persona** — the guided builder (exists). Add **export/import as YAML**
   so a class can share a persona file (the `agent.json` idea).
2. **Body** — pick what runs it: a device from the fleet (assign persona; exists),
   a browser page (launch; exists, now multi-tab safe), a headless runner
   (step 4), or transcriber mode (exists).
3. **Talk and watch** — the fleet row shows live activity, the session log
   shows every tool call, and the dashboard chat box sends text to any agent
   regardless of body.

The Personas page should say this explicitly; today "launch" is a small link
on the edit page and the Models page silently edits only `hub-default`.

## Landed in this PR

- **Page agent was dead over plain `http://<LAN-IP>`** — the class-night
  laptop setup. `crypto.randomUUID` only exists in secure contexts, so the
  script threw on line one and the page sat on "initialising…" forever.
  Fixed with a `getRandomValues` fallback; the page now also says up front
  that camera and microphone need https or localhost (text chat works).
- **Two page agents could not coexist.** Identity was per browser
  (`localStorage`), so a second tab re-registered the same id, re-issued the
  token and silently broke the first. Identity is now per tab; the label
  carries the persona (`page · hero-robot`).
- **Page agents looked idle forever and never went offline.** Heartbeats
  now carry real activity (listening/thinking/speaking) and are pushed on
  change; `/page-agent/ask` and the voice socket set pipeline status and
  record turn latency like a device; a `pagehide` beacon to
  `/page-agent/goodbye` drops the bridge handle and marks the row offline
  immediately. The fleet row shows "page open · bridge connected · N page
  tools" or "page closed" instead of "wake-word standby".
- **Free mode.** `llm.free_only: true` makes the Models page list only free
  OpenRouter models and refuses paid ids on select and on persona save; a
  "Free only" checkbox does the same on demand. The OpenRouter catalogue is
  now cached for ten minutes (it was fetched on every keystroke).

## Landed in the follow-up PR

- **Stale-agent cleanup.** `dashboard/cleanup.py` holds one policy: devices
  stale after 14 days unseen, page agents after 24 hours (both configurable
  under `registry:`). The dashboard home lists what is stale and sweeps it on
  one click; the server prunes **page agents only** hourly, so a board is
  never removed without a person. An agent with a live voice socket or an
  open bridge stream is never stale regardless of its row.
- **Long-term agents.** `Agent.pinned` marks the boards and pages that are
  part of the furniture. Pinned agents are never counted as stale and never
  pruned; the fleet table shows a `kept` badge and the agent page has the
  toggle.
- **Tool-capable models only.** The catalogue now reads
  `supported_parameters`; models that cannot call tools are hidden from the
  picker (with a count of what was hidden) and refused on select and on
  persona save. Every persona here depends on function calling, so offering
  a model without it was offering a trap. Ids the catalogue does not know
  (a local Ollama model) still pass.
- **Spend per agent, whatever the kind.** `spend.bind_device()` sets the
  agent for the current task, so every provider call inside a voice session
  or a page turn is attributed without threading an id through the provider
  API. The fleet table gained a spend column and the agent page a spend line.
- **Voices follow the voice system.** The persona editor swaps its voice list
  when the TTS system changes, and a save that pairs a Kitten persona with an
  Edge voice is refused instead of failing at synthesis time.
- **The page agent speaks with the persona voice.** `POST /page-agent/tts`
  synthesizes with the persona's TTS system and voice and returns WAV; the
  page picks persona voice, browser built-in, or silent, and falls back to
  the browser voice (saying so) when hub TTS is unreachable.
- **Persona editor honesty.** Transcription mode dims the sections it
  ignores, and the model box is a searchable datalist of usable ids rather
  than an invitation to copy from another page.

## Landed for robot build night

Step 4 of the plan (headless agents) and the first half of step 1 (one
harness), driven by needing a room of people to manage their own robots.

- **A third registration door.** `server/agent_api.py` serves `/agent/register`,
  `/agent/heartbeat` and `/agent/goodbye` on the **device** port, with the MCP
  bridge mounted there too. Page agents register through the dashboard port
  behind Cloudflare Access, which a headless robot cannot authenticate to;
  robots use the port Caddy already exposes, gated by the enrollment token.
  A robot is `AgentKind.MCP` and is otherwise an ordinary bridged agent.
- **`examples/robot_agent.py`** — the whole client in one file: declare tools,
  run it, done. This is what makes build night self-service.
- **One shared turn.** `server/agent_turn.py` holds the loop `/page-agent/ask`
  used to own inline: tool assembly, the LLM call, bridge routing, history.
  The page agent, the robot console and the dashboard now run the same code.
  `ws_session` still has its own copy — that is the rest of step 1.
- **A tool console.** The agent page lists every tool a bridged agent declared,
  each with a JSON argument box and a Call button, plus an "Ask this agent" box
  that runs a full persona turn. Testing a robot no longer needs a script.
- **Ownership.** `Agent.owner` plus filter chips on the fleet table. A label
  for organising a busy room, explicitly not a permission boundary.
- **Kind-aware agent pages.** Reboot, Inject and Speak are firmware actions and
  are hidden for bridged agents; the connection table reports the bridge and
  its tools instead of "wake-word standby" and "MCP —".
- **Malformed tool arguments are recoverable.** A model emitting almost-JSON
  used to kill the whole turn with a `JSONDecodeError`. The provider now hands
  the parse error back as the tool result so the model can correct itself.

## Things to know before relying on free mode

- OpenRouter's free models are rate-limited per key per day and can be
  pulled at any time; a demo on free mode needs a fallback model in mind.
- Many free models do **not** support tool calling, and the hub depends on
  it (time, weather, camera, device tools). Handled: the picker now hides
  them everywhere and refuses them on save, so free mode lists only free
  models that can actually run a persona.
- Free mode does not touch models already saved on personas; it only gates
  new selections.
