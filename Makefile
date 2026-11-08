.PHONY: help setup run css css-watch test lint fmt format migrate shell

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## One-time install: Python deps, Node deps (Tailwind)
	uv sync
	npm install
	uv run manage.py migrate
	npx tailwindcss -i static/css/input.css -o static/css/tailwind.css --minify

run: ## Start the dev server (http://127.0.0.1:8000)
	uv run manage.py runserver

css: ## Rebuild Tailwind CSS after editing class names
	npx tailwindcss -i static/css/input.css -o static/css/tailwind.css --minify

css-watch: ## Rebuild Tailwind CSS on change (run in another terminal)
	npx tailwindcss -i static/css/input.css -o static/css/tailwind.css --watch

test: ## Run the test suite
	uv run pytest

lint: ## Lint with ruff
	uv run ruff check .

fmt: ## Fix formatting and imports with ruff
	uv run ruff check --fix . && uv run ruff format .

migrate: ## Run pending migrations
	uv run manage.py migrate

shell: ## Django shell
	uv run manage.py shell

collectstatic: ## Collect static files for production
	uv run manage.py collectstatic --noinput
