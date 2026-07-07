FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY scripts/ scripts/

RUN uv sync --frozen --no-dev \
    && mkdir -p models/SenseVoiceSmall \
    && uv run python scripts/copy_silero.py \
    && uv run python scripts/download_models.py

EXPOSE 8000 8001 8003

CMD ["uv", "run", "python", "-m", "agent_hub.server"]
