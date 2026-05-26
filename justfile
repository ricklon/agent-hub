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
    echo "Downloading SenseVoiceSmall via FunASR (first run downloads from HuggingFace)..."
    uv run python scripts/download_models.py
    echo "Models ready."

lint:
    ruff check src/ tests/ && ruff format --check src/ tests/

format:
    ruff format src/ tests/

typecheck:
    mypy --strict src/agent_hub/

test:
    pytest -xvs

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

deploy-edge:
    ansible-playbook deploy-agent-hub.yml

deploy-fubar:
    docker compose -f docker-compose.yml -f docker-compose.fubar.yml up
