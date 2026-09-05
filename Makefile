# Chronos developer targets. All commands assume the project venv at .venv.
PY := .venv/bin/python

.PHONY: test lint format-check type type-worker security-gate release-gate gates backend ui demo migrate

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

security-gate:
	@untracked="$$(git ls-files --others --exclude-standard)"; \
	if [ -n "$$untracked" ]; then \
	  { \
	    echo "security-gate refuses to run: untracked files in the working tree."; \
	    echo "The tracked-file secret scan enumerates 'git ls-files', so it cannot see these"; \
	    echo "files. Running anyway would report a pass over a file set that excludes them:"; \
	    printf '%s\n' "$$untracked" | sed 's/^/  /'; \
	    echo "Stage them (git add) or remove them, then re-run."; \
	  } >&2; \
	  exit 1; \
	fi
	$(PY) scripts/verify_release_security.py

release-gate:
	$(PY) scripts/verify_release_artifact.py

gates: lint format-check type type-worker test security-gate release-gate

backend:
	$(PY) scripts/run_backend.py

ui:
	$(PY) scripts/run_ui.py

demo: ui

migrate:
	.venv/bin/alembic upgrade head
