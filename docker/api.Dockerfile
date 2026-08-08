# Backline API / init image. Build context: repo root.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer — cached until the lockfile changes.
# The `embed` extra (sentence-transformers) IS installed — the D-011 CPU re-lock
# landed, so the lockfile resolves torch from the pytorch-cpu index
# (download.pytorch.org/whl/cpu), not PyPI's ~5 GB CUDA builds. No model weights
# are baked, though: bge-small + the ms-marco cross-encoder download from
# Hugging Face on first use, and the init job's `rag.embed --best-effort` keeps
# a cold boot without model egress fully working (chunks + FTS-only search).
# See docs/DECISIONS.md D-011.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --extra embed

# Project code.
COPY backline ./backline
COPY datagen ./datagen
COPY migrations ./migrations
COPY config ./config
RUN uv sync --frozen --no-dev --extra embed

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "backline.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
