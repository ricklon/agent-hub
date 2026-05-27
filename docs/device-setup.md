# Device setup

This guide explains how to configure a xiaozhi-firmware ESP32 device to
connect to your agent-hub server.

## Supported hardware

Any board running the [xiaozhi-esp32 firmware](https://github.com/78/xiaozhi-esp32)
(v2.2+) will work. Tested boards:

| Board | Wake word | Camera | Notes |
|-------|-----------|--------|-------|
| XIAO ESP32-S3 Sense | ✓ | ✓ | Built-in mic + camera |
| Waveshare ESP32-S3 Touch AMOLED 1.8 | ✓ | — | Touchscreen display |
| ESP32-C3 (basic) | — | — | Button press only, no wake word |

"Wake word" means the device listens for a phrase ("你好小智" by default) to
start a voice session without pressing a button.

## What you need

- A flashed ESP32 device (see the firmware repo for flashing instructions)
- The IP address of the machine running agent-hub
- The device's WiFi credentials (set during firmware flashing)

## Step 1 — Find your server IP

On the machine running agent-hub:

```sh
# Linux / macOS
ip route get 8.8.8.8 | awk '{print $7; exit}'

# Windows
ipconfig | findstr "IPv4"
```

Your server IP will look something like `192.168.1.39`. Both the server
machine and the ESP32 device must be on the same WiFi network.

## Step 2 — Set the OTA / check-in URL on the device

The device needs to know where agent-hub is. This URL is called the "OTA
URL" in the firmware's config screen (the firmware originally used this
endpoint for over-the-air updates; agent-hub uses it for device registration).

The URL format is:

```
http://YOUR_SERVER_IP:8003/xiaozhi/ota/
```

For example: `http://192.168.1.39:8003/xiaozhi/ota/`

**How to set it:**

1. Power on the device. If it has never been configured, it will create a
   WiFi access point named something like `xiaozhi-XXXX`.
2. Connect your phone or laptop to that access point.
3. Open a browser and go to `http://192.168.4.1` (the device's config page).
4. Enter your WiFi SSID and password.
5. Set the **OTA URL** field to `http://YOUR_SERVER_IP:8003/xiaozhi/ota/`
6. Save. The device will reboot and connect to your WiFi.

If the device was already configured but pointed at a different server,
access its config page over WiFi by holding the config button (check your
board's docs) or via the serial console.

## Step 3 — Verify check-in

When the device boots and connects to WiFi, it will POST to the OTA URL.
You should see it appear in the dashboard within a few seconds:

```
http://YOUR_SERVER_IP:8000/dashboard/
```

The device will show status `discovered`. It is now registered and assigned
the `hub-default` persona automatically — no activation step required.

## Step 4 — Start a voice session

How you start talking depends on the board:

| Method | How |
|--------|-----|
| Wake word | Say **"你好小智"** (nǐ hǎo xiǎo zhì) out loud |
| Button | Press and hold the board's touch/button, speak, release |

The device connects to agent-hub's WebSocket on first interaction. You will
see status change to `active` in the dashboard while a session is running.

## Assigning a persona

By default every new device gets the `hub-default` persona (voice, model,
and system prompt). To assign a different persona:

1. Go to `http://YOUR_SERVER_IP:8000/dashboard/`
2. Click the device in the list
3. Use the **Assign** dropdown to pick a persona
4. Start a new voice session — the new persona takes effect immediately

To create a new persona with a different voice or personality, go to
`http://YOUR_SERVER_IP:8000/dashboard/personas`.

## Troubleshooting

**Device not appearing in dashboard**

- Check the OTA URL is set correctly on the device (must include `http://`,
  the correct IP, port `8003`, and the path `/xiaozhi/ota/`)
- Make sure the device and server are on the same WiFi network
- Check server logs: `tail -f /tmp/agent-hub.log`
- Try pinging the server IP from another device on the same network

**Device appears but voice session won't start**

- Make sure `AGENT_HUB_SERVER_WEBSOCKET` is set in `.env` to
  `ws://YOUR_SERVER_IP:8000/xiaozhi/v1/`
- Without this, the check-in response won't include the correct WebSocket
  URL and the device won't know where to connect

**"I'm having trouble with that right now" from the assistant**

- The LLM API key may be missing or invalid — check `.env`
- Check the server log for API errors

**Device keeps rebooting**

- If you have a serial monitor open (e.g. via Arduino IDE or `screen`),
  close it — the ESP32 resets when the serial port opens/closes
