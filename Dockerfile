FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY scripts/ scripts/

# --extra full because the default ASR is SenseVoice. Its ONNX provider needs
# torch despite what its metadata says (see pyproject.toml), so this image is
# 1.1GB. Dockerfile.do is the torch-free build: Moonshine ASR instead, 532MB.
RUN uv sync --frozen --no-dev --extra full \
    && mkdir -p models/SenseVoiceSmall-onnx \
    && uv run python scripts/copy_silero.py \
    && uv run python scripts/download_models.py

EXPOSE 8000 8001 8003

# --no-sync: the environment is already built above. Without it, every
# container start re-resolves the project and re-fetches the KittenTTS wheel
# from GitHub, so startup fails whenever the network or GitHub is unavailable.
CMD ["uv", "run", "--no-sync", "python", "-m", "agent_hub.server"]
