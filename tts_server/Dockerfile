FROM ghcr.io/astral-sh/uv:0.9.26 AS uv
FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/models/huggingface \
    VLLM_ATTENTION_BACKEND=TRITON_ATTN \
    VLLM_WORKER_MULTIPROC_METHOD=spawn \
    STEP_AUDIO_SOURCE=/opt/Step-Audio-EditX

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv build-essential git ca-certificates \
    ffmpeg libsndfile1 sox libsox-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

# Pin source and its uv.lock together: the vLLM API is not a stable REST API.
ARG STEP_AUDIO_REVISION=a652e87052c109e26f616d60971376ff47a829d4
RUN git init /opt/Step-Audio-EditX \
    && cd /opt/Step-Audio-EditX \
    && git remote add origin https://github.com/stepfun-ai/Step-Audio-EditX.git \
    && git fetch --depth 1 origin ${STEP_AUDIO_REVISION} \
    && git checkout --detach FETCH_HEAD
WORKDIR /opt/Step-Audio-EditX
RUN uv sync --frozen --no-dev \
    && uv pip install 'fastapi>=0.115.6,<1' 'uvicorn>=0.34,<1' 'python-multipart>=0.0.20,<1'
ENV PATH="/opt/Step-Audio-EditX/.venv/bin:$PATH" PYTHONPATH=/srv
COPY tts_server /srv/tts_server
EXPOSE 8006
CMD ["python", "-m", "uvicorn", "tts_server.server:app", "--host", "0.0.0.0", "--port", "8006", "--workers", "1"]
