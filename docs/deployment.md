# Secure Deployment

`agent-hub` is LAN-first software. ESP32 devices need unauthenticated first
contact by design, while the dashboard can change personas, trigger device
tools, and read transcripts. Treat those as different trust boundaries.

## Recommended Default

Run the Docker container on a trusted LAN host and administer it over
Tailscale.

1. Keep the host firewall closed to the public internet.
2. Allow LAN devices to reach:
   - `8003` for `/checkin/` and `/xiaozhi/ota/`
   - `8000` for `/xiaozhi/v1/` WebSocket sessions
3. Restrict dashboard access to trusted operators.
4. Set dashboard auth before any non-local access:

```sh
AGENT_HUB_SERVER_DASHBOARD_USERNAME=admin
AGENT_HUB_SERVER_DASHBOARD_PASSWORD=change-this-long-random-password
```

5. Set an enrollment token before allowing devices to check in over any
   untrusted network:

```sh
AGENT_HUB_SERVER_ENROLLMENT_TOKEN=change-this-long-random-token
```

When this is set, check-in requests must include the token. The server accepts
any of these forms:

- `X-Agent-Hub-Enrollment-Token: <token>`
- `Authorization: Bearer <token>`
- `/xiaozhi/ota/?enrollment_token=<token>`
- JSON body field `enrollment_token` or `agent_hub.enrollment_token`

On successful check-in, the server stores a fresh per-device WebSocket token
and returns it as `websocket.token`. The xiaozhi firmware then sends it on
`/xiaozhi/v1/` as `Authorization: Bearer <token>`.

6. Set a host allowlist for the dashboard whenever it is reachable off
   localhost:

```sh
AGENT_HUB_SERVER_ALLOWED_HOSTS=hub.local,192.168.5.6,agent-hub.example.com
```

List every name and IP you actually browse to. Requests arriving with any
other `Host` are rejected with `400`. This is what blocks **DNS rebinding**,
where a malicious page re-points its own domain at your hub's LAN address;
without it, an attacker's page can send a `Host` and `Origin` that agree with
each other and pass the cross-origin check on the way to `/reboot` or
`/inject`.

It is enforced on the dashboard port only. Devices connect by bare LAN IP, so
a hostname allowlist on the device ports would reject check-in in a way that
looks like a network fault.

7. Set an image token before using camera/image tools outside a private lab:

```sh
AGENT_HUB_SERVER_IMAGE_TOKEN=change-this-long-random-token
```

## Port Map

| Port | Intended callers | Exposure |
| --- | --- | --- |
| `8000` | ESP32 devices — `/xiaozhi/v1/` and image explain | LAN/Tailscale only |
| `8001` | Human operators — `/dashboard/` | Tailscale or authenticated HTTPS only |
| `8003` | ESP32 devices — `/checkin/`, `/xiaozhi/ota/` | LAN/Tailscale only |

Ports are enforced as a trust boundary: each port binds its own application
with only its own routes mounted. `/dashboard/` returns `404` on `8000` and
`8003`, and the device endpoints return `404` on `8001`. Opening a device port
to the LAN therefore does not expose the dashboard.

### Transcript photos

The image endpoint accepts the firmware's multipart `purpose=transcript`
field. These capture-only uploads are saved immediately as chronological
`image` entries in the device transcript and skip vision inference. Ordinary
camera-tool uploads without that field retain the asynchronous image-explain
flow and are attached to the corresponding assistant turn.

> **If you configure two of these ports to the same number, their routes
> merge onto that port and the boundary is gone.** This is normal in local
> development. The server logs a warning when the dashboard shares a port with
> a device endpoint; set a dashboard password in that setup.

Do not publish these ports directly on a cloud VM without a firewall or
reverse proxy.

## Tailscale Pattern

Use this when the server is at home, in a classroom, or in a makerspace.

### App-only sidecar with public device access

This gives Agent Hub its own tailnet node. It does not add the Docker host to
the tailnet or publish Agent Hub ports on the host. Tailscale Funnel exposes
only the device protocol publicly; the dashboard remains tailnet-private.

1. In the Tailscale admin console, create a one-off, non-ephemeral auth key.
   Put it temporarily in the gitignored `.env` file for the first start:

```sh
TS_AUTHKEY=tskey-auth-...
TAILSCALE_HOSTNAME=agent-hub
AGENT_HUB_PUBLIC_HOST=agent-hub.<tailnet-name>.ts.net
AGENT_HUB_SERVER_ENROLLMENT_TOKEN=change-this-long-random-token
AGENT_HUB_SERVER_IMAGE_TOKEN=change-this-other-long-random-token
```

2. Start the sidecar deployment:

```sh
just tailnet-up
just tailnet-status
```

After the node successfully joins, remove `TS_AUTHKEY` from `.env`. The node
identity persists in `data/tailscale/`, and `TS_AUTH_ONCE=true` reuses it on
later starts. The auth key is not needed again unless that state is deleted.

3. Enable Funnel for this node or its tag in the Tailscale policy. Funnel
   requires the `funnel` node attribute and HTTPS support in the tailnet.

4. Configure the device with this OTA/check-in URL, substituting the same
   enrollment token stored in `.env`:

```text
https://agent-hub.<tailnet-name>.ts.net/xiaozhi/ota/?enrollment_token=<token>
```

The public HTTPS endpoint routes only `/checkin/`, `/xiaozhi/ota/`,
`/xiaozhi/heartbeat/`, and `/xiaozhi/v1/`. After authenticated check-in, Agent Hub advertises
`wss://agent-hub.<tailnet-name>.ts.net/xiaozhi/v1/` and issues the device a
per-device WebSocket bearer token.

Devices send that bearer token in the `Authorization` header of
`POST /xiaozhi/heartbeat/` every 60 seconds. Never put the per-device token in
a URL or query string. Agent Hub marks the device offline after 180 seconds
without a heartbeat or live voice session.

5. Open the private dashboard from a device on the tailnet:

```text
https://agent-hub.<tailnet-name>.ts.net:8443/dashboard/
```

Port `443` is Funnel/public. Port `8443` is Serve/tailnet-only. The dashboard
is never routed on the public port.

Tailscale state persists at `data/tailscale/`, so the app keeps its node
identity across container replacement. `just tailnet-down` stops the
deployment without deleting that identity.

If `server.allowed_hosts` is configured, include the full `*.ts.net` name that
operators use. Keep dashboard Basic auth enabled if untrusted people or shared
devices are members of the tailnet.

This override removes the base Compose port list. The ESP32 reaches the
sidecar's public Funnel URL just as it reached `api.tenclass.net`; it does not
need Tailscale software or LAN reachability to the laptop.

### Host-installed alternative

If the Docker host is already intentionally on the tailnet, the normal Compose
deployment is reachable at `http://<host-magicdns-name>:8001/dashboard/`.
Host-level `tailscale serve http://127.0.0.1:8001` can add HTTPS, but it exposes
the service through the host's identity rather than an app-specific node.

For xiaozhi firmware on the same LAN:

```sh
AGENT_HUB_SERVER_WEBSOCKET=ws://192.168.x.y:8000/xiaozhi/v1/
```

For devices that reach the hub through an HTTPS edge:

```sh
AGENT_HUB_SERVER_WEBSOCKET=wss://agent-hub.example.com/xiaozhi/v1/
```

## HTTPS Reverse Proxy Pattern

Use this when remote devices or remote operators must connect without
Tailscale. Put Caddy, nginx, Traefik, or Cloudflare Tunnel in front of
`agent-hub`.

Minimum proxy policy:

- Require HTTPS.
- Require dashboard auth at the app, proxy, or both.
- Proxy WebSocket upgrades for `/xiaozhi/v1/`.
- Keep `/dashboard/` separate from device endpoints in logs and access rules.
- Rate-limit `/checkin/`, `/xiaozhi/ota/`, and WebSocket connection attempts.
- Prefer source IP allowlists for demos and temporary events.

Example Caddy shape:

```caddy
agent-hub.example.com {
    encode zstd gzip

    handle_path /dashboard* {
        reverse_proxy 127.0.0.1:8001
    }

    handle /xiaozhi/v1/* {
        reverse_proxy 127.0.0.1:8000
    }

    handle /checkin/* {
        reverse_proxy 127.0.0.1:8003
    }

    handle /xiaozhi/ota/* {
        reverse_proxy 127.0.0.1:8003
    }
}
```

Set:

```sh
AGENT_HUB_SERVER_WEBSOCKET=wss://agent-hub.example.com/xiaozhi/v1/
AGENT_HUB_SERVER_DASHBOARD_PASSWORD=change-this-long-random-password
AGENT_HUB_SERVER_ENROLLMENT_TOKEN=change-this-long-random-token
AGENT_HUB_SERVER_IMAGE_TOKEN=change-this-long-random-token
AGENT_HUB_SERVER_ALLOWED_HOSTS=agent-hub.example.com
```

Behind a proxy, `allowed_hosts` must name the host the **browser** uses, since
that is what arrives in the `Host` header. If the proxy rewrites `Host` to
something else, add that value to `dashboard_allowed_origins` too — once
either allowlist is set it becomes exhaustive, and the request's own `Host` is
no longer trusted implicitly.

## DigitalOcean Droplet

A public cloud droplet is the least trusted environment `agent-hub` runs in:
there is no LAN boundary, so the device ports and the dashboard are reachable
from the internet the moment the container starts. Read
[Remaining Work Before Public Internet](#remaining-work-before-public-internet)
before leaving one running.

The droplet swaps SenseVoice for a lighter ASR but keeps speech local at both
ends:

| | Local (`Dockerfile`) | Droplet (`Dockerfile.do`) |
| --- | --- | --- |
| ASR | SenseVoice via funasr_onnx (torch) | Moonshine (onnxruntime) |
| TTS | KittenTTS or Edge | Edge by default, KittenTTS installed |
| Image | 1.1GB | 532MB |
| Idle RAM | — | 79MB |

Image sizes are `docker image inspect --format '{{.Size}}'`. The `docker images`
"disk usage" column reads much higher for both (4.08GB and 2.08GB) because it
counts build cache and shared layers — the two are not comparable.

Torch is confined to the `full` extra — SenseVoice in either form, plus the
`silero-vad` package. The droplet skips it, running the Silero VAD from the
bundled `.onnx` through onnxruntime instead. The ASR registry imports lazily,
so the droplet image starts fine; selecting `funasr` or `funasr_onnx` there
fails when that provider is constructed.

**`funasr-onnx` is not torch-free**, despite declaring only onnxruntime in its
metadata: `sensevoice_bin.py` imports torch for the CTC decode path, and
imports `jieba` without declaring it at all. Dependency-graph tools will tell
you otherwise — the local image cannot drop torch while SenseVoice is its
default ASR. Switching the default to Moonshine is what would make a
torch-free local image possible; see the benchmark below for why we don't.

### Debugging ASR accuracy in the field

Field accuracy is worse than the benchmark below, and transcripts alone cannot
tell you whether the model is weak or the audio was already degraded before it
reached the model. Setting `server.debug_audio_dir` captures the exact WAV each
provider received, plus what it returned:

```sh
AGENT_HUB_SERVER_DEBUG_AUDIO_DIR=data/asr-captures
```

Then replay real utterances through providers side by side:

```sh
PROVIDERS=moonshine,funasr_onnx uv run --extra full python \
    scripts/compare_asr_captures.py data/asr-captures
```

It prints per-clip signal statistics next to each provider's transcript.
`peak` near 1.0 with `clipped%` above ~0.1 means the input gain is too high;
`rms` below ~0.01 means too quiet or too far from the microphone. Either points
at capture rather than the model — worth ruling out before paying for a larger
droplet.

**Capture is off by default and should stay off.** It records everything said
to a device, so it is a privacy decision rather than a debug flag.

### ASR benchmark: Moonshine vs SenseVoice

Measured with `scripts/bench_asr.py` over 73 LibriSpeech utterances (481s of
real speech with ground-truth transcripts), through the actual provider
interfaces:

| Provider | WER | Median latency (many-core) | Peak RSS |
| --- | --- | --- | --- |
| Moonshine tiny | 13.22% | 0.332s | 272MB |
| Moonshine base | 8.17% | 0.511s | 534MB |
| **SenseVoice (`funasr_onnx`)** | **7.22%** | **0.292s** | 744MB |

Constrained to 1 vCPU, the droplet case (25 utterances, 206s):

| Provider | WER | Median latency | Real-time factor |
| --- | --- | --- | --- |
| Moonshine tiny | 13.17% | 4.10s | **0.73** |
| Moonshine base | 8.18% | 6.19s | 1.06 — too slow |
| SenseVoice | **6.99%** | 5.50s | 0.88 |

**SenseVoice stays the default.** It is the most accurate of the three *and*
the fastest on a normal machine — the model-size numbers suggest the opposite,
which is why this was worth measuring. Moonshine tiny makes roughly twice as
many errors; Moonshine base nearly closes the accuracy gap but exceeds
real time on 1 vCPU.

The droplet still uses Moonshine tiny, for headroom rather than speed: 272MB
against 744MB peak RSS, a 532MB image against 1.1GB, and RTF 0.73 against
0.88 leaves room for the LLM and TTS on the same core. On an `s-2vcpu-4gb`
droplet or larger, `AGENT_HUB_ASR_DEFAULT_PROVIDER=funasr_onnx` buys roughly
half the error rate — but needs the torch-bearing image, so build with the
default `Dockerfile` rather than `Dockerfile.do`.

Caveat: LibriSpeech is clean, read audiobook speech, and its utterances
average 6.6s — far longer than a device command. Field accuracy on ESP32 mic
audio will be worse for every provider, and device latencies proportionally
lower. Treat this as a relative ranking, not a prediction.

KittenTTS looks like it needs torch, but does not. Its `misaki[en]` dependency
declares `spacy-curated-transformers`, which pulls torch, and the English G2P
path never imports it — so `tool.uv.override-dependencies` in `pyproject.toml`
drops it. `spacy` itself *is* used and stays. If a future misaki release starts
using that package, the override is the first thing to revisit.

The Moonshine, Silero, and KittenTTS models are baked into the image. `models/`
is deliberately **not** a bind mount in `docker-compose.do.yml` — mounting it
would shadow them with the host's empty directory on a fresh droplet — and the
container starts with `uv run --no-sync` so a reboot never depends on GitHub
being reachable. Verified with `--network none`.

### Why the droplet defaults to Edge TTS

KittenTTS is installed and its model is baked in, but it is **CPU-bound and
scales hard with cores**. Measured in the droplet image, synthesizing a
4.4-second reply:

| vCPU | Synthesis time | Real-time factor |
| --- | --- | --- |
| 1 | 9.2s | 2.11 — unusable |
| 2 | 4.1s | 0.94 — no headroom |
| 4 | 0.7s | 0.16 — comfortable |

A real-time factor above 1.0 means speech is generated slower than it plays,
so streaming does not rescue it. On the recommended `s-1vcpu-2gb` droplet
KittenTTS cannot keep up, and ASR and the LLM stream compete for the same
core. Hence Edge TTS by default.

On `s-4vcpu-8gb` or larger, `AGENT_HUB_TTS_DEFAULT_PROVIDER=kitten` makes the
droplet fully local — no rebuild needed, since the package and model already
ship in the image.

### Provisioning

```sh
doctl compute droplet create agent-hub \
  --image docker-20-04 --size s-1vcpu-2gb --region nyc1 \
  --user-data-file deploy/cloud-init.sh
```

Use at least 2GB: the server needs ~250MB with a session live, but building
the image on the droplet needs more. First boot takes ~5 minutes.

`deploy/cloud-init.sh` installs Docker, clones the repo to `/opt/agent-hub`,
generates a random dashboard password **and enrollment token**, sets
`allowed_hosts` to the droplet's public IP, and writes them all to
`/root/agent-hub-credentials.txt`. It never deploys the template's placeholder
password, and aborts rather than starting if a substitution failed to match.

Because the token is generated, **devices must present it to check in** — a
droplet is not a LAN, so open enrollment would let anyone who finds port 8003
register against your hub. The credentials file lists the three ways to send
it. To go back to open enrollment, clear `AGENT_HUB_SERVER_ENROLLMENT_TOKEN`
and restart; only do that behind a firewall.

The LLM key is deliberately left unset — add it and restart:

```sh
ssh root@<droplet-ip>
cat /root/agent-hub-credentials.txt
vi /opt/agent-hub/.env.do          # AGENT_HUB_LLM_OPENAI_API_KEY=sk-or-...
cd /opt/agent-hub && docker compose -f docker-compose.yml -f docker-compose.do.yml restart
```

Locally, `just do-build` / `do-up` / `do-logs` / `do-down` drive the same
stack. It builds as `agent-hub-do:latest` so it does not overwrite the
full-stack local image.

### Public ingress: Caddy for devices, Cloudflare Tunnel for the dashboard

`docker-compose.public.yml` puts the droplet behind TLS and stops publishing
the app ports entirely. It splits the two kinds of traffic because they want
opposite things — the same split the one-app-per-port separation already
encodes:

```
devices    hub.<domain>    -> Caddy :443 -> 8000 (wss, image), 8003 (checkin)
dashboard  admin.<domain>  -> Cloudflare Tunnel -> 8001, behind Access
```

**Devices go direct.** Voice is latency-critical — ASR alone runs at RTF 0.73
on one vCPU — so an extra edge hop is expensive, and vision over a tunnel is
already the flakiest part of the pipeline. Devices also cannot complete an
interactive Access login. They do not need to: they present the enrollment
token, then a per-device WebSocket token, plus the image token for camera
uploads.

**The dashboard goes through the tunnel.** It is latency-insensitive and the
highest-value target in the system — it can change personas, drive device
tools, and read every transcript and photo. Through a tunnel it has no public
port at all, and Cloudflare Access puts real SSO in front of it, replacing
Basic auth. The public overlay explicitly clears the app-level dashboard
password, so operators see only the Access login rather than a second shared-
password popup. Put a default-deny Access policy on the entire dashboard
hostname, allow only the intended accounts or email addresses, and do not add
an `Everyone` or `Bypass` policy. Without an Access policy the tunnel has
simply published your dashboard.

The Caddyfile returns 404 for anything it does not route, so `/dashboard/` on
the device hostname finds nothing even though the app is listening on 8001
inside the compose network.

Set in `.env.do`:

```sh
AGENT_HUB_PUBLIC_HOST=hub.example.com       # devices
AGENT_HUB_DASHBOARD_HOST=admin.example.com  # dashboard, via the tunnel
CLOUDFLARE_TUNNEL_TOKEN=...                 # from the Cloudflare dashboard
CLOUDFLARE_ACCESS_TEAM_DOMAIN=example.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUDIENCE=...              # Access application AUD tag
ACME_EMAIL=you@example.com
```

The two Access identity values are optional but recommended. When both are
set, Agent Hub verifies Cloudflare's signed assertion on every dashboard
request, displays the authenticated operator, and provides an Access logout
link. Find the team domain in the Access login URL and the AUD tag under
Access controls → Applications → your dashboard application. Configure both
or neither; a partial configuration stops dashboard startup rather than
silently trusting an incomplete identity setup.

`AGENT_HUB_PUBLIC_HOST` is what selects this mode — cloud-init detects it,
adds the overlay, and opens only 80 and 443 in ufw instead of the app ports.
Port 80 is needed for the ACME http-01 challenge; the tunnel needs no inbound
port.

Create the tunnel under Zero Trust → Networks → Tunnels, add a public hostname
routing your dashboard host to `http://agent-hub:8001`, and paste the token
into `.env.do`. Then `just public-up`, or let cloud-init do it on a fresh
droplet.

Certificates live in `./data/caddy`. Keep that directory across restarts or
Caddy re-issues every time and will hit Let's Encrypt rate limits.

### Before pointing devices at it

The droplet's ports are open to the internet, not a LAN, so the LAN-first
assumptions above do not hold:

1. `AGENT_HUB_SERVER_ENROLLMENT_TOKEN` is generated for you by cloud-init —
   configure each device with it. If you provisioned by hand, set it, or
   anyone who finds `8003` can register a device.
2. Set `AGENT_HUB_SERVER_IMAGE_TOKEN` before enabling camera tools.
3. Put the droplet behind the HTTPS proxy pattern above, or restrict the
   ports with a DO cloud firewall. The dashboard is Basic auth over plain
   HTTP until you do — see [Why Basic auth, and its
   limits](#why-basic-auth-and-its-limits).
4. Cap spend at the LLM provider **and** set `llm.spend` limits — see below.
   An exposed hub with a working API key bills whoever finds it.

## LLM Spend

Every LLM call is metered: model, tokens, cost, and the device it came from.
Two independent caps are checked before each request, so a cap can be exceeded
by at most one call and no billable request goes out once one is reached.

```yaml
llm:
  spend:
    daily_limit_usd: 5.0     # 0 disables this cap
    total_limit_usd: 50.0    # 0 disables this cap
    warn_at: 0.8             # warn from 80% of either cap
    limit_message: ""        # what the device says when blocked
```

At `warn_at` the hub logs a warning once per window. At the cap it refuses
further calls and the device *speaks* a notice rather than going silent —
a dropped turn is indistinguishable from a device or network fault, which is
the worst way to discover you hit a limit mid-demo.

Current spend is at `/dashboard/spend.json`, with a per-model breakdown for
the day.

**This is a backstop, not the real limit.** It only counts what it sees: a
crash between the API call and the ledger write loses that call. Keep a hard
spend cap at the provider — on OpenRouter, a credit limit on the key.

### Where cost figures come from

OpenRouter reports the real charge when asked for it, and the hub asks. Other
OpenAI-compatible endpoints do not, so cost falls back to a local price table
and is flagged as estimated — the dashboard distinguishes the two rather than
presenting a guess as billing truth.

```yaml
llm:
  spend:
    pricing:                       # USD per 1M tokens
      "some/model":
        input: 0.10
        output: 0.20
```

An unpriced model still records its tokens, just with zero cost — so tokens
are always trustworthy even when dollars are not.

Usage-reporting request fields go only to endpoints known to accept them
(OpenRouter, api.openai.com). Local servers like Ollama reject unknown fields,
so they are metered by token count and the price table instead. Note that
streamed responses carry no usage block unless it is explicitly requested,
which is why streaming spend would otherwise silently record as zero.

## Current Hardening

The app currently includes:

- Optional HTTP Basic auth for `/dashboard/*`.
- Optional enrollment-token auth for `/checkin/` and `/xiaozhi/ota/`.
- Per-device WebSocket bearer tokens issued after authenticated check-in.
- Bearer-token auth for `/xiaozhi/v1/image/` when `server.image_token` is set.
- Dashboard image serving restricted to `server.dashboard_image_root`.
- Transcript HTML escaping in dashboard history.
- Origin checking on dashboard state changes (CSRF defence), active whether
  or not a dashboard password is set.
- Per-port route isolation: the dashboard is not mounted on the device-facing
  ports, so opening them to the LAN does not expose it.
- Host allowlisting on the dashboard when `server.allowed_hosts` is set,
  which blocks DNS rebinding.
- A startup warning when the dashboard is bound off-loopback with neither
  Basic auth nor verified Cloudflare Access identity.

### Why Basic auth, and its limits

Basic is used only for `/dashboard/*` — human operators. Devices authenticate
with bearer tokens instead, because ESP32 firmware cannot do an interactive
login. Basic was chosen for the dashboard because it needs no session store,
no login page, and no build step, and it keeps `curl` and `scripts/` working.

Its weaknesses are worth knowing:

- Browsers replay Basic credentials automatically, and there is no cookie to
  carry a `SameSite` flag. That is why the Origin check above exists — without
  it, any page an authenticated operator visits could POST to `/reboot`,
  `/speak`, or `/inject`.
- There is no logout. Browsers hold the credentials until they are closed,
  which matters for a shared or handed-around demo machine.
- The password is compared in cleartext, so it lives unhashed in the config
  file or environment. Prefer the environment over `data/.config.yaml`.

For anything internet-facing, do not rely on Basic as the primary control.
Put an identity-aware proxy in front. The public-ingress overlay uses
Cloudflare Access as the sole human-authentication layer: its tunnel leaves no
direct dashboard origin, and it provides per-person audit and revocation
without application changes. Basic remains available for plain-IP, LAN, and
other deployments that do not have an identity-aware proxy. WebAuthn cannot
be implemented in the app as it stands: the server is HTTP-only, and passkeys
require a secure context and a stable origin.

## Remaining Work Before Public Internet

Before treating this as production internet-facing software, add:

- Per-device enrollment tokens instead of one shared fleet enrollment secret.
- Persistent audit logging for dashboard actions.
- Rate limiting in the app or proxy.
- A firewall or compose override that binds raw app ports to localhost when
  all access goes through a local reverse proxy.
- Regular backups for `data/registry.db`, transcripts, and captured images.
