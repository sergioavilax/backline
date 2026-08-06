# Backline — developer entry points. `make help` lists targets.
.DEFAULT_GOAL := help
.PHONY: help up down logs ps test lint typecheck fmt doctor seed emit-period embed retrieval-probe eval-smoke eval-suite corpus-tokens api dev-ui e2e openapi

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

up: ## Build and start the full stack (db, init/migrations, api, ui)
	docker compose up -d --build
	docker compose ps

down: ## Stop the stack (keeps the db volume)
	docker compose down

logs: ## Tail service logs
	docker compose logs -f

ps: ## Show service status
	docker compose ps

test: ## Run the Python test suite (Postgres tests skip unless DATABASE_URL is set)
	uv run pytest

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

typecheck: ## mypy (strict)
	uv run mypy

fmt: ## Auto-format Python
	uv run ruff format .
	uv run ruff check --fix .

doctor: ## Verify local environment (docker, ports, env, WSL/line endings)
	python3 scripts/doctor.py

seed: ## Build the deterministic Foldback Records world (DB + /data), < 3 min
	uv run python -m datagen seed

emit-period: ## Drop a new statement month into data/inbox, e.g. make emit-period PERIOD=2026-07
	uv run python -m datagen emit-period $(PERIOD)

corpus-tokens: ## Print corpus token counts (the scale-claim evidence)
	uv run python -m datagen corpus-tokens

embed: ## Build clause chunks + embeddings (idempotent; EMBED_MODEL=hash for offline)
	uv run python -m backline.rag.embed

retrieval-probe: ## Measure retrieval quality (recall@k/MRR, rerank on vs off)
	uv run python -m evals.retrieval_probe

eval-smoke: ## Keyless MockProvider eval plumbing test (seeds --if-empty; gates vs baseline)
	uv run python -m evals smoke

eval-suite: ## Regenerate evals/suites/core.json from the answer key (--check in CI)
	uv run python -m evals generate

api: ## Run the API locally (uvicorn, reload) against DATABASE_URL
	uv run uvicorn backline.api.main:app --port 8000 --reload

dev-ui: ## Run the Next.js dev server (expects the API on :8000)
	cd ui && pnpm dev

e2e: ## Playwright smoke against an already-running stack (API :8000 + UI :3000)
	cd ui && pnpm exec playwright test

openapi: ## Regenerate the committed OpenAPI schema (docs/api/openapi.json)
	uv run python scripts/export_openapi.py
