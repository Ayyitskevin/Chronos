# Chronos developer targets. All commands assume the project venv at .venv.
PY := .venv/bin/python

.PHONY: test lint format-check type type-worker gates backend ui demo migrate

test:
	$(PY) -m pytest -q

lint:
	.venv/bin/ruff check .

format-check:
	.venv/bin/ruff format --check .

type:
	.venv/bin/mypy src/chronos

type-worker:
	.venv/bin/mypy --strict worker

gates: lint format-check type type-worker test

backend:
	$(PY) scripts/run_backend.py

ui:
	$(PY) scripts/run_ui.py

demo: ui

migrate:
	.venv/bin/alembic upgrade head
