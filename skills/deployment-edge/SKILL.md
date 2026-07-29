---
name: deployment-edge
description: Use when writing or modifying Agent Hub deployment playbooks, Docker Compose files, Tailscale or Cloudflare exposure, bind mounts, firewall rules, backups, or edge-host secret handling, including changes to docs/deployment.md.
---

# Deploy Agent Hub at the Edge

## Preserve the trust boundaries

- Keep `8000` (voice WebSocket), `8001` (dashboard), and `8003` (check-in/OTA) separate.
- Preserve both `/checkin/` and the firmware-compatible `/xiaozhi/ota/` alias.
- Expose the dashboard only to trusted operators. Prefer Tailscale Serve for tailnet-only HTTPS.
- Never use Tailscale Funnel unless the user explicitly asks for public-internet access.
- Keep device ports on the trusted LAN unless remote devices need them and have authentication configured.
- Require dashboard authentication and host allowlisting before exposing the dashboard beyond a trusted tailnet.

## Keep state recoverable

- Bind-mount `./data:/app/data`; Docker container replacement must not remove registry, transcript, or image data.
- Never edit or commit `data/.config.yaml`. Put documented placeholders in `.config.example.yaml` and secrets in the ignored `.env` or the deployment platform's secret store.
- Back up `data/registry.db`, transcripts, captured images, and any operator-managed configuration before destructive deployment changes.

## Choose the exposure pattern

1. Use a Tailscale sidecar when only the application should have a tailnet identity. Share its network namespace with Agent Hub, keep all raw app ports off the host, persist `/var/lib/tailscale`, and mount Serve configuration as a directory. For public ESP32 access, Funnel only the authenticated device paths on HTTPS 443 and keep the dashboard on a separate tailnet-only Serve port.
2. Use host-installed Tailscale plus `tailscale serve` only when the Docker host is already intentionally a tailnet node. Proxy only `127.0.0.1:8001` for the dashboard.
3. Use Funnel, a reverse proxy, or Cloudflare Tunnel only for an explicit public-internet requirement. Add HTTPS, enrollment and image tokens, rate limits, and audit logging first. Never expose the dashboard on the same Funnel port as device endpoints.
4. Use the FUBAR compose override for class-night LAN operation; do not add Tailscale assumptions to that override.

## Validate changes

- Run `docker compose config` for every edited compose combination.
- Run `just lint typecheck test` before a deployment handoff or remote push.
- Verify the dashboard with an HTTP GET, not HEAD (the dashboard route intentionally returns `405` to HEAD).
- Check `tailscale serve status` and confirm the output says it is available within the tailnet.
- Confirm Funnel is not enabled when the requested scope is tailnet-only.
