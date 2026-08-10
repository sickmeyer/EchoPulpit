# Local development / manual testing image only -- NOT the production deploy
# path. Production runs on an ephemeral EC2 spot instance built from a
# custom AMI (see deploy/packer/echopulpit-worker.pkr.hcl), not this container.
FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# --- system deps ---
# - python3.11 + pip
# - ffmpeg for audio conversion
# - build tools for llama-cpp-python (may compile)
# - curl/ca-certificates for NodeSource
RUN apt-get update && apt-get install -y --no-install-recommends \
      software-properties-common \
      curl \
      ca-certificates \
      ffmpeg \
      git \
      build-essential \
      cmake \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
      python3.11 \
      python3.11-venv \
      python3.11-distutils \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11 \
    && rm -rf /var/lib/apt/lists/*

# --- Node.js (for yt-dlp JS runtime) ---
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python3.11 -m pip install -r /app/requirements.txt

# ---- Pre-download faster-whisper model during build (offline runtime) ----
# Usage: docker build --build-arg WHISPER_MODEL=medium ...
ARG WHISPER_MODEL=medium
ENV WHISPER_MODEL=${WHISPER_MODEL}

RUN python3.11 - <<'PY'
import os
from faster_whisper import WhisperModel

model_name = os.environ.get("WHISPER_MODEL", "medium")
print(f"Pre-downloading faster-whisper model: {model_name}")
# This forces download into the image. CPU init is fine; it just populates cache.
WhisperModel(model_name, device="cpu", compute_type="int8")
print("Done pre-downloading.")
PY

COPY . /app

ENV CONFIG_PATH=/app/config.yaml

CMD ["python3.11", "/app/sermon_pipeline.py"]