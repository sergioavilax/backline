# Backline API / init image. Build context: repo root.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer — cached until the lockfile changes.
# The `embed` extra (sentence-transformers) is deliberately NOT installed: PyPI's
# linux torch wheels are CUDA builds (~5 GB of image for a CPU-only stack), so the
# init job runs `rag.embed --best-effort` (chunks + FTS always work) and full hybrid
# embeddings build via host-side `make embed` — see docs/DECISIONS.md D-011. The
# CPU-wheel re-lock is staged, commented out, in pyproject.toml (build sandboxes
# cannot reach download.pytorch.org). To bake real models in, on a network that
# reaches it: uncomment the pytorch-cpu index block there, run `uv lock`, and add
# `--extra embed` to both `uv sync` lines below.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Project code.
COPY backline ./backline
COPY datagen ./datagen
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "backline.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
