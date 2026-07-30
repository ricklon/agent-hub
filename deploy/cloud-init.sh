#!/bin/bash
# Provisioning script for a fresh DigitalOcean droplet, run as user data.
#
# Installs Docker, clones agent-hub, and starts the Moonshine/Edge stack from
# docker-compose.do.yml. Runs as root on first boot only.
#
# Usage in the DO dashboard: Create droplet -> User data -> paste this file.
# Or via doctl:
#   doctl compute droplet create agent-hub \
#     --image docker-20-04 --size s-1vcpu-2gb --region nyc1 \
#     --user-data-file deploy/cloud-init.sh
#
# Use at least 2GB RAM: the server only needs ~250MB, but building the image
# on the droplet needs more. First boot takes ~5 minutes.
#
# When it finishes, the generated dashboard password is in
# /root/agent-hub-credentials.txt and the server is on the droplet's IP:8001.

set -euo pipefail

APP_DIR=/opt/agent-hub
CREDS_FILE=/root/agent-hub-credentials.txt
# Branch or tag to deploy. Edit before pasting to test a branch that has not
# merged yet — the compose and Dockerfile this script uses must exist on it.
AGENT_HUB_REF="${AGENT_HUB_REF:-main}"

# Install Docker if not present
if ! command -v docker &>/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi

# Clone the repo
if [ ! -d "$APP_DIR" ]; then
  git clone --branch "$AGENT_HUB_REF" https://github.com/ricklon/agent-hub.git "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p data

# Fail early with a clear reason rather than a confusing compose error.
for required in Dockerfile.do docker-compose.do.yml .env.do.example; do
  if [ ! -f "$required" ]; then
    echo "FATAL: $required missing on ref '$AGENT_HUB_REF' — deploy a ref that has it." >&2
    exit 1
  fi
done

# Generate the deployment env on first boot. This droplet has a public IP, so
# neither of the template's open defaults is safe here: the dashboard controls
# every device, and an empty enrollment token lets anyone who finds port 8003
# register a device. Generate both and record them for the operator.
if [ ! -f .env.do ]; then
  PUBLIC_IP=$(curl -fsS --max-time 10 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || echo "")
  DASHBOARD_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
  ENROLLMENT_TOKEN=$(openssl rand -hex 24)

  cp .env.do.example .env.do
  # Holds the dashboard password, enrollment token, and later the LLM API key.
  chmod 600 .env.do
  sed -i "s|^AGENT_HUB_SERVER_DASHBOARD_PASSWORD=.*|AGENT_HUB_SERVER_DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}|" .env.do
  sed -i "s|^AGENT_HUB_SERVER_ENROLLMENT_TOKEN=.*|AGENT_HUB_SERVER_ENROLLMENT_TOKEN=${ENROLLMENT_TOKEN}|" .env.do
  if [ -n "$PUBLIC_IP" ]; then
    sed -i "s|^AGENT_HUB_SERVER_ALLOWED_HOSTS=.*|AGENT_HUB_SERVER_ALLOWED_HOSTS=${PUBLIC_IP},localhost,127.0.0.1|" .env.do
  fi

  # Fail loudly rather than booting with a placeholder still in place — a
  # silently unmatched sed would put `changeme` on a public IP.
  if grep -q "changeme" .env.do || grep -qE "^AGENT_HUB_SERVER_ENROLLMENT_TOKEN=$" .env.do; then
    echo "FATAL: .env.do still contains placeholder values; refusing to start." >&2
    exit 1
  fi

  umask 077
  cat > "$CREDS_FILE" <<EOF
agent-hub dashboard
  URL:      http://${PUBLIC_IP:-<droplet-ip>}:8001/dashboard/
  username: admin
  password: ${DASHBOARD_PASSWORD}

Enrollment token (devices must send this to check in):
  ${ENROLLMENT_TOKEN}

Devices send it as one of:
  X-Agent-Hub-Enrollment-Token: <token>
  Authorization: Bearer <token>
  /xiaozhi/ota/?enrollment_token=<token>

To allow a device to enroll without the token, clear
AGENT_HUB_SERVER_ENROLLMENT_TOKEN in ${APP_DIR}/.env.do and restart. Only do
that if the droplet's ports are firewalled off from the public internet.

The LLM API key is NOT set yet. Add it to ${APP_DIR}/.env.do:
  AGENT_HUB_LLM_OPENAI_API_KEY=sk-or-...
then: cd ${APP_DIR} && docker compose -f docker-compose.yml -f docker-compose.do.yml restart
EOF
  chmod 600 "$CREDS_FILE"
fi

# Setting AGENT_HUB_PUBLIC_HOST in .env.do selects the public ingress overlay:
# Caddy terminates TLS for devices and a Cloudflare Tunnel carries the
# dashboard, so the app ports are not published on the host at all.
COMPOSE_FILES=(-f docker-compose.yml -f docker-compose.do.yml)
PUBLIC_INGRESS=false
if grep -qE "^AGENT_HUB_PUBLIC_HOST=.+" .env.do; then
  COMPOSE_FILES+=(-f docker-compose.public.yml)
  PUBLIC_INGRESS=true
fi

# DigitalOcean's Docker image boots with ufw active, allowing only 22, 2375
# and 2376. Without this the stack starts and binds correctly but every
# external request is dropped, which looks exactly like a crashed container.
if command -v ufw &>/dev/null && ufw status | grep -q "Status: active"; then
  if [ "$PUBLIC_INGRESS" = true ]; then
    # Only the TLS front door. 80 is required for the ACME http-01 challenge,
    # and the tunnel needs no inbound port at all.
    ufw allow 80/tcp comment "agent-hub ACME http-01"
    ufw allow 443/tcp comment "agent-hub TLS"
  else
    ufw allow 8000/tcp comment "agent-hub device WebSocket"
    ufw allow 8001/tcp comment "agent-hub dashboard"
    ufw allow 8003/tcp comment "agent-hub device check-in"
  fi
  ufw reload
fi

# Build and start
docker compose "${COMPOSE_FILES[@]}" build
docker compose "${COMPOSE_FILES[@]}" up -d

echo "agent-hub is starting. Credentials and next steps: $CREDS_FILE"
if [ "$PUBLIC_INGRESS" = true ]; then
  echo "Public ingress enabled — devices via Caddy, dashboard via Cloudflare Tunnel."
fi
cat "$CREDS_FILE"
