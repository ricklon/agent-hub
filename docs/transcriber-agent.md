# Design note: a Transcriber agent

Status: **in progress**. PR 1 (this change) adds the persona flag, the
dispatch wiring, the seeded `transcriber` persona, and the persona-editor
control. PR 2 adds photo captioning for transcript uploads, the check-in
`mode` field, and a transcript download. The firmware side (a physical
button, continuous streaming, a recording indicator, periodic photos) is
tracked in the `xiaozhi-esp32` repo — see "Firmware contract" below.

## The goal

The device pattern today: a device checks in, gets auto-bound to
`hub-default`, and an operator can assign a different persona. Every persona
is a voice assistant — ASR in, LLM + tools, TTS out.

We want one more kind of assigned agent: a **Transcriber**. When it is the
device's agent, the device stops being an assistant and becomes a room
recorder. It streams mic audio continuously; the hub transcribes each
utterance (ASR only — no LLM, no spoken reply) and logs it, and photos the
device sends are captioned into the same transcript. The operator reads the
running transcript in the dashboard.

Use cases: capturing a workshop or meeting, a walkaround where someone
narrates and photographs, an interview station at the demo table.

## What already exists

Most of the server-side machinery is in place and only needs a switch:

| Piece | Where | State |
| --- | --- | --- |
| ASR-only turn — no LLM, no TTS; writes `append_history(id, "transcript", text)` | `ws_session.py` `_run_transcription_turn` | works, but only reachable when the device's `hello` sets `features.transcription` |
| Streaming Silero VAD over a continuous Opus stream — `vad.push()` returns true at end-of-utterance, `vad.take()` returns that segment | `ws_session.py` main audio loop | works, independent of `listen` start/stop bracketing |
| Overloaded-ASR guard — drops stale audio and reports `overloaded` when the model can't keep up | `ws_session.py` `_asr_realtime_factor` | works; matters for a busy room |
| Transcript photo upload — `POST /xiaozhi/v1/image/?purpose=transcript&device_id=` saves the JPEG and adds an `[image:…]` history row | `image_explain.py` | works, but vision is **explicitly skipped** for `purpose=transcript` |
| `transcript` / `image` history rows render in the agent-detail "Conversation history" panel | `dashboard/app.py` | works |

The gap is that transcription mode can only be turned on by the firmware
advertising `features.transcription`. There is no way to make it the
device's assigned behaviour from the hub, and photos are stored but never
described.

## Server-side design

### 1. Persona flag (PR 1)

Add `Persona.transcription: bool` — a nullable column, added in
`store._migrate()` with `ALTER TABLE personas ADD COLUMN transcription
BOOLEAN`, matching the `linked_agents` pattern from #67. `NULL`/`0` means a
normal assistant persona; `1` means transcription mode.

In `ws_session.py`, after the persona is resolved, compute:

```python
transcription = hello.transcription_only or bool(persona.transcription)
```

and branch on `transcription` everywhere the code currently checks
`hello.transcription_only`:

- pipeline dispatch — `_run_transcription_turn` vs `_run_voice_turn`
- the greeting (`_GREETING`) — skipped in transcription mode
- the reactive "🤔" thinking face — skipped
- the `listen` `stop` handling that emits `{"type":"transcription",
  "state":"stopped"}`

A device that already sets `features.transcription` keeps working
unchanged; the persona flag is an additional way in.

### 2. Seeded `transcriber` persona (PR 1)

`_ensure_default_persona()` seeds `hub-default`. Add a sibling
`_ensure_transcriber_persona()` (or fold into one seeding pass) that
creates a `transcriber` persona if absent:

- `transcription = True`
- `asr_provider = self._default_asr_provider`
- `llm_provider` / `tts_provider` — set to the same defaults as
  `hub-default` for schema simplicity, but they are never used
- `system_prompt = ""`, `server_skills = "[]"` (no skills), `linked_agents`
  empty

Then the existing "assign an agent" flow is all an operator needs: pick
`transcriber` from the device's persona dropdown.

### 3. Photo captioning (PR 2)

In `image_explain.py`, when `purpose == "transcript"` (or the device's
assigned persona is transcription mode), stop early-returning. Instead:

- keep the immediate `{"status": "accepted"}` ack — the device blocks on
  this POST, and issue #4 is about not making that worse
- run the existing async vision job (`_complete_image_job` /
  `_describe_image`) with a transcript-oriented prompt, e.g. *"Describe
  what this photo shows, factually and in one or two sentences, for a
  meeting record."*
- append the caption to history as an `image` row:
  `[image:{path}] {caption}`

### 4. Check-in `mode` field (PR 2)

`CheckinResponse` gains `mode: "assistant" | "transcription"`, derived from
the device's assigned persona at check-in time. The firmware uses it to
enable the transcription button UI (or auto-start) when `transcriber` is
assigned. Absent/`"assistant"` changes nothing — purely additive.

### 5. Dashboard (PR 1 + PR 2)

- **Persona editor** (the guided form from #65): a "Transcription mode"
  checkbox. When checked, grey out the TTS, system-prompt, skills, and
  linked-agents fields — they do nothing in this mode — and show a short
  note explaining what the persona does.
- **Persona list**: show a `transcription` badge.
- **Agent-detail page**: when the assigned persona is transcription mode,
  relabel "Conversation history" → "Transcript" and hide the "Speak" box
  (it runs the full LLM pipeline, which does not apply). (PR 2)
- **Transcript download**: a button that streams the device's `transcript`
  and `image` history rows as timestamped plain text. (PR 2)

### Interaction with existing features

- `just reset-data` clears `conversation_history`, so it also clears
  transcripts and their photo captions. That is the right default — a
  transcript is session data — but worth calling out in the runbook.
- `memory_window` is irrelevant in transcription mode (no LLM context to
  trim); leave the column, ignore the value.
- Spend metering is untouched — transcription mode never calls the chat
  LLM. Photo captioning in PR 2 *does* call the vision model and is
  metered like any other vision call.

## Firmware contract (`xiaozhi-esp32` repo)

The firmware work is a separate change delivered over OTA. Summary of what
the hub expects; the full brief lives with that project.

- **Primary button** toggles a transcription session. While recording: an
  unmistakable local indicator (LED + on-screen `● REC` and elapsed time),
  wake-word detection **off**, no TTS playback.
- **Audio**: on session start send `hello` with `features.transcription =
  true` and one `{"type":"listen","state":"start"}`, then stream Opus
  (16 kHz mono, 60 ms frames) **continuously** with no per-utterance
  `listen` `stop` and no local endpointing — the hub segments with Silero
  VAD. Send `listen` `stop` only when the session ends.
- **Photos**: every ~30 s while recording (and on demand), capture a
  modest-resolution JPEG (≤ ~100 KB — do not reintroduce the issue #4
  PSRAM OOM) and `POST {image_url}?device_id={id}&purpose=transcript` with
  `Authorization: Bearer {image_token}`, off the audio path.
- **Device MCP tools** so the session can also be driven from the hub /
  dashboard: `transcription.start`, `transcription.stop`,
  `transcription.capture_photo` (all `readOnlyHint: false`,
  `destructiveHint: false`), `transcription.status` (`readOnlyHint: true`,
  returns `{recording, elapsed_s, photos_sent}`). Final tool names are
  whatever the firmware ships — the hub's linked-agent policy matches to
  them.
- **Check-in**: read the new `mode` field; `"transcription"` means the
  `transcriber` agent is assigned.
- **Reconnect**: a session survives a WebSocket drop — reconnect and resume
  streaming without another button press.

## Phasing

1. **PR 1 (hub)** — `Persona.transcription` + migration, dispatch wiring,
   seeded `transcriber` persona, persona-editor checkbox and list badge.
   Testable now with `tests/harness` and with a device that sets
   `features.transcription`.
2. **PR 2 (hub)** — photo captioning for `purpose=transcript`, check-in
   `mode` field, agent-detail relabel + Speak-box hide, transcript
   download.
3. **Firmware (`xiaozhi-esp32`)** — button, continuous streaming, recording
   indicator, periodic photo capture, `transcription.*` MCP tools.

## Open questions

- **End-of-session summary.** Should stopping a session optionally run one
  LLM pass over the transcript to produce a summary / action items? Cheap
  to add later as an opt-in per persona; out of scope for PR 1–2.
- **Speaker labels.** Silero VAD segments on silence, not on speaker
  change, so the transcript is a flat list of utterances. Diarization is a
  much bigger piece and not planned.
- **Retention.** Transcripts live in `conversation_history` and are wiped
  by `just reset-data`. If a demo needs them kept, that is a separate
  export-before-reset step.
- **Continuous capture without a persona.** A device could stream
  continuously today by setting `features.transcription` even with no
  `transcriber` persona assigned. That still works; the persona flag is
  the supported, discoverable path.
