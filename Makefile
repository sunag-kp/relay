SHELL := /bin/bash
.DEFAULT_GOAL := help

UV ?= uv
COMPOSE ?= docker compose
INFRA := postgres redis

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install Python deps (incl. dev) into .venv
	$(UV) sync --all-groups

.PHONY: up
up: ## Build and start the full stack (3 gateway replicas behind nginx, infra, observability)
	$(COMPOSE) up --build -d --wait
	@echo "Gateway (nginx): http://localhost:8080   API docs: http://localhost:8080/docs"

.PHONY: down
down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v --remove-orphans

.PHONY: infra
infra: ## Start only the infrastructure the integration tests need
	$(COMPOSE) up -d --wait $(INFRA)

.PHONY: migrate
migrate: ## Apply database migrations against DATABASE_URL
	$(UV) run alembic upgrade head

.PHONY: lint
lint: ## Ruff lint + format check, mypy (strict), architecture contracts
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	$(UV) run lint-imports

.PHONY: fmt
fmt: ## Auto-format and fix lint
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

TEST_DATABASE_URL ?= postgresql+asyncpg://relay:relay@localhost:5432/relay_test

.PHONY: test
test: ## Unit + integration tests with coverage (starts infra; uses the relay_test database)
	$(MAKE) infra
	DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run alembic upgrade head
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) $(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

.PHONY: test-unit
test-unit: ## Fast unit tests only (no Docker needed)
	$(UV) run pytest -m "not integration"

.PHONY: logs
logs: ## Tail gateway logs
	$(COMPOSE) logs -f gateway
