.PHONY: help setup db-up db-down db-shell schema run-sim reseed streak survival cohort nudge quality report test lint fmt notebook clean

VENV := venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

help:  ## Show available commands
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup:  ## Create venv (Python 3.12), install deps, install pre-commit hooks
	uv venv --seed --python 3.12 $(VENV)
	$(PIP) install -r requirements.txt
	$(VENV)/bin/pre-commit install
	@test -f .env || cp .env.example .env

db-up:  ## Start the Postgres + Metabase containers
	docker compose up -d postgres
	@echo "waiting for postgres..."
	@until docker compose exec -T postgres pg_isready -U cadence_user -d cadence >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready"

metabase-up:  ## Start Metabase (depends on postgres)
	docker compose up -d metabase

db-down:  ## Stop containers (data volume is preserved)
	docker compose down

db-shell:  ## Open a psql shell inside the container
	docker compose exec postgres psql -U cadence_user -d cadence

schema:  ## Apply sql/schema.sql to the local database
	docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U cadence_user -d cadence < sql/schema.sql

run-sim:  ## Generate synthetic data and load it into Postgres (refuses if data exists)
	$(PY) -m src.simulate.generate_daily_sip_data

reseed:  ## DESTRUCTIVE: wipe all tables, restart user_id at 1, regenerate
	$(PY) -m src.simulate.generate_daily_sip_data --reseed

streak:  ## Build streak tables from raw transactions
	$(PY) -m src.analysis.streak_builder

survival:  ## Run Kaplan-Meier + Cox PH survival analysis
	$(PY) -m src.analysis.survival_analysis

cohort:  ## Build D1/D7/D30/D90 cohort retention curves
	$(PY) -m src.analysis.cohort_analysis

nudge:  ## Run the nudge treatment/control statistical test
	$(PY) -m src.analysis.nudge_simulation

quality:  ## Run data quality checks and write flags
	$(PY) -m src.analysis.data_quality

report:  ## Generate the weekly markdown report
	$(PY) -m src.reporting.generate_weekly_report

all: schema reseed streak survival cohort nudge quality report  ## Full pipeline, end to end

test:  ## Run the test suite
	$(VENV)/bin/pytest -q

lint:  ## Lint and format-check
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/black --check src tests

fmt:  ## Auto-format
	$(VENV)/bin/black src tests
	$(VENV)/bin/ruff check --fix src tests

notebook:  ## Launch Jupyter
	$(VENV)/bin/jupyter notebook notebooks/

clean:  ## Remove caches and generated reports
	rm -rf .pytest_cache .ruff_cache reports/ && find . -name __pycache__ -type d -prune -exec rm -rf {} +
