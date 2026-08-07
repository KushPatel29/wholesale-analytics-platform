PYTHON ?= python3
BASE_URL ?= http://127.0.0.1:5000
PY_PATHS := app tests scripts data etl manage.py run.py wsgi.py data_loader.py fact_checkpoints.py worker.py

.PHONY: help run run-full preflight test test-all lint format check-static smoke smoke-rbac smoke-fact test-playwright

help:
	@printf "Wholesale Analytics developer commands\n\n"
	@printf "  make run             Start the dev server without mutating seed users or running heavy checks\n"
	@printf "  make run-full        Start the dev runner with its default behavior\n"
	@printf "  make preflight       Run runtime preflight checks only\n"
	@printf "  make test            Run pytest excluding slow tests\n"
	@printf "  make test-all        Run the full pytest suite\n"
	@printf "  make lint            Run black --check, ruff, mypy, and bandit\n"
	@printf "  make format          Fix import ordering and run black\n"
	@printf "  make check-static    Check key static assets against a running local app\n"
	@printf "  make smoke           Run parquet smoke checks\n"
	@printf "  make smoke-rbac      Run auth/RBAC smoke checks\n"
	@printf "  make smoke-fact      Run DuckDB fact smoke checks\n"
	@printf "  make test-playwright Run Playwright specs (requires browser deps/local app)\n"

run:
	$(PYTHON) run.py --server --skip-tests --skip-smoke --skip-seed

run-full:
	$(PYTHON) run.py --server

preflight:
	$(PYTHON) run.py --preflight

test:
	$(PYTHON) -m pytest -q --maxfail=1 -m "not slow"

test-all:
	$(PYTHON) -m pytest -q --maxfail=1

lint:
	$(PYTHON) -m black --check $(PY_PATHS)
	$(PYTHON) -m ruff check $(PY_PATHS)
	$(PYTHON) -m mypy --ignore-missing-imports -p app wsgi.py data_loader.py
	$(PYTHON) -m bandit -q -r app data_loader.py

format:
	$(PYTHON) -m ruff check --select I --fix $(PY_PATHS)
	$(PYTHON) -m black $(PY_PATHS)

check-static:
	bash scripts/check_static_assets.sh $(BASE_URL)

smoke:
	$(PYTHON) scripts/smoke.py

smoke-rbac:
	$(PYTHON) scripts/smoke_rbac.py

smoke-fact:
	$(PYTHON) scripts/fact_smoke.py

test-playwright:
	npx playwright test
