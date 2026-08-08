# Backline AWS deploy image. Build context: repo root, BuildKit required
# (docker/aws.Dockerfile.dockerignore re-admits data/ — see V3).
#   docker build -f docker/aws.Dockerfile -t backline-aws:latest .
# Extends the api image contract with: evals/ (V1), baked HF model weights (V2),
# baked deterministic /data (V3), pre-created trace/eval dirs.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra embed

COPY backline ./backline
COPY datagen ./datagen
COPY migrations ./migrations
COPY config ./config
COPY evals ./evals
RUN uv sync --frozen --no-dev --extra embed

# Bake the retrieval models so a cold Fargate task never touches Hugging Face (V2).
RUN uv run --no-sync python -c "\
from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('BAAI/bge-small-en-v1.5'); \
CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')"

# Bake the deterministic world files: inbox drops (Reconciler runtime input),
# contract PDFs/txt (provenance), writable dirs for traces/eval artifacts (V3, V12).
COPY data /data
RUN mkdir -p /data/traces /data/evals

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "backline.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
