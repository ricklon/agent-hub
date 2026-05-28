# Lessons learned

Running notes on non-obvious problems, root causes, and fixes discovered
during development. Updated as new issues are found.

---

## Server

### Server only bound to one port — devices couldn't check in

**What happened:** The server started cleanly and the dashboard worked, but
ESP32 devices never appeared. They were sending check-in POSTs to `:8003`
but getting connection refused.

**Root cause:** `__main__.py` called `uvicorn.run(..., port=settings.server.ws_port)`
— a single binding to port 8000. The startup log printed "check-in on :8003"
but that was informational only; no listener existed on 8003.

**Fix:** Run three `uvicorn.Server` instances concurrently via
`asyncio.gather()`, one per port (8000, 8001, 8003).

**Watch for:** Startup log saying "check-in on :8003" doesn't mean port 8003
is open. Always verify with `ss -tlnp | grep python`.

---

### All routes are on one port in local dev

**What happened:** README and startup log said dashboard was on `:8001` but
hitting that URL got connection refused. Dashboard was actually on `:8000`.

**Root cause:** All routes (WS, check-in, dashboard) share a single FastAPI
app. In the old single-port setup, everything was on the WS port (8000).
After the multi-port fix, all three ports serve all routes.

**Fix:** Dashboard is at `http://SERVER:8000/dashboard/` (or 8001 — both
work after the multi-port fix). Updated README accordingly.

---

### Startup event fires once per uvicorn server

**What happened:** Registry initialized three times on startup (one per port).

**Root cause:** Three `uvicorn.Server` instances each trigger the FastAPI
startup event independently.

**Impact:** Cosmetic — SQLAlchemy `CREATE TABLE IF NOT EXISTS` is idempotent.
No data corruption. Worth knowing if startup has side effects.

---

### `on_event("startup")` is deprecated

FastAPI deprecated `@app.on_event("startup")` in favor of lifespan context
managers. Current code still uses the old form and logs a deprecation warning
on startup. Migration to `@asynccontextmanager` lifespan is a future cleanup.

---

## Devices

### One-digit IP typo wasted significant debugging time

**What happened:** Device was configured with `192.168.1.139` instead of
`192.168.1.39`. Serial log showed "Failed to get host by name" (ESP-IDF's
generic TCP error) rather than "connection refused", which obscured the issue.

**Lesson:** Always run `!status` on a device first to confirm the OTA URL
before diagnosing anything else. The Waveshare board can also long-press the
boot button to show IP and OTA URL on screen.

---

### Serial monitor causes DTR reset on ESP32-S3

**What happened:** Device appeared to crash and reboot every ~15 seconds
while being monitored. Server logs showed repeated check-ins.

**Root cause:** ESP32-S3 resets when the USB serial port's DTR line toggles.
Python's `serial.Serial()` toggles DTR on open/close. Every time our serial
read script exited, it reset the board.

**Fix:** Close serial monitors before expecting stable device behavior. Use
`serial.Serial(..., dsrdtr=False)` or keep the port open continuously if
monitoring.

---

### Firmware version reported as `0.0.0`

**What happened:** Server logs show `Check-in from 'xx:xx:...' (fw 0.0.0)`
even for boards running firmware 2.2.6.

**Root cause:** The firmware version field in the check-in request is parsed
from HTTP headers. The Waveshare board (and some others) don't send the
expected header format.

**Status:** Cosmetic — doesn't affect functionality. Tracked for future fix.

---

### Board chip name ≠ capabilities

**What happened:** A "new ESP32-C3 board" was assumed to be an ESP32-C3 from
description. Serial output revealed it was actually a Waveshare ESP32-S3
Touch AMOLED. Caused confusion about wake-word support.

**Lesson:** Always check serial boot log for `Board: UUID=... SKU=...` to
confirm the actual board identity before debugging capability issues.
`!status` also shows this.

---

## MCP (device tools)

### MCP handshake must complete before persona assignment

**What happened:** Server was assigning a persona before MCP tools were
discovered, so the persona couldn't be matched to device capabilities.

**Fix:** Moved MCP `initialize` + `tools/list` handshake to before persona
lookup. The server now waits up to 5 seconds for the handshake, then falls
back to the stored persona.

---

### Device disconnect during MCP handshake caused crash loop

**What happened:** When a device disconnected during the 5-second MCP
handshake wait, the next `websocket.receive()` raised
`RuntimeError: Cannot call "receive" once a disconnect message has been received`.
This filled the log and caused every reconnect to error.

**Fix:** Catch `WebSocketDisconnect` and `RuntimeError` in the drain loop,
and also check for `msg.get("type") == "websocket.disconnect"`. Return
immediately on any of these.

---

### Hardcoded tool list in system prompt caused LLM hallucination

**What happened:** The `hub-default` persona's system prompt listed
`self_camera_take_photo` explicitly. Boards without a camera (Waveshare S3)
would call it anyway, getting `"unknown tool"` from the executor and
responding with "I'm having trouble with the camera."

**Root cause:** The system prompt was written before dynamic tool discovery
existed. It assumed every device had a camera.

**Fix:** Removed the hardcoded tool list from `hub-default`. `ws_session.py`
now builds the tools section dynamically from whatever MCP tools were
actually discovered during the handshake. No camera discovered = no camera
in prompt.

**Lesson:** Never hardcode device capabilities in a persona's system prompt.
The persona should only set personality and constraints; tools come from
discovery.

---

### Camera tool returns "unknown tool" when MCP handshake timed out

**What happened:** LLM tried to call `self_camera_take_photo` but the server
returned "unknown tool", even on a board that has a camera.

**Root cause:** The MCP handshake had timed out (device was slow to respond),
so no tools were registered. The system prompt (at the time, still hardcoded)
told the LLM the camera existed, but the tool executor had no record of it.

**Fix:** Both issues addressed — hardcoded prompt removed (see above), and
handshake disconnect handling improved.

---

### Camera causes firmware OOM crash on XIAO ESP32-S3 Sense

**What happened:** Asking the XIAO S3 Sense to take a photo causes it to
crash with a reset ~10 seconds after the tool call. The server-side code is
correct; the crash is firmware-side.

**Root cause:** JPEG capture on the XIAO S3 Sense requires large PSRAM
allocations. The firmware OOM-crashes during capture, especially at default
frame sizes.

**Status:** Firmware-side fix needed. Possible mitigations: reduce camera
frame size in firmware config, or increase PSRAM reservation. Tracked in
firmware project.

---

### Firmware 2.2.6 emits stray `)` in MCP initialize response — breaks all tool discovery

**What happened:** Every device connected and said it supported MCP (`supports_mcp=True`),
but `tools/list` was never sent and no tools were discovered. Server logged
"MCP handshake timed out" every session.

**Root cause:** The firmware's JSON serializer emits `)` instead of `}` to close the
`features` object inside the MCP initialize response capabilities block. `json.loads`
fails immediately at that character, so `handle_message` was never called, and the
`tools/list` request was never sent.

**Fix:** `_parse_ctrl()` in `ws_session.py` tries `json.loads` first; on failure it
strips stray `)` characters that appear immediately before `,` or `}` (structural JSON
positions) using `re.sub(r'\)([,}\]])', r'\1', text)`, then retries.

**Watch for:** `"MCP handshake timed out"` with no preceding `"MCP server:"` line means
the `initialize` response never parsed. Add `_parse_ctrl` debug logging to inspect the
raw text if a new firmware version breaks this again.

---

## Audio pipeline

### SenseVoice EMO_UNKNOWN was filtering out real speech

**What happened:** Short or quiet speech was being dropped because the ASR
filter required a non-UNKNOWN emotion tag.

**Root cause:** The filter was written assuming EMO_UNKNOWN meant background
noise. In practice SenseVoice returns EMO_UNKNOWN for real speech that's
emotionally neutral or ambiguous.

**Fix:** Allow EMO_UNKNOWN through the filter; only drop audio tagged as
non-speech event types.

---

### Pipeline lock caused frame drops under load

**What happened:** When VAD fired quickly (short utterances in succession),
the second pipeline run would be dropped because the first hadn't finished.
The `asyncio.Lock` approach serialized correctly but the log showed
excessive "pipeline busy — dropping N frames."

**Fix:** Changed from lock-based dispatch to `asyncio.Task` (`_fire_pipeline`).
A running task blocks new dispatches, but the logic is cleaner and the
task can be cancelled on disconnect.

---

## LLM provider

### OpenAI-compatible APIs can return empty `choices` or `None` tool calls

**What happened:** Some models (especially via OpenRouter) occasionally return
responses with `choices=[]` or tool call objects with `None` function fields.
This caused `IndexError` or `AttributeError` in the LLM provider.

**Fix:** Added guards: check `if not resp.choices: return ""` and
`if tc is None or tc.function is None: continue` in the tool call loop.

---

## Build / deployment

### C3 and C6 XIAO boards have different GPIO for same physical pads

| Pad | C3 GPIO | C6 GPIO |
|-----|---------|---------|
| D0  | GPIO2   | GPIO0   |
| D1  | GPIO3   | GPIO1   |
| D2  | GPIO4   | GPIO2   |
| D3  | GPIO5   | GPIO21  |

Flashing a C3 build onto a C6 breaks the microphone (wrong I2S DIN pin).
Always use `switch-board.sh` with the exact board target. Never assume
"close enough."

---

### `switch-board.sh` shared `sdkconfig` corrupted cross-board builds

**What happened:** Running `switch-board.sh` for a second board would corrupt
the first board's config because all builds shared one `build/` directory.

**Fix:** Each board now gets `build-<board>/` and its own `sdkconfig` passed
via `-DSDKCONFIG=$bdir/sdkconfig`. Switching boards is now safe and fast
(no full rebuild needed).

---

*Last updated: 2026-05-27*
