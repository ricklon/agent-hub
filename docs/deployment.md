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
- A startup warning when the dashboard is bound off-loopback with no password.

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
Put an identity-aware proxy in front — Cloudflare Access supports passkeys,
SSO, per-person audit, and revocation with no application changes — and keep
Basic underneath as a backstop. WebAuthn cannot be implemented in the app as
it stands: the server is HTTP-only, and passkeys require a secure context and
a stable origin.

## Remaining Work Before Public Internet

Before treating this as production internet-facing software, add:

- Per-device enrollment tokens instead of one shared fleet enrollment secret.
- Persistent audit logging for dashboard actions.
- Rate limiting in the app or proxy.
- A firewall or compose override that binds raw app ports to localhost when
  all access goes through a local reverse proxy.
- Regular backups for `data/registry.db`, transcripts, and captured images.
