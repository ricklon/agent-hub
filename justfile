default:
    @just --list

install:
    uv sync --all-extras

download-models:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p models/SenseVoiceSmall
    echo "Copying Silero VAD ONNX from installed package..."
    uv run python scripts/copy_silero.py
    echo "Downloading SenseVoiceSmall ONNX from HuggingFace..."
    uv run python scripts/download_models.py
    echo "Models ready."

lint:
    uv run --extra dev ruff check src/ tests/ && uv run --extra dev ruff format --check src/ tests/

format:
    ruff format src/ tests/

typecheck:
    uv run --extra dev mypy --strict src/agent_hub/

test:
    uv run --extra dev pytest -xvs

smoke:
    uv run python scripts/smoke.py

# Drive every feature end-to-end against a live device (server must be running)
test-features:
    uv run python scripts/test_features.py

test-watch:
    pytest-watch

run:
    uv run python -m agent_hub.server

dashboard:
    uv run python -m agent_hub.dashboard.app

docker-build:
    docker compose build

docker-up:
    docker compose up

# Public device protocol through Funnel; private dashboard through Serve.
tailnet-up:
    docker compose -f docker-compose.yml -f docker-compose.tailnet.yml up -d

tailnet-status:
    docker compose -f docker-compose.yml -f docker-compose.tailnet.yml exec tailscale tailscale status
    docker compose -f docker-compose.yml -f docker-compose.tailnet.yml exec tailscale tailscale serve status

tailnet-down:
    docker compose -f docker-compose.yml -f docker-compose.tailnet.yml down

# DigitalOcean stack: Moonshine ASR + Edge TTS, no torch (390MB image).
do-build:
    docker compose -f docker-compose.yml -f docker-compose.do.yml build

do-up:
    docker compose -f docker-compose.yml -f docker-compose.do.yml up -d

do-logs:
    docker compose -f docker-compose.yml -f docker-compose.do.yml logs -f

do-down:
    docker compose -f docker-compose.yml -f docker-compose.do.yml down

# Same stack behind public ingress: Caddy for devices, Cloudflare Tunnel for
# the dashboard. Needs AGENT_HUB_PUBLIC_HOST, AGENT_HUB_DASHBOARD_HOST and
# CLOUDFLARE_TUNNEL_TOKEN in .env.do.
# --env-file is required, not cosmetic: env_file: only injects into the
# container, while ${VAR} interpolation in the compose files themselves reads
# the default .env. Without it the hostnames resolve empty and compose aborts.
public-up:
    docker compose --env-file .env.do -f docker-compose.yml -f docker-compose.do.yml -f docker-compose.public.yml up -d

public-logs:
    docker compose --env-file .env.do -f docker-compose.yml -f docker-compose.do.yml -f docker-compose.public.yml logs -f

public-down:
    docker compose --env-file .env.do -f docker-compose.yml -f docker-compose.do.yml -f docker-compose.public.yml down

deploy-edge:
    ansible-playbook deploy-agent-hub.yml

deploy-fubar:
    docker compose -f docker-compose.yml -f docker-compose.fubar.yml up
