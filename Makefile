-include .env
API_PORT ?= 8000

COMPOSE := docker compose
# A separate project, because the test suite truncates tables and purges queues.
TEST_COMPOSE := docker compose -p mailroom-test

.PHONY: up down migrate test lint seed demo evaluate evaluate-advisor web drills backfill-blobs

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

# Copies document files from the old shared volume into Postgres. Safe to repeat.
backfill-blobs: .env
	$(COMPOSE) run --rm --build api python -m mailroom.backfill_blobs

seed: .env
	$(COMPOSE) run --rm --build --no-deps api python -m mailroom.seed

# Writes the report to docs/EVALUATION.md. It needs no database or broker.
evaluate: .env
	$(TEST_COMPOSE) build test
	$(TEST_COMPOSE) run --rm --no-deps -T test python -m mailroom.evaluation > docs/EVALUATION.md

# Sends the generated synthetic documents, and nothing else, to Gemini's free tier. Needs
# GEMINI_API_KEY in your shell. Answers are cached in .cache/, so a run stopped by the daily
# limit continues where it left off.
evaluate-advisor: .env
	@test -n "$$GEMINI_API_KEY" || { echo "Set GEMINI_API_KEY in your shell first" >&2; exit 1; }
	$(TEST_COMPOSE) build test
	mkdir -p .cache
	$(TEST_COMPOSE) run --rm --no-deps -T -v "$(CURDIR)/.cache:/app/.cache" \
		-e ADVISOR=gemini -e ADVISOR_SYNTHETIC_DATA_ONLY=true -e GEMINI_API_KEY \
		test python -m mailroom.advisor_evaluation > .cache/report.md
	mv .cache/report.md docs/ADVISOR_EVALUATION.md

# Injects real failures into a separate stack and writes the run to docs/DRILLS.md.
drills: .env
	python3 scripts/drills.py

# The review page, on http://127.0.0.1:5173. It needs Node, and `make up` for the API.
web:
	npm --prefix web ci
	API_PORT=$(API_PORT) npm --prefix web run dev

demo: up seed
	@echo "--- GET /healthz"
	@curl -fsS http://127.0.0.1:$(API_PORT)/healthz
	@echo
	@echo "--- GET /readyz"
	@curl -fsS http://127.0.0.1:$(API_PORT)/readyz
	@echo
	@echo "--- Uploads"
	@$(COMPOSE) exec -T api python -m mailroom.demo
