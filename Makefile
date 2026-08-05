# Backline — developer entry points. `make help` lists targets.
.DEFAULT_GOAL := help
.PHONY: help up down logs ps test lint typecheck fmt doctor seed embed eval-smoke corpus-tokens

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

# ── Phase-gated targets (stubs fail loudly until their phase ships) ──────────
seed: ## Build the synthetic world (Phase 1)
	@echo "make seed: not implemented yet — ships in Phase 1 (datagen)." >&2; exit 1

embed: ## Build contract-chunk embeddings (Phase 3)
	@echo "make embed: not implemented yet — ships in Phase 3 (RAG)." >&2; exit 1

eval-smoke: ## Keyless MockProvider eval plumbing test (Phase 5)
	@echo "make eval-smoke: not implemented yet — ships in Phase 5 (evals)." >&2; exit 1

corpus-tokens: ## Print corpus token count (Phase 1)
	@echo "make corpus-tokens: not implemented yet — ships in Phase 1 (datagen)." >&2; exit 1
