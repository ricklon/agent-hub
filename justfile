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

deploy-edge:
    ansible-playbook deploy-agent-hub.yml

deploy-fubar:
    docker compose -f docker-compose.yml -f docker-compose.fubar.yml up
