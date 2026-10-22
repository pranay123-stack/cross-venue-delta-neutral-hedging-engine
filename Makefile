# Development commands.  Every target is a plain command you can also run by hand.
PY := backend/.venv/bin/python
SQLITE := sqlite+aiosqlite:///$(CURDIR)/backend/hedgelab.db
REPLAY := HEDGELAB_MARKET_DATA_SOURCE=replay \
	HEDGELAB_MARKET_REPLAY_PATH=$(CURDIR)/backend/tests/data/live_frames.jsonl.gz \
	HEDGELAB_MARKET_HISTORY_PATH=$(CURDIR)/backend/tests/data/history.json.gz

.PHONY: help install test lint format typecheck frontend e2e check demo demo-live scenarios backtest up down screenshots

help:  ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install:  ## backend venv (pinned) + frontend packages
	python3.12 -m venv backend/.venv
	cd backend && .venv/bin/pip install -c requirements-lock.txt -e ".[dev]"
	cd frontend && npm ci

test:  ## backend tests, offline (unit, property, integration)
	cd backend && .venv/bin/python -m pytest tests -q

lint:  ## ruff
	cd backend && .venv/bin/ruff check src tests scripts

format:  ## ruff autofix
	cd backend && .venv/bin/ruff check src tests scripts --fix

typecheck:  ## mypy (strict) + tsc
	cd backend && .venv/bin/mypy src
	cd frontend && npx tsc --noEmit

frontend:  ## typecheck and build the dashboard
	cd frontend && npm run build

e2e:  ## browser tests on the replay recording (needs Google Chrome)
	cd frontend && npm run test:e2e

check: lint typecheck test frontend e2e  ## everything CI runs

demo:  ## the 25-step acceptance run, offline on the recorded session
	HEDGELAB_DATABASE_URL=$(SQLITE) $(REPLAY) backend/.venv/bin/hedgelab demo

demo-live:  ## the same, on the venues' live public data
	HEDGELAB_DATABASE_URL=$(SQLITE) backend/.venv/bin/hedgelab demo

scenarios:  ## the ten failure scenarios, offline
	HEDGELAB_DATABASE_URL=$(SQLITE) $(REPLAY) backend/.venv/bin/hedgelab scenario run-all

backtest:  ## 30 days of real history, with book impact from the recording
	backend/.venv/bin/hedgelab backtest run --history backend/tests/data/history.json.gz \
		--books backend/tests/data/live_frames.jsonl.gz

up:  ## full stack in Docker (live data)
	docker compose up --build -d

down:
	docker compose down

screenshots:  ## re-capture README screenshots from the running app
	cd frontend && npx playwright test -c playwright.screenshots.config.ts
