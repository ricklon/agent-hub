# Demo quickstart

This guide is for people setting up agent-hub for a demo, class, or study
group. It splits the work into two phases: **before you arrive** (the slow
parts) and **at the event** (the fast parts).

---

## Why local-first?

agent-hub runs speech recognition on your own machine using
[SenseVoiceSmall](https://github.com/FunAudioLLM/SenseVoice). Your voice
never leaves your network. Only the text transcript is sent to the cloud LLM.

This means:
- No audio privacy concerns
- Works on a local network with no internet (except for the LLM call)
- No per-utterance ASR cost
- Consistent latency regardless of upstream API availability

The tradeoff is a one-time ~1 GB model download. Do it before the event on
a good connection.

---

## Before the event (do at home)

### 1. Install prerequisites

**uv** (Python package manager):
```sh
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**just** (command runner):
```sh
# macOS
brew install just

# Linux
curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to /usr/local/bin

# Windows
winget install Casey.Just
```

### 2. Clone and install

```sh
git clone https://github.com/ricklon/agent-hub.git
cd agent-hub
just install          # installs Python dependencies into .venv/
```

This takes 2–5 minutes. No downloads yet, just packages.

### 3. Download the speech models (~1 GB, one-time)

```sh
just download-models
```

This downloads SenseVoiceSmall (speech-to-text) and Silero VAD
(voice activity detection) into `models/`. It takes 5–15 minutes on a
fast connection — do it at home, not on conference WiFi.

**You only ever need to do this once.** The models live in `models/` and
are reused every run.

### 4. Get an API key

agent-hub needs a cloud LLM for conversation. The easiest option is
[OpenRouter](https://openrouter.ai) — free tier, many models, one key.

1. Go to [openrouter.ai](https://openrouter.ai) and sign in
2. Click your profile → **Keys** → **Create key**
3. Copy the key (starts with `sk-or-`)

### 5. Configure

```sh
cp .env.example .env
```

Open `.env` and set your key:
```
AGENT_HUB_LLM_OPENAI_API_KEY=sk-or-your-key-here
```

### 6. Test run at home

```sh
just run
```

Open `http://localhost:8000/dashboard/` — you should see the dashboard.
No devices will appear yet; that's fine. If the server starts without
errors, you're ready.

---

## At the event

### 1. Find your laptop's IP on the local network

```sh
# Linux / macOS
ip route get 8.8.8.8 | awk '{print $7; exit}'

# Windows
ipconfig | findstr "IPv4"
```

You'll get something like `192.168.1.42`. Everyone on the same WiFi can
reach your server at this address.

### 2. Set the WebSocket URL

Open `.env` and add (or update) this line:
```
AGENT_HUB_SERVER_WEBSOCKET=ws://192.168.1.42:8000/xiaozhi/v1/
```

Replace `192.168.1.42` with your actual IP. This tells devices where to
connect for voice sessions.

### 3. Start the server

```sh
just run
```

Share this URL with the room:
```
http://192.168.1.42:8000/dashboard/
```

Anyone on the same WiFi can watch the dashboard as devices connect and
conversations happen.

### 4. Connect a device

Power on an ESP32 running [ricklon/xiaozhi-esp32](https://github.com/ricklon/xiaozhi-esp32)
firmware. If it hasn't been pointed at your server before, connect a USB
cable and run:

```sh
# in the firmware repo
./switch-board.sh <board> monitor
```

Then in the serial console:
```
!server 192.168.1.42
!status        # confirm OTA URL updated
!reboot
```

The device will appear in the dashboard within a few seconds of rebooting.

### 5. Start talking

- **Wake word boards** (S3, Waveshare): say **"你好小智"**
- **Button boards** (C3, C6): press and hold the button, speak, release

The first turn after a fresh server start takes a few extra seconds — the
ASR model loads into memory on first use. Every turn after that is faster.

---

## What to show / talking points

**The pipeline — no audio leaves the room:**
> "When you speak, the audio goes from the device to this laptop. Speech
> recognition runs here locally using SenseVoiceSmall. Only the text is
> sent to the cloud model. The reply comes back as text, gets converted to
> speech here, and streams back to the device."

**Personas — give each device a different personality:**
> Go to `/dashboard/personas`, create a new one with a different voice
> (try `en-GB-RyanNeural` for British, `en-AU-WilliamMultilingualNeural`
> for Australian). Assign it to a device. The change takes effect on the
> next voice session.

**MCP tools — the device can act, not just talk:**
> "The device advertises what tools it has — volume control, screen
> brightness, camera on boards that have one. The LLM can call those
> tools directly. Say 'turn the volume up' and it does it."

**Multiple devices, one server:**
> Connect a second device. Both appear in the dashboard. Each has its own
> conversation history and persona.

---

## Troubleshooting at the event

**Device not appearing in dashboard**
- Run `!status` on the device — confirm the OTA URL is your laptop's IP
- Check that the device and laptop are on the same WiFi
- Verify port 8003 is reachable: `curl http://192.168.1.42:8003/xiaozhi/ota/`

**"I'm having trouble with that" from assistant**
- LLM API key issue — check `.env`, restart server

**First response is very slow**
- Normal — ASR model loads on first use. Subsequent turns are faster.

**Server not starting**
- Make sure no other process is on ports 8000, 8001, or 8003:
  `ss -tlnp | grep -E '8000|8001|8003'`
- Kill any leftover server: `pkill -f agent_hub.server`
