# CapEx platform — thin command aliases (see README for the full runbook).
# Django lives in backend/; data stores come from docker compose (db/redis).

.PHONY: help setup run test lint fmt migrate seed worker css collectstatic

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## uv deps + tailwind tooling + local DB/redis via compose
	uv sync
	npm install
	docker compose up -d db redis

DB_ENV := POSTGRES_HOST=${POSTGRES_HOST:-localhost} POSTGRES_PORT=${POSTGRES_PORT:-15432} REDIS_URL=${REDIS_URL:-redis://localhost:16379}

run: ## Run the API/dev-server on :8000 (local venv, compose data stores)
	cd backend && $(DB_ENV) uv run manage.py runserver

test: ## Test suite (pytest; uses the compose data stores)
	cd backend && $(DB_ENV) uv run pytest ../backend/capex/tests $(PYTEST_ARGS)

lint: ## ruff
	cd backend && uv run ruff check .

fmt: ## ruff autofix + format
	cd backend && uv run ruff check --fix --unsafe-fixes . && uv run ruff format .

migrate: ## Apply migrations
	cd backend && $(DB_ENV) uv run manage.py migrate

seed: ## (Re)load the deterministic harness dataset (big tenant included)
	cd backend && $(DB_ENV) uv run manage.py load_seed --scale big

worker: ## Local q2 worker against the compose redis
	cd backend && $(DB_ENV) uv run manage.py qcluster

css: ## Rebuild tailwind (tokens/components in frontend/static/css)
	npx tailwindcss -i frontend/static/css/input.css -o frontend/static/css/tailwind.css --minify
