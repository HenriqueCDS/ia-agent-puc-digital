# syntax=docker/dockerfile:1.7
#
# Imagem ÚNICA para todos os processos do projeto (mesmo modelo de embeddings em
# todos, o que é obrigatório — ver app/core/config.embedding_model):
#
#   API HTTP (CMD padrao):   docker run -p 8000:8000 --env-file .env ia-agent
#   Ingestao dos arquivos:   docker run --env-file .env -v "$PWD/data/raw:/app/data/raw" ia-agent \
#                                python -m scripts.ingest canvas puc-digital email_modelos -v
#   Re-crawl da allowlist:   docker run --env-file .env ia-agent python -m scripts.crawl --prune --verbose
#   Pergunta pela CLI:       docker run --env-file .env ia-agent python -m scripts.ask "Como envio uma atividade?"
#
# O modelo de embeddings (~1GB) e baixado NO BUILD e a imagem roda offline
# (HF_HUB_OFFLINE=1): sem isso todo cold start baixava 1GB do HuggingFace e
# dependia de rede externa + HF_TOKEN em runtime.

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------------
# Stage 1 — builder: dependencias num venv + download do modelo de embeddings
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential cobre pacotes sem wheel pronto para esta plataforma
# (ex.: em arm64 alguns caem para sdist). psycopg[binary] ja vem compilado.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Torch CPU-only: evita arrastar ~2GB de libs CUDA que nao servem para inferencia
# de embeddings em CPU (Fargate nao tem GPU).
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2,<3"

COPY requirements.txt .
RUN pip install -r requirements.txt

# Baixa os pesos do modelo de embeddings para o cache do HF, dentro da imagem.
# ARG para trocar sem editar o Dockerfile; tem que bater com EMBEDDING_MODEL.
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-base
ENV HF_HOME=/opt/hf-cache
RUN python -c "from sentence_transformers import SentenceTransformer as M; M('${EMBEDDING_MODEL}')"

# (Opcional) reranker cross-encoder — so descomente se for subir com
# RERANKER_ENABLED=true (ver app/core/config.reranker_model):
# ARG RERANKER_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1
# RUN python -c "from sentence_transformers import CrossEncoder as C; C('${RERANKER_MODEL}')"

# --------------------------------------------------------------------------
# Stage 2 — runtime
# --------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# libgomp1: runtime do OpenMP que o torch/sentence-transformers linka.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=app:app /opt/hf-cache /opt/hf-cache

WORKDIR /app
COPY --chown=app:app app/ ./app/
COPY --chown=app:app scripts/ ./scripts/

USER app
EXPOSE 8000

# Liveness barato (nao toca o banco); readiness real e /v1/ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/health').status==200 else 1)"

# 1 worker de proposito: o rate limit e o teto diario sao em memoria por
# processo (ver app/api/ratelimit.py). Escale por replicas + REDIS_URL, nunca
# subindo --workers. O warm-up (~65s p/ carregar o modelo) roda no lifespan.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
