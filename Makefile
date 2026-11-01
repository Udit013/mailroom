-include .env
API_PORT ?= 8000

COMPOSE := docker compose
# A separate project, because the test suite truncates tables and purges queues.
TEST_COMPOSE := docker compose -p mailroom-test

.PHONY: up down migrate test lint seed demo

.env:
	@echo "Missing .env. Run: cp .env.example .env" >&2
	@exit 1

up: .env
	$(COMPOSE) up -d --build --wait api relay worker

down:
	$(COMPOSE) down

migrate: .env
	$(COMPOSE) run --rm --build migrate

# Pass pytest options with ARGS, for example: make test ARGS='-m "not ownership"'
test: .env
	$(TEST_COMPOSE) run --rm --build test pytest $(ARGS); \
	status=$$?; $(TEST_COMPOSE) down -v; exit $$status

lint: .env
	$(TEST_COMPOSE) run --rm --build --no-deps test sh -c "ruff check . && mypy"

seed: .env
	$(COMPOSE) run --rm --build --no-deps api python -m mailroom.seed

demo: up seed
	@echo "--- GET /healthz"
	@curl -fsS http://127.0.0.1:$(API_PORT)/healthz
	@echo
	@echo "--- GET /readyz"
	@curl -fsS http://127.0.0.1:$(API_PORT)/readyz
	@echo
	@echo "--- Uploads"
	@$(COMPOSE) exec -T api python -m mailroom.demo
