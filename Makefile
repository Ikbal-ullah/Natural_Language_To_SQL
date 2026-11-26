# nl2sql -- natural language to SQL over a learning platform
#
# Start here:  make help
# First run:   make install && make db && make index-exercise

PY      := .venv/bin/python
PIP     := .venv/bin/pip
RUFF    := .venv/bin/ruff
MYPY    := .venv/bin/mypy
PYTEST  := .venv/bin/python -m pytest
# Use psql from PATH if present, else the Homebrew location. Keeps `make db` working
# both for someone who installed PostgreSQL via brew and for someone who did not.
PSQL    := $(shell command -v psql || echo /opt/homebrew/opt/postgresql@16/bin/psql)
PGBIN   := $(dir $(PSQL))
DB      := nl2sql
SCALE   ?= 1.0

.PHONY: help install db migrate seed reset index-exercise run test exercises \
        lint types check eval sweep safety failures drills demos clean

help:
	@echo "Setup"
	@echo "  make install         create .venv and install dependencies"
	@echo "  make db              create the database, migrate and seed ($(SCALE) scale)"
	@echo "  make reset           drop and rebuild the database from scratch"
	@echo ""
	@echo "Learn"
	@echo "  make index-exercise  measure 10 queries before/after indexing (start here)"
	@echo "  make demos           run all four demos"
	@echo "  make drills          list the 40 SQL drills"
	@echo "  make failures        run the 8 deliberate bugs and measure the damage"
	@echo "  make exercises       run the exercise tests (they fail until you implement)"
	@echo ""
	@echo "Use"
	@echo "  make run             start the API and frontend on :8912"
	@echo "  make eval            run the 60-question golden set + 15 adversarial cases"
	@echo "  make safety          adversarial set only (no model calls, no cost)"
	@echo "  make sweep           compare prompt strategies / formats / few-shot counts"
	@echo ""
	@echo "Verify"
	@echo "  make check           lint + types + tests"

install:
	python3.11 -m venv .venv
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -e ".[dev]"
	@echo "installed. next: make db"

db:
	@$(PSQL) -lqt | cut -d'|' -f1 | grep -qw $(DB) || $(PGBIN)createdb $(DB)
	$(PY) db/migrate.py --to 8
	$(PY) db/seed/seed.py --scale $(SCALE)
	@echo "database ready. Indexes are deliberately NOT built yet: run make index-exercise"

migrate:
	$(PY) db/migrate.py --to 8

reset:
	-$(PGBIN)dropdb --if-exists $(DB)
	$(MAKE) db

# The teaching artifact. Measures the unindexed baseline, applies migration 0009,
# measures again, and writes db/artifacts/index_exercise.json.
index-exercise:
	$(PY) db/indexing/run_exercise.py

run:
	.venv/bin/uvicorn nl2sql.api:app --host 127.0.0.1 --port 8912 --app-dir src --reload

test:
	$(PYTEST) tests/ -q

exercises:
	$(PYTEST) exercises/tests/ -q

lint:
	$(RUFF) check .

types:
	$(MYPY) src/

check: lint types test

eval:
	$(PY) eval/run_eval_learners.py

safety:
	$(PY) eval/run_eval_learners.py --safety-only --tag safety

sweep:
	$(PY) eval/sweep.py

failures:
	$(PY) failures/run_failures.py

drills:
	$(PY) drills/check.py --list

demos:
	@for d in demos/demo_*.py; do echo; echo "=== $$d ==="; $(PY) $$d; done

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------------------
# E-commerce hardening stack (docker). Python: .venv312 (3.12; .venv is 3.15 and lacks wheels)
# ---------------------------------------------------------------------------
PY312 := .venv312/bin/python

shop-up:
	docker compose up -d --wait
	$(PY312) db/seed_ecommerce.py --checksum

shop-test:
	$(PY312) -m pytest tests/test_security_integration.py -q

shop-eval:
	$(PY312) eval/run_eval.py --provider oracle
	$(PY312) eval/run_eval.py --provider faulty

shop-eval-live:  # needs ANTHROPIC_API_KEY
	$(PY312) eval/run_eval.py --provider anthropic
	$(PY312) eval/render_results.py

shop-down:
	docker compose down -v
