---
name: mcp-bridge
description: Use when writing or modifying code that routes Model Context Protocol tool calls to or from browser page agents—server/mcp_bridge.py, server/page_agent.py, the page_speak/page_see skills, or the SSE-down/POST-up JSON-RPC bridge—including any change to /mcp/v1/events or /mcp/v1/respond.
---

# Page-agent MCP bridge

A page agent is a browser page that acts as a talking + seeing MCP server. The hub drives it the same way it drives a xiaozhi device's MCP tools, so the existing `tool_policy` / `Persona.mcp_tools_allowlist` / `MCPClient` machinery can reach page tools without new abstractions.

## Required workflow

1. Read `server/mcp_bridge.py` and `server/page_agent.py` together with `server/mcp_client.py` before changing the bridge — the client and bridge are the two MCP layers and must keep the same JSON-RPC shape.
2. Run `just lint typecheck test`. The SSE generator test (`test_event_generator_emits_tools_call_and_ping`) is the only test that exercises the stream body directly; do not rewrite it to use httpx streaming (ASGITransport buffers streaming bodies and the test hangs).
3. Keep the page HTML in `server/_page_html.py` (E501-suppressed). Do not inline it back into `page_agent.py`.

## Transport

A browser page cannot accept inbound HTTP, so the standard streamable-HTTP MCP transport does not fit. The bridge uses a half-duplex adaptation that reuses the xiaozhi JSON-RPC shape (protocolVersion `2024-11-05`):

- `GET /mcp/v1/events?device_id=…&token=…` — SSE stream. The hub pushes JSON-RPC `tools/call` requests as `data:` events; `: ping` comments keep the connection alive.
- `POST /mcp/v1/respond` — the page posts a JSON-RPC `{jsonrpc, id, result|error}` back to resolve a pending call.

The `initialize` and `tools/list` handshake is folded into page-agent registration (`POST /page-agent/register`), which stores the page's tool list in the bridge and issues the token the SSE/respond endpoints authenticate against.

## Registration and lifecycle

- `POST /page-agent/register` creates an `AgentKind.PAGE` registry row auto-bound to `hub-default` (no activation gate — same rule as xiaozhi devices), issues a per-page websocket token via `store.issue_websocket_token`, and calls `mcp_bridge.register_page_agent(device_id, token, tools)`.
- `POST /page-agent/heartbeat` reuses `store.record_authenticated_heartbeat`, so page agents appear on the dashboard with the same health/activity/tools columns as devices.
- Live state (tools, outbound queue, pending calls) is in-process in `mcp_bridge._page_agents`; durable state is the registry row. A reconnect drops stale pending futures so a reconnecting page does not inherit a dangling call.

## Calling page tools

- `mcp_bridge.call_page_tool(device_id, name, arguments, timeout)` enqueues a `tools/call`, awaits the matching `/mcp/v1/respond`, and returns the text result or an image data URL (so a vision model can consume a `page.camera.take_photo` capture the same way it consumes a device `self.camera.take_photo` capture).
- `mcp_bridge.find_page_agent_for_tool(name)` resolves a tool name to a connected page agent — used by the `page_speak` / `page_see` skills so the LLM can drive the page without knowing which page agent id is live.
- The result shape mirrors the firmware: `{content:[{type:"text"|"image",…}], isError:false}`.

## Port placement

The bridge is mounted on `server.mcp_bridge_port`, which defaults to the dashboard port so the browser page connects same-origin (no CORS friction, no new trust boundary). Set `mcp_bridge_port: 8004` in config to isolate it as its own port. When collapsed with the dashboard port, `build_apps()` merges them — the collapsed-ports test asserts the bridge routes survive.

## What not to do

- Do not poll `request.is_disconnected()` in the SSE generator — it blocks on ASGI transports without a real socket and hangs the page. Let cancellation propagate via `BaseException` to the `finally` block instead.
- Do not add a frontend build step for the page. It is plain HTML + ES modules served from the dashboard port; the `_page_html.py` E501 suppression exists because of this.
- Do not put page-agent tools in `tool_policy`'s device-tool path without a persona allowlist story — page tools are server-side skills (`page_speak`, `page_see`) that route through the bridge, not device MCP tools the voice loop calls directly.
