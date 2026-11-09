# Plan: every new thing tells you a code

Status: **proposed.** Replaces the shared enrollment token as the way devices,
robots, and page agents join a hub. Decisions recorded 2026-11-08:

1. Hard rule #4 changes from "no activation gate" to "no cloud or account
   activation; local pairing with a code is allowed and on by default".
2. Pairing is **on** by default, including for classroom use.
3. `wss://` is **preferred**: the hub warns about plain `ws://` but still allows it.
4. The firmware behaviour this plan relies on is checked on a flashed board
   before any hub code is written (see [Step 0](#step-0--hardware-check)).

## The rule users learn

> **Every new thing tells you a code. Type it into the hub.**

A beginner plugs in a board, hears "four seven two nine one three", opens the
dashboard, clicks **Add device**, and types it. A robot builder runs their robot,
sees `Pair code: 472913` in the console, and does the same. There is one
gesture to learn, no secret to copy, and nothing to configure first.

## Why: what is weak today

| Weakness | Where | Consequence |
| --- | --- | --- |
| One enrollment token is shared by every device and robot | `server.enrollment_token`, `checkin.py`, `agent_api.py` | Anyone holding it plus a board's MAC can check in as that board. |
| Each check-in issues a new WebSocket token | `checkin.py` → `store.issue_websocket_token` | That impostor's check-in replaces the real board's token: a takeover that locks the real board out. |
| Bearer tokens are stored in plain text | `Agent.websocket_token` in SQLite | Anyone who reads the database can impersonate every agent. |
| With no enrollment token set, anything that checks in is trusted | default config | Any device on the network becomes an agent and can spend LLM credit. |
| The audit log names operators but not agents | `dashboard/audit.py` | You can't tell which robot or script did something. |

## What the firmware already does

Read from upstream `78/xiaozhi-esp32` at tag `v2.5.2` (`main/ota.cc`,
`main/application.cc`, `main/protocols/websocket_protocol.cc`). This has
not yet been observed on hardware. Step 0 does that.

- If the check-in response contains `"activation": {"code", "message",
  "challenge", "timeout_ms"}`, the board speaks each digit of `code` using
  built-in sounds and shows `message` on its screen.
- It then sends `POST <check-in URL>/activate` with headers `Device-Id`,
  `Client-Id`, and `Activation-Version`. The body is either `{}` or, when the
  eFuse holds a serial number, `{"algorithm": "hmac-sha256", "serial_number",
  "challenge", "hmac"}`. **200** means activated. **202** means still waiting,
  and the board retries every 3 s. Any other status means it retries every
  10 s. After 10 attempts it checks in again and repeats the code.
- An activation response requires a `challenge`. Without one the board never
  sends `/activate`.
- No voice session starts until activation succeeds. Pressing the button just
  replays the code.
- The WebSocket token comes from the check-in response's `websocket.token` and
  is stored in NVS. The board sends it as `Authorization: Bearer <token>`,
  together with `Device-Id`, `Client-Id`, and `Protocol-Version`.
- On chips with HMAC hardware, a key burned into eFuse `HMAC_KEY0` signs the
  challenge and the key itself is never sent. Hobby boards almost never have
  that key, so the hub treats HMAC as an optional extra, never a requirement.

Because the existing `/xiaozhi/ota/` route gains `/xiaozhi/ota/activate`,
hard rule #3 still holds.

## Design

### Pairing attempts, not pairing devices

A code belongs to **one check-in attempt**, not to a MAC address. When an
unpaired board checks in, the hub creates an attempt:

| Field | Purpose |
| --- | --- |
| `attempt_id` | Primary key. |
| `device_id`, `client_id`, `ip_address` | What the board claimed, shown to the person pairing it. |
| `code` | 6 digits from `secrets`, unique among live attempts. |
| `challenge` | Random. The board echoes it on `/activate`, which is how the hub finds the attempt. |
| `token_hash` | Hash of the WebSocket token given to this attempt only. |
| `expires_at` | About 10 minutes. After that the board checks in again and gets a new code. |
| `approved_by`, `approved_at` | Set when a person confirms the code. |

Someone who spoofs a board's MAC gets **their own** attempt, and their own
code is spoken by **their own** speaker. The person pairing types the code they
heard from the board in front of them, so only that attempt is approved, and
only that attempt's token becomes the board's credential. Every other live
attempt for the same `device_id` is discarded.

### Board flow

1. **Unpaired check-in.** The hub returns the usual fields plus
   `activation: {code, challenge, message: "Pair code 472 913. Type it into
   the hub."}`, and a `websocket` section whose token works only after
   approval. Step 0 settles whether the token goes in this response or the
   next one.
2. **`/activate` polling.** The hub looks the attempt up by `challenge` and
   `Device-Id` and returns 202 while it is pending, 200 once it is approved,
   and 410 once it has expired.
3. **Add device in the dashboard.** The page lists waiting boards by name, MAC,
   and IP but **never shows their codes**. The person types the code they heard.
   The hub approves the matching attempt and records them as the owner.
4. **Paired check-in.** A paired board checks in with its token and gets no
   activation block and no new token. A check-in that lacks the current token
   starts a new attempt, so it shows up in the dashboard and never silently
   replaces the real board.
5. **Re-pair** in the dashboard clears the board's credential. Its next
   check-in pairs again. Use it for a factory-reset or reflashed board.

### Robots and scripts

The same idea, using the [device-code flow](https://datatracker.ietf.org/doc/html/rfc8628):

1. On first run the robot generates an Ed25519 key pair, or reuses an existing
   `ssh-ed25519` key, and calls `POST /agent/register` with the public key.
2. The hub replies with a pairing code. The robot prints it, and shows it or
   speaks it if it can.
3. A person types the code into **Add device**, and the hub binds the public key
   to the agent.
4. From then on the robot authenticates by signing a challenge, which gets it a
   short-lived session token (about 15 minutes) for heartbeats and the MCP
   bridge. Calls that do something (registration changes, tool results that
   carry side effects) are signed individually, covering method, path, body
   hash, timestamp, and nonce.

The robot never holds a secret the hub also holds. Revoking a key in the
dashboard ends the robot's session at its next refresh.

A small helper (`scripts/agent_pair.py`, or a function in the robot SDK) does
key generation, pairing, and session refresh, so a robot builder writes no
cryptography code.

### Page agents

These already need a verified Cloudflare Access operator and record that
operator as owner (#86). Opening the page while logged in **is** the
approval, so no code is needed. A kiosk page with nobody logged in shows a
code instead. Its key pair is a WebCrypto key the page can't export, stored
in IndexedDB.

### People

- **Passkeys (WebAuthn)** for the dashboard. They resist phishing, are often
  hardware-backed, and you log in with Touch ID, Windows Hello, or a phone.
  Password login and Cloudflare Access remain as alternatives.
- **First run:** when there are no admins yet, the server prints a one-time
  setup link to its log, valid for 1 hour, like Jupyter's token link. Opening
  it creates the admin passkey.
- **Inviting someone:** an admin makes an invite link that sets the role, and
  the person opening it registers their own passkey.
- SSH keys remain an option for people who script against the hub. They
  register their key the same way a robot does.

### Managing agents

The agents list gains a pairing column: **Waiting**, **Paired** (with owner
and date), or **Revoked**. A key fingerprint is shown for keyed agents. Row
actions: **Re-pair**, **Revoke**, **Rename** (#118). Each audit-log entry
records the key fingerprint or device token that made the call.

### Transport

`wss://` is preferred. When the hub has a public `https` URL (Cloudflare
Tunnel, Let's Encrypt), check-in returns a `wss://` URL. The dashboard shows a
warning when paired boards use plain `ws://` while the hub is reachable from
beyond its LAN. It is a warning rather than a block, so a laptop on class-night
Wi-Fi still works. The plain `ws://` risk is that anyone sniffing the Wi-Fi can
capture a token and replay it. Step 0 also checks whether the firmware
validates server certificates.

## Public hubs (the DigitalOcean droplet)

A droplet has no LAN boundary: the device hostname is reachable by anyone
from the moment it starts (see `docs/deployment.md` → *DigitalOcean
Droplet*). Pairing is what makes that safe. It also delivers the first two
items of *Remaining Work Before Public Internet*: per-device credentials and
rate limiting.

**What changes for users.** Today a public hub needs Rick's forked firmware,
because only the fork can send `enrollment_token` and stock firmware gets a
401. With pairing, a beginner can flash **stock** firmware, point it at
`https://hub.<domain>/xiaozhi/ota/`, and type the code the board says.

**What the plan adds for public hubs:**

- **Rate limits are required here, not optional.** Limit new pairing attempts
  per IP (a few per minute) and cap the number of live attempts per hub, so
  strangers can't flood **Add device**. Old attempts are dropped first.
  Limit wrong codes per operator, as on a LAN.
- **Waiting boards are quiet in the dashboard.** **Add device** lists only
  attempts made in the last 10 minutes, and newest first, so a person pairing
  the board in front of them isn't hunting through strangers' attempts.
- **`enrollment_token` is still allowed, as an optional filter.** When it is
  set, a check-in without it gets a 401 before any pairing attempt is created.
  That suits hubs that only ever serve the fork. It never replaces pairing:
  having the token still doesn't pair a board.
- **`wss://` is the default whenever `AGENT_HUB_PUBLIC_HOST` is set.** Caddy
  already terminates TLS for the device hostname, so check-in hands out a
  `wss://` URL. A paired board that still connects over plain `ws://` on a
  public hub shows a warning in the dashboard.
- **Caddy routes.** `/xiaozhi/ota/activate` and `/checkin/activate` are
  already covered by the existing `/xiaozhi/ota/*` and `/checkin/*` blocks.
  Every new device-facing path must get a block in `deploy/caddy/Caddyfile`,
  and every operator path must not get one. The Caddyfile's 404 default does
  the rest.
- **Approving stays behind Access.** **Add device** is a dashboard page, so on
  the droplet it lives on the Cloudflare Access hostname, never on the device
  hostname.

**Upgrading the shared droplet.** Devices already enrolled with the fork's
enrollment token are carried over as *paired*, the same as Step 2's
upgrade path, so they keep working. The cloud-init enrollment token stays set
until the operator chooses to drop it.

## Hard limits we accept

- **A board's token is stored in plain text in its flash.** Physical access to
  the board means access to its identity until someone revokes it in the
  dashboard. Fixing that needs firmware that holds a non-exportable key
  (see [Later](#later)).
- **HMAC activation is used only when the hub has been given that board's
  key**, which is a vendor-provisioning step hobbyists don't do.

## Steps

Each step is its own PR, and each one leaves the hub working.

### Step 0 — hardware check

Flash stock `v2.5.2` to a Waveshare ESP32-S3 Touch AMOLED 1.8, point
`CONFIG_OTA_URL` at a hub running `spike/pairing-activation`
(`AGENT_HUB_PAIRING_SPIKE=1`), and record the results below.

Run it twice:

1. **Throwaway droplet first.** Provision a fresh droplet with cloud-init,
   not the shared one, and with `AGENT_HUB_PUBLIC_HOST` set so Caddy serves
   `wss://`. Stock firmware can't send the enrollment token, so clear it for
   the length of the test. That leaves the droplet open to anyone, so destroy
   it straight after the test. The spike's `/pairing-spike/*` endpoints aren't
   in the Caddyfile and return 404 from outside. Approve codes over SSH:
   `ssh root@<droplet> curl -s localhost:8003/pairing-spike/approve?code=…`.
   Set an `llm.spend` cap anyway.
2. **LAN second**, over plain `ws://`, for class night. On a WSL host this
   needs mirrored networking or port forwarding to reach the hub.

Record:

- That the code is spoken and shown, and exactly which characters are displayed.
- Whether the board reads `websocket.token` from the response that carried
  `activation`, or only from the check-in after `/activate` returns 200.
- What the board does right after 200: does it check in again, and when?
- How it behaves on 410, or another error, and on a hub restart while it is
  waiting.
- Whether `wss://` with a public CA certificate works, and whether a
  self-signed certificate is rejected.
- Baseline check-in and a voice session on v2.5.2. `lessons-learned.md`
  dates from 2.2.6.

Write the results into `docs/lessons-learned.md`.

### Step 1 — hash and pin tokens (no UX change)

Store token hashes instead of tokens, hashing existing ones in a migration.
Stop re-issuing a token to a board that already has one unless the old token
is presented. Includes regression tests for the takeover.

### Step 2 — board pairing

Add the pairing-attempts table, the activation block in check-in (with a
fixture in `tests/server/test_protocol.py`), `/activate` (served under both
`/checkin/` and `/xiaozhi/ota/`), the **Add device** page, **Re-pair**, and
rate limits: per user on wrong codes, and per IP and per hub on new attempts
(see [Public hubs](#public-hubs-the-digitalocean-droplet)).

**Upgrading:** boards in the registry before this release are marked
*paired (carried over)* and pinned on their next check-in, so a running
classroom doesn't need to re-pair everything. The dashboard lists them so an
operator can review them.

### Step 3 — keys for robots and scripts

Add the keys table, keyed registration with pairing codes, challenge login,
short-lived session tokens, signed calls that do something, and the pairing
helper. `enrollment_token` becomes an optional pre-filter that stops strangers
from creating pairing attempts. It no longer grants trust.

### Step 4 — passkeys and first-run setup

WebAuthn registration and login, the first-run setup link, and invite links.

### Step 5 — wss guidance

Pick `wss://` automatically when `AGENT_HUB_PUBLIC_HOST` is set, show the
dashboard warning, and update `docs/deployment.md`. That includes replacing the
*Before pointing devices at it* steps on configuring each device with the
enrollment token, and ticking off the per-device credentials and rate limiting
in *Remaining Work Before Public Internet*.

## Data model (needs sign-off, per AGENTS.md)

- `pairing_attempts` — as above.
- `agent_keys` — `agent_id`, `public_key` (OpenSSH format), `fingerprint`,
  `created_by`, `created_at`, `revoked_at`.
- `operator_passkeys` — `operator_id`, `credential_id`, `public_key`,
  `sign_count`, `created_at`, `last_used_at`.
- `Agent` — `websocket_token` becomes `websocket_token_hash`; adds
  `paired_by` and `paired_at`.

## Dependencies

- Ed25519 and OpenSSH key parsing: `cryptography`, already installed through
  `pyjwt[crypto]`. No new dependency.
- WebAuthn: probably `webauthn` (py_webauthn). This is a new dependency, so it
  needs approval at Step 4.

## Open questions

- Signature format for robots: raw Ed25519 over a canonical string (simplest),
  or SSHSIG (`ssh-keygen -Y sign`), which lets `ssh-agent` hold the key but
  needs a small parser.
- Who may pair: anyone with the operator role, or admins only?
- Should an unpaired board say something more helpful when the hub can't be
  reached from the dashboard? For example: "Open http://hub.local:8001 and
  type this code."

## Later

- A firmware fork that keeps an Ed25519 key on the board, through the
  ESP32-S3 Digital Signature peripheral or a key protected by flash
  encryption. The board would then sign challenges the way robots do, and
  `agent_keys` already has room for that.
- The hub as an SSH certificate authority, issuing short-lived user
  certificates that carry roles, so revocation is just waiting for expiry.
