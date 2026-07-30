# DigitalOcean-optimized Dockerfile — Moonshine ASR + Edge TTS, no torch.
#
# Everything that needs torch lives in the `full` extra, which this image
# skips: SenseVoice/FunASR ASR, and KittenTTS, which pulls torch in through
# misaki[en] -> spacy-curated-transformers. Moonshine runs on onnxruntime
# instead. Measured: 390MB image (vs ~4GB) and ~95MB idle RAM.
#
# Both provider registries import lazily, so selecting a provider that isn't
# installed fails at that provider's construction, not at startup.
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

# Bake the Moonshine model (~70MB) into the image so the first transcription
# doesn't stall on a download. models/ is deliberately NOT a bind mount in
# docker-compose.do.yml — mounting it would shadow these files.
RUN uv run python scripts/download_moonshine.py

EXPOSE 8000 8001 8003

CMD ["uv", "run", "python", "-m", "agent_hub.server"]
