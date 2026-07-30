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
  git clone https://github.com/ricklon/agent-hub.git "$APP_DIR"
fi
cd "$APP_DIR"
mkdir -p data

# Generate the deployment env on first boot. The dashboard controls every
# device and this droplet has a public IP, so never ship the template's
# placeholder password — generate a real one and record it for the operator.
if [ ! -f .env.do ]; then
  PUBLIC_IP=$(curl -fsS --max-time 10 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || echo "")
  DASHBOARD_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)

  cp .env.do.example .env.do
  sed -i "s|^AGENT_HUB_SERVER_DASHBOARD_PASSWORD=.*|AGENT_HUB_SERVER_DASHBOARD_PASSWORD=${DASHBOARD_PASSWORD}|" .env.do
  if [ -n "$PUBLIC_IP" ]; then
    sed -i "s|^AGENT_HUB_SERVER_ALLOWED_HOSTS=.*|AGENT_HUB_SERVER_ALLOWED_HOSTS=${PUBLIC_IP},localhost,127.0.0.1|" .env.do
  fi

  umask 077
  cat > "$CREDS_FILE" <<EOF
agent-hub dashboard
  URL:      http://${PUBLIC_IP:-<droplet-ip>}:8001/dashboard/
  username: admin
  password: ${DASHBOARD_PASSWORD}

The LLM API key is NOT set yet. Add it to ${APP_DIR}/.env.do:
  AGENT_HUB_LLM_OPENAI_API_KEY=sk-or-...
then: cd ${APP_DIR} && docker compose -f docker-compose.yml -f docker-compose.do.yml restart
EOF
  chmod 600 "$CREDS_FILE"
fi

# Build and start
docker compose -f docker-compose.yml -f docker-compose.do.yml build
docker compose -f docker-compose.yml -f docker-compose.do.yml up -d

echo "agent-hub is starting. Credentials and next steps: $CREDS_FILE"
cat "$CREDS_FILE"
