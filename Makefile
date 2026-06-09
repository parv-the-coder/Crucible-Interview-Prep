# Crucible — common tasks.
#   make help    lists everything

COMPOSE := docker compose -f infra/docker-compose.yml
PY      := .venv/bin/python
VENV    := .venv/bin

.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps api migrate revision lint fmt clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------ infrastructure --

up:  ## Start Postgres
	$(COMPOSE) up -d
	@echo "waiting for healthy..."
	@until [ "$$($(COMPOSE) ps --format '{{.Health}}' | grep -c healthy)" = "1" ]; do sleep 1; done
	@$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}'

down:  ## Stop Postgres (data is kept)
	$(COMPOSE) down

restart: down up  ## Restart the datastores

ps:  ## What is running
	@$(COMPOSE) ps --format 'table {{.Name}}\t{{.Status}}'

logs:  ## Tail datastore logs
	$(COMPOSE) logs -f --tail=50

# ------------------------------------------------------------------ services --

api:  ## Run the API (reloads on change)
	cd backend && PYTHONPATH=. ../$(VENV)/uvicorn crucible.main:app --reload --port 8000

# ------------------------------------------------------------------ database --

migrate:  ## Apply migrations
	cd backend && PYTHONPATH=. ../$(VENV)/alembic upgrade head

revision:  ## Generate a migration: make revision m="add x"
	cd backend && PYTHONPATH=. ../$(VENV)/alembic revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------- code style --

lint:  ## Check formatting and lint
	cd backend && ../$(VENV)/ruff check crucible tests
	cd backend && ../$(VENV)/ruff format --check crucible tests

fmt:  ## Auto-fix formatting and lint
	cd backend && ../$(VENV)/ruff check --fix crucible tests
	cd backend && ../$(VENV)/ruff format crucible tests

# ------------------------------------------------------------------- cleanup --

clean:  ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache backend/htmlcov
