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

6. Set an image token before using camera/image tools outside a private lab:

```sh
AGENT_HUB_SERVER_IMAGE_TOKEN=change-this-long-random-token
```

## Port Map

| Port | Endpoint | Intended callers | Exposure |
| --- | --- | --- | --- |
| `8000` | `/xiaozhi/v1/` and image explain | ESP32 devices | LAN/Tailscale only |
| `8001` | `/dashboard/` | Human operators | Tailscale or authenticated HTTPS only |
| `8003` | `/checkin/`, `/xiaozhi/ota/` | ESP32 devices | LAN/Tailscale only |

Do not publish these ports directly on a cloud VM without a firewall or
reverse proxy.

## Tailscale Pattern

Use this when the server is at home, in a classroom, or in a makerspace.

1. Install Tailscale on the Docker host.
2. Keep `docker-compose.yml` as-is for LAN access.
3. Use host firewall rules to limit `8001` to the Tailscale interface if the
   LAN is not fully trusted.
4. Browse to `http://<tailscale-hostname>:8001/dashboard/`.
5. Keep device check-in URLs on LAN IPs unless the devices themselves can
   reach the tailnet.

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
```

## Current Hardening

The app currently includes:

- Optional HTTP Basic auth for `/dashboard/*`.
- Optional enrollment-token auth for `/checkin/` and `/xiaozhi/ota/`.
- Per-device WebSocket bearer tokens issued after authenticated check-in.
- Bearer-token auth for `/xiaozhi/v1/image/` when `server.image_token` is set.
- Dashboard image serving restricted to `server.dashboard_image_root`.
- Transcript HTML escaping in dashboard history.

## Remaining Work Before Public Internet

Before treating this as production internet-facing software, add:

- Per-device enrollment tokens instead of one shared fleet enrollment secret.
- Persistent audit logging for dashboard actions.
- Rate limiting in the app or proxy.
- A firewall or compose override that binds raw app ports to localhost when
  all access goes through a local reverse proxy.
- Regular backups for `data/registry.db`, transcripts, and captured images.
