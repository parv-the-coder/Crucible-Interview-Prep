# Crucible — common tasks.
#   make help    lists everything

COMPOSE := docker compose -f infra/docker-compose.yml
PY      := .venv/bin/python
VENV    := .venv/bin

.DEFAULT_GOAL := help
.PHONY: help up down restart logs ps api worker migrate revision seed reseed \
        test test-unit test-integration test-sandbox cov lint fmt check \
        images reap clean

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
	@echo "--- sandbox containers ---"
	@docker ps --filter label=crucible.sandbox=1 --format 'table {{.Names}}\t{{.Status}}' | tail -n +1

logs:  ## Tail datastore logs
	$(COMPOSE) logs -f --tail=50

# ------------------------------------------------------------------ services --

api:  ## Run the API (reloads on change)
	cd backend && PYTHONPATH=. ../$(VENV)/uvicorn crucible.main:app --reload --port 8000

worker:  ## Run a queue worker (run it more than once for more capacity)
	cd backend && PYTHONPATH=. ../$(VENV)/python -m crucible.workers.runner

# ------------------------------------------------------------------ database --

migrate:  ## Apply migrations
	cd backend && PYTHONPATH=. ../$(VENV)/alembic upgrade head

revision:  ## Generate a migration: make revision m="add x"
	cd backend && PYTHONPATH=. ../$(VENV)/alembic revision --autogenerate -m "$(m)"

seed:  ## Load demo users and the question bank
	cd backend && PYTHONPATH=. ../$(PY) -m crucible.scripts.seed

reseed:  ## Wipe and reload all data
	cd backend && PYTHONPATH=. ../$(PY) -m crucible.scripts.seed --reset

# --------------------------------------------------------------------- tests --

test: test-unit test-integration  ## Unit + integration

test-unit:  ## Fast tests, no external dependencies
	cd backend && PYTHONPATH=. ../$(VENV)/pytest tests/unit -q

test-integration:  ## Needs Postgres
	cd backend && PYTHONPATH=. ../$(VENV)/pytest -m integration -q

test-sandbox:  ## Real attack cases against real containers (needs Docker)
	cd backend && PYTHONPATH=. ../$(VENV)/pytest -m sandbox -q

cov:  ## Coverage report
	cd backend && PYTHONPATH=. ../$(VENV)/pytest tests/unit --cov=crucible --cov-report=term-missing

# ---------------------------------------------------------------- code style --

lint:  ## Check formatting and lint
	cd backend && ../$(VENV)/ruff check crucible tests
	cd backend && ../$(VENV)/ruff format --check crucible tests

fmt:  ## Auto-fix formatting and lint
	cd backend && ../$(VENV)/ruff check --fix crucible tests
	cd backend && ../$(VENV)/ruff format crucible tests

check: lint test-unit  ## What CI runs

# ------------------------------------------------------------------- sandbox --

images:  ## Pre-pull the language images (do this once)
	docker pull python:3.12-alpine
	docker pull node:20-alpine
	docker pull gcc:13
	@echo "pulled. gcc is ~2GB; skip it with SANDBOX_ENABLED_LANGUAGES=python,javascript"

reap:  ## Destroy orphaned sandbox containers left by a crashed worker
	@docker ps -aq --filter label=crucible.sandbox=1 | xargs -r docker rm -f
	@echo "orphaned sandbox containers removed"

# ------------------------------------------------------------------- cleanup --

clean:  ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf backend/.pytest_cache backend/.ruff_cache backend/.mypy_cache backend/htmlcov
