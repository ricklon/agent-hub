# DigitalOcean-optimized Dockerfile — Moonshine ASR + KittenTTS, no torch.
#
# Both run on onnxruntime, so speech stays local on the droplet. What this
# image skips is the `full` extra: SenseVoice/FunASR ASR and the silero-vad
# package, which need torch (~700MB). Measured: 690MB image (vs ~4GB).
#
# The ASR registry imports lazily, so selecting funasr here fails at that
# provider's construction, not at startup.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.do.yml build
#   docker compose -f docker-compose.yml -f docker-compose.do.yml up -d

FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libopus0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

COPY pyproject.toml uv.lock ./
COPY src/ src/
COPY scripts/ scripts/

# Install base deps only (no torch/funasr — Moonshine uses onnxruntime).
#
# The VAD runs the Silero ONNX graph through onnxruntime at runtime, so only
# the 2.3MB .onnx file is needed — not the silero-vad package, which pulls
# torch. Install it with --no-deps purely to source that file, pinned to the
# version in uv.lock, then discard the package.
RUN uv sync --frozen --no-dev \
    && uv pip install --no-deps silero-vad==6.2.1 \
    && uv run python scripts/copy_silero.py \
    && uv pip uninstall silero-vad

# Bake the speech models into the image so the first request doesn't stall on
# a download. models/ is deliberately NOT a bind mount in
# docker-compose.do.yml — mounting it would shadow the Moonshine/Silero files.
# KittenTTS caches under ~/.cache/huggingface, which is not mounted either.
RUN uv run python scripts/download_moonshine.py \
    && uv run python scripts/download_kittentts.py

EXPOSE 8000 8001 8003

# --no-sync: the environment is already built above. Without it, every
# container start re-resolves the project and re-fetches the KittenTTS wheel
# from GitHub, so a droplet reboot fails whenever GitHub is unreachable.
CMD ["uv", "run", "--no-sync", "python", "-m", "agent_hub.server"]
