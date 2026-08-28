---
name: chronos-build-and-env
description: "Recreate or diagnose the Chronos Python environment, dependency lock, package build, migrations, and CI parity. Use for fresh setup, interpreter or install failures, lock maintenance, wheel or package-data problems, migration setup, and local-versus-CI drift. Differentiator: derive the current workflow from repository build files and verify the installed artifact; use chronos-run-and-operate for runtime operation and chronos-validation-and-qa for test design."
---

# Chronos build and environment

Treat this skill as a procedure, not a snapshot. Derive versions, gate membership,
migration state, package contents, and expected test outcomes from the checked-out
revision each time. If this file and an executable repository authority disagree,
the repository authority wins; update this skill and its contract test in the same
change.

## Route the task

Use this skill for:

- selecting a compatible Python interpreter and creating `.venv`;
- reproducing the pinned dependency installation used by CI;
- changing or diagnosing `requirements-dev.lock`;
- running the build, quality, and release-artifact gates;
- diagnosing missing wheel assets, console entry points, or migrations;
- initializing a fresh database or preparing a controlled existing-database
  upgrade; and
- explaining why a local environment differs from CI.

Route runtime startup, dashboard operation, and operator recovery to
`chronos-run-and-operate`. Route test strategy, evidence design, and QA coverage to
`chronos-validation-and-qa`. Route schema-model changes to
`chronos-change-control` after the environment is healthy.

## Source hierarchy

Read these before changing the build or environment:

1. `AGENTS.md` and `docs/AGENT_PROTOCOL.md` for task authority, safety gates, and
   verification requirements.
2. `pyproject.toml` for `requires-python`, build backend, dependencies, entry
   points, package data, and tool configuration.
3. `requirements-build.in`, `requirements-build.lock`, and
   `requirements-dev.lock` for the exact hash-locked build backend and
   development environment plus their generator commands.
4. `Makefile` and `.github/workflows/ci.yml` for the live local and CI gates.
5. `docs/DEPLOYMENT.md` and `docs/SECURITY.md` for supported deployment and
   dependency-maintenance policy.
6. `scripts/verify_release_artifact.py` for what the built artifact must prove.
7. `scripts/initialize_database.py`, `src/chronos/persistence/database.py`,
   `alembic.ini`, `src/chronos/persistence/migrations/env.py`, and
   `src/chronos/persistence/migrations/versions/` for current database
   initialization, URL resolution, and upgrade behavior.
8. `docs/limitations.md` for optional integrations and unsupported assumptions.

Prose explains intent. The checked-in configuration, source, scripts, and tests
are the executable truth.

## 1. Establish the exact revision and authorities

Work from a clean, isolated checkout and inspect before installing:

```bash
git status --short --branch
git rev-parse HEAD
rg -n '^requires-python|^\[build-system\]|^build-backend' pyproject.toml
sed -n '/^gates:/p' Makefile
rg -n '^\s*- name:|^\s*run:|python-version:' .github/workflows/ci.yml
sed -n '1,2p' requirements-build.lock requirements-dev.lock
```

Record the commit. Do not compare outcomes from different revisions as though
they describe one environment. Do not infer the current interpreter, gate list,
action versions, timeout, migration head, or dependency versions from this skill.

## 2. Create the interpreter environment

Derive the supported range from `pyproject.toml` and the CI-selected interpreter
from `.github/workflows/ci.yml`. Select an installed interpreter that satisfies
both, then substitute its command below:

```bash
<python-that-satisfies-the-repo> --version
<python-that-satisfies-the-repo> -m venv .venv
.venv/bin/python -c 'import sys; print(sys.executable); print(sys.version)'
```

Use `.venv/bin/python -m ...` in later commands so the evidence names the actual
interpreter. A virtual environment is disposable and non-portable: recreate it
after moving the checkout or changing interpreter families instead of repairing
its internal paths.

## 3. Reproduce the pinned CI install

First re-read the install block in `.github/workflows/ci.yml`. The supported local
equivalent installs the hash-verified dependency closure and then the Chronos
project without resolving a second dependency graph:

```bash
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --require-hashes -r requirements-build.lock
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install -e . --no-deps --no-build-isolation --check-build-dependencies
```

The first command updates the installer and is outside the application dependency
lock. The build lock must be installed before isolation is disabled: this makes
the hash-verified backend the build input, while `--check-build-dependencies`
verifies that it satisfies `pyproject.toml`. Record the resulting toolchain when
provenance matters:

```bash
.venv/bin/python -m pip --version
.venv/bin/python -m pip freeze --all
```

Do not remove `--require-hashes`, relax pins, or fall back to an unpinned install
to make a failure disappear. A hash or resolution failure is evidence that the
lock, package index, platform, or intended dependency inputs need investigation.

## 4. Run the gates CI defines

Derive gate membership before execution:

```bash
sed -n '/^gates:/p' Makefile
rg -n '^\s*- name:|^\s*run:' .github/workflows/ci.yml
make gates
```

`make gates` is the local aggregate. The workflow is the final authority for what
CI executes and in what environment. If the two diverge, report the drift; do not
silently declare parity. Preserve the command, exit code, warnings, skips, and
failure output from the current run instead of comparing against counts copied
from an older revision.

When diagnosing one gate, run its current `Makefile` recipe directly, make the
smallest correction, then rerun `make gates` from the beginning.

## 5. Verify the release artifact boundary

An editable install proves source-tree imports, not that a user can install the
published artifact. Run the repository release check independently when packaging
is involved:

```bash
make release-gate
```

Read `scripts/verify_release_artifact.py` before changing packaging. It builds in
an isolated tree, installs the wheel into a clean environment, and checks the
installed wheel rather than the editable checkout. Keep these boundaries intact:

- terminal assets under `src/chronos/terminal/static/` must be declared by the
  package-data configuration in `pyproject.toml`;
- the migration environment and revisions under
  `src/chronos/persistence/migrations/` must ship in the archive and wheel;
- the installed console entry point and supported module entry points must start;
- an existing supported database must reach the migration head from the installed
  package; and
- imports must resolve from the clean installed environment, not the repository.

The verifier derives its local source set with:

```bash
git ls-files --cached --others --exclude-standard
```

That deliberately includes non-ignored untracked files. Therefore a local release
pass can include files that CI will not receive. Inspect `git status` and ensure
every required artifact is committed before claiming CI or release parity.

## 6. Initialize or upgrade a database deliberately

### Fresh database

For a genuinely new local database, inspect `scripts/initialize_database.py` and
`src/chronos/persistence/database.py`, choose a new explicit disposable path, and
initialize only that target:

```bash
.venv/bin/python scripts/initialize_database.py \
  --url sqlite:////absolute/new/path/chronos.db
```

Do not point a “fresh” initialization command at an existing file. The script and
database module, not a schema number copied into documentation, define current
fresh-create behavior.

### Existing database

An existing database is state, not a build artifact. Before any upgrade:

1. identify the exact database URL and environment;
2. stop writers and back up the database with a restorable copy;
3. inspect the migration graph and current state with the checked-out code; and
4. obtain human approval before touching shared, live, or otherwise valuable data.

Useful read-only inspection starts with:

```bash
.venv/bin/alembic -c alembic.ini heads
DATABASE_URL="sqlite:////absolute/path/to/existing.db" \
  .venv/bin/alembic -c alembic.ini current
```

The phrase `alembic heads` is a concept here; the command above is authoritative
because it also names this repository's configuration. Only after the target,
backup, supported starting state, and approval are explicit should an operator
follow `chronos-run-and-operate`, inspect the current `make migrate` recipe, and run
it with `DATABASE_URL` set to the same explicit target. Never run an upgrade merely
because application startup reported drift.

## 7. Maintain the dependency lock

Dependency changes are owner-reviewed build-input changes. Keep the exact
requirement in `requirements-build.in` aligned with `[build-system].requires` in
`pyproject.toml`. Before regenerating either lock, inspect the declaration diff
and read the generator commands from the locks themselves:

```bash
git diff -- pyproject.toml requirements-build.in requirements-build.lock requirements-dev.lock
sed -n '1,2p' requirements-build.lock requirements-dev.lock
```

Run the applicable checked-in command with its output directed to the existing
lock. uv prefers versions already present in an existing output file, which
limits unrelated churn. Use `--upgrade` or a package-scoped upgrade only when the
intended owner review explicitly includes that broader change.

After regeneration:

```bash
git diff -- pyproject.toml requirements-build.in requirements-build.lock requirements-dev.lock
.venv/bin/python -m pip install --require-hashes -r requirements-build.lock
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install -e . --no-deps --no-build-isolation --check-build-dependencies
make gates
```

Review the complete lock diff for unexpected transitive changes and preserve its
hashes. The application lock and build-backend lock cover distinct inputs; verify
both from the workflow, `docs/SECURITY.md`, and the release verifier.

## Failure triage

| Symptom | Inspect first | Required response |
|---|---|---|
| Wrong interpreter or imports | `pyproject.toml`, CI interpreter, `sys.executable` | Recreate `.venv` with a compatible interpreter. |
| Hash mismatch or resolver failure | lock header, package index, platform, declaration diff | Diagnose the mismatch; do not disable hash checking. |
| Local gate differs from CI | commit, workflow environment, install block, `Makefile` | Reproduce the safe CI environment and report real configuration drift. |
| Editable install passes but artifact fails | `scripts/verify_release_artifact.py`, package-data config, clean-tree status | Fix the archive or wheel boundary and rerun `make release-gate`. |
| Database version or migration error | explicit URL, backup, `alembic.ini`, migration graph, database module | Inspect first; never auto-upgrade unknown state. |
| `ibapi` import failure | `docs/limitations.md`, the adapter's error message, official IBKR TWS API docs | Follow the supported optional-integration procedure; do not install a similarly named package by guess. |

## Known pitfalls

- Local shell variables or `.env` files can differ from the safe CI environment.
  Preserve the workflow's demo and transmission safeguards; do not weaken safety
  flags to make tests pass.
- `worker/` is outside `src/chronos` but has its own strict type-check boundary in
  the live gates. Do not conclude it is unverified because the main package type
  check succeeds.
- `requirements.txt` and `requirements-dev.txt` are convenience inputs, not the
  hash-locked reproducibility boundary. CI uses both `requirements-build.lock`
  and `requirements-dev.lock`.
- Prefer the existing `.venv` executables for evidence commands. Tool convenience
  runners may create unrelated project metadata and dirty the checkout.
- A clean editable import does not prove package data, migrations, entry points,
  or installed-wheel behavior. The release gate exists for that distinction.
- IBKR's optional `ibapi` SDK is not permission to guess at a public package name.
  Follow `docs/limitations.md`, source diagnostics, and the official vendor
  installation guidance.

## Close the loop

Before reporting success:

1. capture `git status --short --branch` and the exact commit;
2. run the focused reproducer for the original failure;
3. run `make gates` after the last change;
4. verify the release artifact separately when build, package-data, entry-point,
   dependency, or migration behavior changed;
5. report every skipped check, warning, environment difference, and uncommitted
   input; and
6. compare the exact branch tip with the intended integration target before saying
   the fix is on `main`.

## Primary documentation

- Python virtual environments:
  <https://docs.python.org/3.12/library/venv.html>
- Python packaging `pyproject.toml` specification:
  <https://packaging.python.org/en/latest/specifications/pyproject-toml/>
- pip secure and hash-checked installs:
  <https://pip.pypa.io/en/stable/topics/secure-installs/>
- uv lock compilation and existing-output preference:
  <https://docs.astral.sh/uv/pip/compile/>
- setuptools package-data behavior:
  <https://setuptools.pypa.io/en/stable/userguide/datafiles.html#package-data>
- Alembic command API:
  <https://alembic.sqlalchemy.org/en/latest/api/commands.html>
- Interactive Brokers TWS API documentation:
  <https://www.interactivebrokers.com/docs/tws-api/doc/download-the-tws-api/install-the-tws-api-on-mac-os-linux>
