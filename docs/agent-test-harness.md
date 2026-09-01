# Design note: a page-agent test harness for agents

Status: **built and running in CI** (PRs #59–#62). `tests/harness/` has the
protocol client (`PageAgentClient`, `Turn`, `ToolCall`), the `ScriptedLLM` fake
provider, `SkillSpy`, and the `discover_scenarios` / `run_scenario` YAML runner;
`tests/scenarios/test_scenarios.py` parametrizes one test per scenario. Every
PR runs the mock-LLM scenarios plus `tests/harness/test_page_agent_client.py`;
`.github/workflows/nightly-live-scenarios.yml` runs the `llm: live` scenarios
daily against the real model. Still proposed: Layer 2 browser fixtures and
Layer 3 voice.

## The problem

An "agent" on this hub is a persona (LLM + TTS + ASR + system prompt + skill
allowlist + `mcp_tools_allowlist` + `memory_window`) plus the skills and MCP
tools it can reach. Today the only way to exercise one end to end is
`scripts/test_features.py`, which drives a physical df-k10 over the real voice
WebSocket. That makes agent iteration slow and keeps behaviour regressions
(does the LLM call `get_current_time` or fabricate a time — closed issue #8;
"is it the model or the tool policy" — the `feedback_check_permissions_before_model`
note) out of CI.

The page agent already is a hardware-free client that speaks the hub's real
protocols. This note proposes building the test harness on top of it instead of
writing a separate fake client from scratch.

## What the page agent already gives us

The page is served at `/dashboard/page-agent`. On load it POSTs its tool list
to `/page-agent/register`, which creates an `AgentKind.PAGE` registry row
(auto-bound to `hub-default`, no activation gate) and returns a per-device
token plus three endpoints:

| Endpoint | Direction | What it exercises |
| --- | --- | --- |
| `POST /page-agent/register` | page → hub | check-in/registry path, persona binding, tool registration |
| `GET /mcp/v1/events?device_id=&token=` | hub → page (SSE) | JSON-RPC tool-call requests bound for the page |
| `POST /mcp/v1/respond` | page → hub | JSON-RPC tool-call responses |
| `POST /page-agent/ask` | page → hub | **text in → full LLM + tool loop → text out** |
| `WS /page-agent/voice` | page ↔ hub | 16 kHz PCM in → VAD + ASR + LLM + TTS |

`/page-agent/ask` is the important one. It mirrors the voice session's LLM
loop exactly — `mcp_bridge.list_page_tool_definitions()` + server skills (minus
`page_speak`/`page_see`), `llm.complete_with_tools(history, tools, _exec_tool,
system_prompt=...)`, page tools routed through the bridge, skills run in
process — but with a plain JSON request instead of an audio stream. It returns
`{"ok": true, "reply": "...", "images": [...]}`.

So the page agent is not one test entry point but three, at decreasing speed
and increasing fidelity:

1. **Register + bridge** — test the MCP bridge with no LLM at all.
2. **`/page-agent/ask`** — test the LLM + tool loop with no audio and no
   browser.
3. **`/page-agent/voice`** — test the (page) voice path with canned PCM.

## Proposed harness

### The protocol client — `tests/harness/page_agent_client.py`

Built. Runs in-process against an ASGI app the way
`tests/server/test_page_agent.py` does — no network, and auth is whatever the
test config sets (empty config is permissive).

```python
async with PageAgentClient.session(store) as page:      # builds the app
    page.add_tool("get_screen", "Return text on screen", handler)
    await page.register()                                # POST /page-agent/register
    turn = await page.ask("what's on screen?")           # POST /page-agent/ask
```

`ask()` fires the request as a task and concurrently pumps the bridge: it
consumes the SSE event stream via `mcp_bridge.events_generator`, dispatches each
`tools/call` to the matching `add_tool` / `on_tool` handler, and POSTs the
result to `/mcp/v1/respond` — because `/page-agent/ask` blocks inside the
handler on every page-tool call until its result comes back.

`Turn` carries `reply`, `images`, and `tool_calls` (ordered `ToolCall`s with
`name`, `arguments`, `duration_s`), plus `elapsed_s`. Helpers: `turn.called(name)`
and `turn.call_args(name)`.

Not built: `voice()` over `/page-agent/voice` (Layer 3) — it needs the Silero
VAD model plus an ASR/TTS provider, which the hermetic suite avoids, and there
is no WebSocket test-client pattern in the repo yet.

### Layer 1 — text scenarios (CI, every PR)

Drive `/page-agent/ask` with the protocol client. Two sub-modes:

- **Mock LLM provider** (built — `tests/harness/scripted_llm.py`).
  `ScriptedLLM(tool_calls=[(name, args), ...], reply=...)` calls the executor
  with a fixed sequence then returns fixed text; `install_scripted_llm(monkeypatch,
  llm)` patches `page_agent.get_provider`. Deterministic. Tests the plumbing:
  tool routing through the bridge, skill execution, history persistence,
  system-prompt assembly. No network, no cost.
- **Cheap real LLM.** A small real model. Tests behaviour: given the tool is
  available, does the model call it; does it answer from the result rather
  than from history. Non-deterministic, so assertions are "tool X was
  invoked with arg matching …" plus a regex/contains on the reply.

### Layer 2 — "the page is the fixture"

A page tool's result is whatever the client's `on_tool` handler returns, so the
scenario controls the ground truth. For `page_see`-style tests, the handler
returns a known DOM snapshot; the assertion is that the agent's answer reflects
it. A higher-fidelity variant drives the real `_page_html.py` in a headless
browser against a static fixture page — same idea, slower, for nightly runs.

### Layer 3 — voice (nightly)

`/page-agent/voice` with a canned WAV played in as PCM frames. Asserts on the
ASR transcript and the reply. Optionally Playwright-driven through the real
page for full-stack fidelity.

### Scenario file format — `tests/scenarios/*.yaml`

Built. A file is one scenario (a mapping) or a list of them.
`tests/scenarios/test_scenarios.py` parametrizes one async test over every
discovered scenario — a normal test, so the `store` and `monkeypatch` fixtures
work with no custom-collector machinery. Node id is the scenario `name`.

```yaml
name: reads-the-screen-fixture
llm: mock                    # mock (default) | live
system_prompt: "..."         # optional; overrides hub-default's prompt
page_tools:
  - name: get_screen
    description: Return the visible text on screen
    result: "ALERT: disk usage 92%"     # str or mapping — the canned result
skill_results:               # optional; stub these server skills with fixed text
  get_current_time: "It is 3:04 PM."
turns:
  - say: "what does the alert say?"
    respond:                 # mock only — what the fake LLM does this turn
      calls:
        - { name: get_screen, args: { region: main } }   # or a bare "name"
      reply: "Disk usage is at 92%."
    expect:
      called: [get_screen]              # page tools and/or server skills
      not_called: [reboot]
      args: { get_screen: { region: main } }
      reply_contains: "92%"
      reply_matches: "92\\s?%"          # re.search
      images: 0
```

`mock` uses `ScriptedLLM` re-installed per turn from `respond`. `live` drops the
fake, uses the real configured model (no `respond` block), and is skipped
unless `AGENT_HUB_TEST_LIVE_LLM=1` and `llm.openai.api_key` is set.
`expect.called` unifies page-tool calls (from `Turn.tool_calls`) and server-skill
calls (from `SkillSpy`). Unknown keys anywhere raise `ScenarioError` rather than
passing silently.

## What this does not cover

The page voice path is a **parallel** implementation, not the device's. It uses
`PcmSileroVAD` on raw 16 kHz PCM. The device path in `server/ws_session.py`
decodes Opus packets through `OpusDecoder` + `SileroVAD`, and paces TTS back
out through `OpusEncoder` + `AudioRateController`. The harness will not exercise:

- Opus framing, sample-rate handling, or the rate controller
- the welcome-frame `transport: "websocket"` check the firmware requires
- `sentence_start` reply display (closed issue #46)
- per-device heartbeat and WebSocket-token auth
- any firmware-specific frame handling

It also cannot stand in for **real far-field device audio**. Browser mic audio
is clean and close-mic; issue #43 (Moonshine mangling wake-word commands) is
about ESP32 capture in a room. Behaviour ("did it call the tool, did it answer
right") is testable here; ASR quality is not.

## Recommended split

| Layer | Tool | Covers |
| --- | --- | --- |
| Page-agent `/ask` harness | this note | behaviour, tool use, tool policy, skills, persona, MCP bridge, registry — in CI |
| Thin Opus fake-device client | not yet built | `ws_session.py`: Opus decode/encode, rate control, welcome frame, heartbeat auth |
| `debug_audio` captures + `just compare-asr` | shipped (#58) | ASR accuracy on real hardware (#43) |

## Open questions

- **An endpoint trace mode vs. patching.** `SkillSpy` patches
  `agent_hub.skills.run_result` so scenarios can see and stub server-skill
  calls (page-tool calls already land in `Turn.tool_calls`). A trace/dry-run
  field on `/page-agent/ask` — tied to the broader per-turn trace idea — would
  make the patch unnecessary and give real (non-test) runs the same
  visibility.
- **`system_prompt` is only meaningful under `live`.** The mock LLM ignores
  it, so a `mock` scenario with `system_prompt:` exercises the persona-update
  path but proves nothing about prompt influence. Prompt-behaviour scenarios
  have to be `live`.
- **Assertion vocabulary.** `expect.called` is set membership — it cannot say
  "called exactly once", "called before X", or "args match a pattern". Extend
  the schema when a scenario needs one of these.

Resolved while building: **scenario isolation.** Each `test_scenario[...]` gets
a fresh function-scoped `store` (tmp SQLite), so history is stateful within a
scenario's turns and does not leak between scenarios.

## What's built vs. proposed

Built (`tests/harness/`): `PageAgentClient`, `Turn`/`ToolCall`, `ScriptedLLM` +
`install_scripted_llm`, `SkillSpy` + `install_skill_spy`, and the
`discover_scenarios` / `run_scenario` YAML runner with the
`tests/scenarios/test_scenarios.py` collector. Coverage: `test_page_agent_client.py`
(client mechanics) and the example scenarios under `tests/scenarios/`
(time-skill call, page fixture, no-tool chit-chat, multi-turn history, and a
`live` example). `.github/workflows/nightly-live-scenarios.yml` runs the `live`
scenarios daily against the real model (needs the `AGENT_HUB_LLM_OPENAI_API_KEY`
repo secret; model/base URL default to the demo's OpenRouter setup and are
overridable via `LIVE_SCENARIO_MODEL` / `LIVE_SCENARIO_BASE_URL` repo
variables); it retries failures once and opens a tracking issue on a repeated
failure.

Still proposed: Layer 2 browser fixtures and Layer 3 voice (`voice()` + a
WebSocket test-client pattern).

Prior art the client builds on: `tests/server/test_page_agent.py`,
`test_page_agent_wake.py`, `test_mcp_bridge.py`, `test_streaming_turn.py`.
