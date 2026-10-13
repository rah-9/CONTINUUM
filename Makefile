# Developer entry points. Requires a local PostgreSQL (see README "Setup").
PY ?= .venv/bin

.PHONY: install db lint type test unit integration chaos check lab-smoke lab-safety

install:
	uv venv -p 3.12 .venv && uv pip install -p .venv -e ".[dev,http,mcp,live]"

db:
	$(PY)/continuum db-init

lint:
	$(PY)/ruff check src eval tests

type:
	$(PY)/mypy

unit:
	$(PY)/pytest tests/unit -q

integration:
	$(PY)/pytest tests/integration tests/contract -q

chaos:
	$(PY)/pytest tests/chaos -q

test:
	$(PY)/pytest -q

# The gate every change must pass before review (docs/06 section 5).
check: lint type test

lab-smoke:
	$(PY)/continuum-lab run --suite smoke --out results/smoke

lab-safety:
	$(PY)/continuum-lab run --suite safety --out results/safety
