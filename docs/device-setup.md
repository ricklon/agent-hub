# Device setup

This guide explains how to flash and configure an ESP32 device to connect
to your agent-hub server. It uses the
[ricklon/xiaozhi-esp32](https://github.com/ricklon/xiaozhi-esp32) firmware
fork, which adds a local-server workflow on top of the upstream project.

## Supported boards

| Board | Wake word | Camera | Chip | Notes |
|-------|-----------|--------|------|-------|
| XIAO ESP32-S3 Sense | ✓ | ✓ | ESP32-S3 | Built-in mic + camera |
| Waveshare ESP32-S3 Touch AMOLED 1.8 | ✓ | — | ESP32-S3 | Touchscreen display |
| XIAO ESP32-C6 | — | — | ESP32-C6 | Button press only |
| XIAO ESP32-C3 | — | — | ESP32-C3 | Budget option, button only |
| LilyGo T-Display S3 | ✓ | — | ESP32-S3 | Color display |

"Wake word" means the device listens for **"你好小智"** (nǐ hǎo xiǎo zhì)
to start a session without pressing a button.

## Prerequisites

- [ESP-IDF v5.x](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/get-started/) installed and sourced
- A USB cable that carries data (not charge-only)
- The IP address of the machine running agent-hub (see below)

Find your server IP:
```sh
# Linux / macOS
ip route get 8.8.8.8 | awk '{print $7; exit}'

# Windows
ipconfig | findstr "IPv4"
```

## Step 1 — Clone the firmware

```sh
git clone https://github.com/ricklon/xiaozhi-esp32.git
cd xiaozhi-esp32
```

## Step 2 — Build and flash

The `switch-board.sh` script manages per-board build directories so
switching between boards doesn't wipe compiled artifacts.

```sh
# List available boards
./switch-board.sh list

# Build and flash (replace <board> with one from the list above)
./switch-board.sh xiao-esp32-s3-sense flash
./switch-board.sh waveshare/esp32-s3-touch-amoled-1.8 flash
./switch-board.sh xiao-esp32-c6 flash
./switch-board.sh xiao-esp32-c3 flash

# Flash to a specific port
./switch-board.sh xiao-esp32-c6 flash /dev/ttyUSB0
```

If you prefer raw idf.py commands:
```sh
. ~/esp/esp-idf/export.sh
idf.py -B build-xiao-esp32-c6 -p /dev/ttyACM0 flash
```

## Step 3 — Configure WiFi and server over serial

Once flashed, open a serial monitor at 115200 baud and use the built-in
serial commands to configure the device without reflashing.

```sh
# Open serial monitor (Ctrl+] to exit)
idf.py -B build-<board> monitor
```

**Set the server URL** (the most important step):
```
!server 192.168.1.39
```
This tells the device to check in at `http://192.168.1.39:8003/xiaozhi/ota/`.
You can also pass a full URL:
```
!server http://192.168.1.39:8003/xiaozhi/ota/
```

**Set WiFi credentials** (if the device doesn't already have them):
```
!wifi YourNetworkName YourPassword
```

**Check current config**:
```
!status
```
Output shows firmware version, IP address, OTA URL, and free heap. This
is the quickest way to confirm the device is pointing at the right server.

**Other serial commands:**

| Command | What it does |
|---------|-------------|
| `!wifi SSID PASS` | Store WiFi credentials |
| `!wifi list` | Show saved networks |
| `!server IP` | Set OTA URL to `http://IP:8003/xiaozhi/ota/` |
| `!server URL` | Set full custom OTA URL |
| `!status` | Show firmware version, IP, OTA URL, free heap |
| `!reboot` | Restart the device |
| any other text | Send directly to LLM as a chat message |

> **Waveshare board shortcut:** Long-press the boot button to show
> firmware version, WiFi SSID, IP address, and OTA URL on the display
> for 5 seconds — no serial monitor needed.

## Step 4 — Verify check-in

Reboot the device (`!reboot` or unplug/replug). When it connects to WiFi
it will POST to agent-hub's check-in endpoint. Open the dashboard:

```
http://YOUR_SERVER_IP:8000/dashboard/
```

The device appears with status `discovered`. It is registered and assigned
the `hub-default` persona automatically — no activation step needed.

If the device does not appear:
- Run `!status` and confirm the OTA URL is correct
- Make sure the device and server are on the same WiFi network
- Check server logs: `tail -f /tmp/agent-hub.log`

## Step 5 — Start a voice session

| Board type | How to start |
|-----------|-------------|
| Has wake word | Say **"你好小智"** out loud |
| Button only | Press and hold the board's button, speak, release |

The device connects to agent-hub's WebSocket on first interaction. Status
changes to `active` in the dashboard while a session is running.

## Assigning a persona (voice / personality)

Every new device gets `hub-default`. To change it:

1. Go to `http://YOUR_SERVER_IP:8000/dashboard/`
2. Click the device
3. Use the **Assign** dropdown to pick a persona
4. Start a new voice session — the persona takes effect immediately

To create a new persona with a different voice, go to
`http://YOUR_SERVER_IP:8000/dashboard/personas`.

Available Edge TTS voices: any name from the
[Microsoft voice list](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support?tabs=tts).
Common ones:
- `en-US-AriaNeural` (US female, default)
- `en-GB-RyanNeural` (British male)
- `en-AU-WilliamMultilingualNeural` (Australian male)
- `en-IE-EmilyNeural` (Irish female)

## Troubleshooting

**Device not appearing in dashboard**
- Run `!status` on the device — check the OTA URL matches your server IP and port 8003
- Make sure port 8003 is reachable: `curl http://YOUR_SERVER_IP:8003/xiaozhi/ota/`
- Check server logs: `tail -f /tmp/agent-hub.log`

**"I'm having trouble with that" from the assistant**
- LLM API key may be missing — check `.env` on the server
- Server logs will show the error

**Device keeps rebooting after serial monitor exits**
- The ESP32 resets when the serial port closes. This is normal — close the
  monitor before expecting the device to run stably.

**Wrong GPIO / microphone not working (C3 vs C6)**
- XIAO C3 and C6 use different GPIO numbers for the same physical pads.
  Always use the correct board target with `switch-board.sh`. Never flash a
  C3 build onto a C6 or vice versa.
