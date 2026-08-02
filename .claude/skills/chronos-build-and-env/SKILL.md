---
name: chronos-build-and-env
description: >
  Load this skill BEFORE touching the Chronos build environment in any way: setting up a
  fresh clone, creating a venv, running pip install, wondering which Python to use, or when
  anything environment-shaped breaks. Trigger phrases and symptoms: "set up", "install",
  "fresh clone", "venv", "pip", "lockfile", "requirements-dev.lock", "CI environment",
  "make test fails", "No such file or directory: .venv", "environment broken",
  "dependencies", "python version", "requires-python", "ModuleNotFoundError: chronos",
  "ibapi not installed", "hash mismatch", "alembic", "initialize_database". NOT for
  running the app (chronos-run-and-operate), config meanings
  (chronos-config-and-flags), or test-writing (chronos-validation-and-qa).
---

# Chronos — Build and Environment

Everything needed to recreate a working Chronos development environment from a bare
checkout, and every known way that process goes wrong. **CI is ground truth**: an
environment "works" if and only if the four CI gate commands pass in it, the same way
they pass on GitHub Actions. All facts below verified against the repo on 2026-08-02;
volatile numbers are date-stamped and re-baselined in the Provenance section.

Jargon, defined once:
- **venv** — a Python virtual environment; this repo's convention places it at `.venv/`
  in the repo root (the Makefile hard-codes that path).
- **Lockfile** — `requirements-dev.lock`: every runtime+dev dependency pinned to an exact
  version and SHA-256 hash. The supply-chain authority for this repo.
- **The four gates** — `ruff check .`, `ruff format --check .`, `mypy src/chronos`,
  `pytest -q` — exactly what CI runs, exactly what `make gates` runs.

## When NOT to use this skill

| You actually want | Go to |
|---|---|
| Start/stop the backend, UI, terminal, service; kill/halt/arm procedures | chronos-run-and-operate |
| What an env var / flag / risk-YAML key means, defaults, safety class | chronos-config-and-flags |
| Write or interpret tests; the suite map; what counts as evidence | chronos-validation-and-qa |
| Which document to trust when docs disagree (e.g. stale DEPLOYMENT.md sections) | chronos-docs-map |

## 0. The trap that hits first: bare `python3` may be 3.11

The project requires **Python >= 3.12** (`requires-python = ">=3.12"`,
pyproject.toml:10). On several machines — including the container this library was
authored in — the default `python3` is **3.11** while `python3.12` exists separately
(verified here: `python3 --version` → 3.11.15; `/usr/bin/python3.12 --version` → 3.12.3).

The failure is quiet: `python3 -m venv .venv` **succeeds** with 3.11 — venv creation does
not check `requires-python`. The break comes later: `pip install -e '.[dev]'` refuses with
a "requires a different Python" error, or (worse, with an old pip) you get an environment
that fails imports and gates in confusing ways.

**Rule: always create the venv with an explicit `python3.12`** (or newer). Check first:

```bash
python3 --version        # if this prints 3.11.x, do NOT use bare python3
python3.12 --version     # use this binary for venv creation
```

## 1. From-scratch setup — two sanctioned routes

Both routes start the same: a fresh checkout ships **no `.venv/`** (verified: absent), so
nothing runs until you build one. Both end the same: the four gates pass (§7).

### Route A — README quickstart (fast, unpinned)

Source: README.md:205-216. Use for a quick dev environment when you do not need
reproducibility. Substituting `python3.12` for the README's bare `python3` per §0:

```bash
cd Chronos
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python scripts/initialize_database.py
```

`pip install -e '.[dev]'` resolves from the *ranges* in pyproject.toml (lines 20-44), so
two installs a week apart can differ. Fine for scratch work; not what CI tests.

### Route B — CI/deploy-faithful (pinned, hash-verified)

Source: docs/DEPLOYMENT.md:15-26, identical to what CI runs (ci.yml:32-36). Use for
anything you intend to keep running, for debugging "works locally, fails in CI", and for
any environment where supply-chain integrity matters:

```bash
cd Chronos
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install -e . --no-deps
cp .env.example .env
.venv/bin/python scripts/initialize_database.py
```

`--require-hashes` refuses any package or version not pinned with a matching SHA-256 hash
in the lock. `--no-deps` on the project install stops pip re-resolving anything the lock
already provided.

### When to use which

| Situation | Route |
|---|---|
| Quick local hacking, throwaway env | A |
| Reproducing a CI result, debugging CI-vs-local differences | **B** (it IS the CI install) |
| A machine that will run Chronos unattended (deployment) | **B** (docs/DEPLOYMENT.md uses it) |
| Verifying a dependency bump | **B** after regenerating the lock (§5) |

The lock is the supply-chain truth; the ranges in pyproject.toml express intent only
(docs/SECURITY.md:96-114). When Route A and Route B environments disagree, the Route B
environment is the one that matters, because CI is the arbiter (§4).

`requirements.txt` is exactly `-e .` and `requirements-dev.txt` is exactly `-e .[dev]` —
one-line conveniences, not authorities.

## 2. Python and dependency facts

- **`requires-python = ">=3.12"`** (pyproject.toml:10). CI pins exactly 3.12 (ci.yml:29).
  mypy and ruff both target py312 (pyproject.toml:71,81).
- Key pins in the lock as of 2026-08-02 (requirements-dev.lock; ranges in
  pyproject.toml:20-44): `ib-async==2.1.0`, `fastapi==0.139.2`, `streamlit==1.59.2`,
  `pandas==2.3.3`, `sqlalchemy==2.0.51`, `alembic==1.18.5`, `pydantic==2.13.4`,
  `numpy==2.5.1`, `uvicorn==0.51.0`, `pytest==8.4.2`, `mypy==1.20.2`, `ruff==0.15.22`,
  `hypothesis==6.156.6`.
- **`ibapi` is DELIBERATELY not installed and not installable from PyPI.** It appears in
  no dependency list and not in the lock (verified by grep). The official-IBKR code paths
  import it lazily and raise install guidance if absent — src/chronos/broker/
  official_ibkr.py:202-206: *"The official IBKR TWS API package (ibapi) is not installed.
  It is not on PyPI: download the TWS API from interactivebrokers.github.io… Demo mode
  runs without it."* Exactly three files import it (broker/official_ibkr.py,
  histdata/official_client.py, histdata/official_options_client.py). **The entire test
  suite passes without ibapi** (verified in this environment, which has no ibapi). Do NOT
  "fix" the missing dependency by pip-installing some PyPI package named `ibapi` — that
  would be an unvetted substitute for the official IBKR SDK.
- **`node` is required for CI, optional locally.** The terminal-client safety tests run
  the real browser JavaScript in `node:vm`. If `node` is not on PATH they **skip locally
  but hard-fail whenever the `CI` env var is set** (tests/safety/
  test_terminal_client.py:57-68). GitHub runners ship node; never strip it from a CI
  image. This container has node v22.22.2, so nothing skips here.
- The mypy gate is **strict** (`strict = true`, pyproject.toml:82) but covers
  **`src/chronos` only** — tests/ and scripts/ are not type-checked. Ruff checks
  everything.
- Only ONE pytest marker is registered (`ibkr`, pyproject.toml:65-67) and
  `--strict-markers --strict-config` are on (pyproject.toml:62): an unregistered marker
  or config key fails collection outright.
- **No coverage tooling exists** (no pytest-cov, no coverage config, no CI coverage
  step). Any coverage number you encounter was not produced by this repo.

## 3. Makefile — every target

/home/user/Chronos/Makefile (29 lines). Line 2: `PY := .venv/bin/python`. **Every target
hard-codes `.venv/bin/…`; nothing falls back to system python; every target fails with
"No such file or directory" until `.venv` exists.**

| Target | Runs | Lines |
|---|---|---|
| `make test` | `.venv/bin/python -m pytest -q` | 6-7 |
| `make lint` | `.venv/bin/ruff check .` | 9-10 |
| `make format-check` | `.venv/bin/ruff format --check .` | 12-13 |
| `make type` | `.venv/bin/mypy src/chronos` | 15-16 |
| `make gates` | depends on `lint format-check type test` — the four CI commands | 18 |
| `make backend` | `.venv/bin/python scripts/run_backend.py` | 20-21 |
| `make ui` | `.venv/bin/python scripts/run_ui.py` | 23-24 |
| `make demo` | alias for `ui` only | 26 |
| `make migrate` | `.venv/bin/alembic upgrade head` | 28-29 |

There is no `make install`, `make setup`, `make coverage`, or `make clean`.

## 4. CI — exactly what runs, and why it is the arbiter

One quality workflow: `.github/workflows/ci.yml` — workflow "CI", single job `quality`,
`ubuntu-latest`, **10-minute timeout** (line 17), triggered on every push and
pull_request. Job-level env (lines 18-21) — CI *forces* the safe posture:

```yaml
BROKER_MODE: demo
ALLOW_ORDER_TRANSMIT: "false"
ALLOW_LIVE_TRADING: "false"
```

Steps, in order (lines 22-48):

1. `actions/checkout@v5`
2. `actions/setup-python@v6` — Python `"3.12"`, pip cache
3. Install: `python -m pip install --upgrade pip` →
   `pip install --require-hashes -r requirements-dev.lock` →
   `pip install -e . --no-deps`
4. `ruff check .`
5. `ruff format --check .`
6. `mypy src/chronos`
7. `pytest -q`

So CI = Route B install + the four gates in lint→format→type→test order. No matrix, no
coverage, no artifacts, no separate integration job; migration verification runs *inside*
pytest (§6). The whole job fits inside 10 minutes — the suite itself takes ~2 minutes —
so **a hang, not slowness, is what kills CI**.

**When local results differ from CI, CI is ground truth.** Reproduce its environment with
Route B on Python 3.12 before concluding anything about the code. The second workflow,
`opencode.yml`, is not a quality gate (a comment-triggered agent assist; needs the
`OPENCODE_API_KEY` secret) — ignore it for environment purposes.

## 5. Lockfile discipline

- The lock is autogenerated; its own header (requirements-dev.lock:1-2) records the exact
  command:

  ```
  uv pip compile pyproject.toml --extra dev --generate-hashes --python-version 3.12 -o requirements-dev.lock
  ```

- **Regenerating the lock is an owner-reviewed change**, not a casual one: docs/
  SECURITY.md:111-114 — "Maintenance (owner action): regenerate the lock with the command
  above when bumping a bound, and review the diff before committing." If you bump a bound
  in pyproject.toml without regenerating, CI's `--require-hashes` install fails.
- **Known residuals of the hash gate** (docs/SECURITY.md:106-110): it covers the
  runtime+dev closure but NOT (a) the PEP 517 *build backend* — `pip install -e .` still
  fetches `setuptools`/`wheel` unpinned inside pip's isolated build env — and NOT (b) pip
  itself (upgraded unpinned in step 3). docs/DEPLOYMENT.md:37-42 therefore recommends
  recording `pip freeze` at deployment time.
- **The `aeventkit==2.1.0` entry is genuine, not a typosquat** (docs/SECURITY.md:108-110):
  it is the dependency `ib_async` itself declares — the ib-api-reloaded republication of
  `eventkit`, same maintainer org, provides the `eventkit` module. Do not "clean it up".

## 6. Databases and directories the environment creates

- `scripts/initialize_database.py` (24 lines): constructs
  `Database(DATABASE_URL)` and calls `database.initialize()` — **create or verify** the
  current wheel-dashboard SQLite schema, then prints "Chronos schema version 7 is
  initialized" (`SCHEMA_VERSION = 7`, src/chronos/persistence/database.py:20). Optional
  `--url` overrides `DATABASE_URL` for that run. It is idempotent on a healthy DB.
- **Where files land** (all CWD-relative — run from the repo root):
  - Wheel DB: `data/chronos.db` (default `DATABASE_URL = sqlite:///data/chronos.db`,
    src/chronos/config/settings.py:159; same default in alembic.ini:7).
  - Logs: `logs/chronos.log` (settings.py:161).
  - Platform state: `data/platform_halt.json`, `data/platform_audit.jsonl` — CLI/service
    defaults, CWD-relative (docs/DEPLOYMENT.md:56-59). The platform creates `data/` on
    demand, but `mkdir -p data logs` first keeps first-run behavior obvious
    (DEPLOYMENT.md:44-51).
  - Everything under `data/*.db|json|jsonl` and `logs/*.log` is gitignored
    (.gitignore:13-20).
- **Alembic is the v2→head upgrade path ONLY — fresh databases never run alembic.**
  alembic.ini:1-4 (header comment): "Fresh databases never need alembic:
  Database.initialize() creates the current schema directly." Six revisions exist
  (src/chronos/persistence/migrations/versions/): 0001_v2_baseline …
  0006_proposal_queue. `make migrate` = `.venv/bin/alembic upgrade head`; only needed
  when upgrading a pre-existing old DB.
- **Migration correctness is tested inside pytest**, not as a CI step:
  tests/integration/test_migrations.py builds a v2-shaped DB, runs
  `alembic upgrade head`, then asserts `Database.initialize()` accepts it with zero
  drift. So a green `pytest -q` already proves the migration chain.

## 7. Verification — prove your environment works

Run the four gates from the repo root (this is `make gates`, and it is CI):

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/chronos
.venv/bin/python -m pytest -q
```

Expected SHAPE as of 2026-08-02 (all four re-run and verified; the authoritative
numeric baseline lives in **chronos-validation-and-qa §2** — re-measure, don't quote):

| Gate | Expected shape |
|---|---|
| `ruff check .` | `All checks passed!` |
| `ruff format --check .` | `N files already formatted`, exit 0 (~379 as of 2026-08-02; the `.claude/skills` scripts are in scope) |
| `mypy src/chronos` | `Success: no issues found in N source files`, exit 0 (~218) |
| `pytest -q` | green with exactly 1 skip and 5 warnings (~2489 passed as of 2026-08-02) in ~98-115 s |

The 1 skip is always tests/integration/test_ibkr_smoke.py (opt-in via
`CHRONOS_RUN_IBKR_SMOKE=1`; marker `ibkr`; lines 15-23). The 5 warnings are Starlette
deprecation warnings (httpx test-client deprecation plus `HTTP_422_UNPROCESSABLE_ENTITY`
naming, e.g. src/chronos/api/routes/orders.py:154) — benign today, will bite on a future
starlette/fastapi bump.

**These counts drift.** Every doc-quoted count is a snapshot (docs/TEST_RESULTS.md says
1901 — stale; see chronos-docs-map for the stale-doc ledger). To re-baseline: run
`.venv/bin/python -m pytest -q` yourself and trust that number; anything else green +
`0 failed` = working environment. If mypy/format file counts differ from the table but
exit 0, the environment is still fine — files were added since 2026-08-02.

**If the whole suite fails with "SAFETY TRIPWIRE: ambient settings are live-capable"** —
that is your `.env`, not the code. Two autouse tripwires in tests/conftest.py:17-52
(ADR-0009) fail the run by design if a live-capable `.env` leaks into the test
environment. Keep the repo-root `.env` demo-safe when running tests. This is a feature;
never weaken it.

## 8. Environment gotchas — this container vs a laptop

| # | Gotcha | Detail |
|---|---|---|
| 1 | No `.venv` ships in the checkout | Every make target fails until you create it (§1). Verified absent here. |
| 2 | Bare `python3` may be 3.11 | This container: 3.11.15 default, `/usr/bin/python3.12` exists. Always `python3.12 -m venv` (§0). |
| 3 | `.env` is gitignored and never committed | .gitignore:1; README.md:216 "Do not commit `.env`; it is ignored." `cp .env.example .env` per machine. Defaults are demo-safe (`BROKER_MODE=demo`); meanings live in chronos-config-and-flags. |
| 4 | Test/lint runs leave cache dirs | `.pytest_cache/`, `.ruff_cache/`, `__pycache__/` appear in the checkout; all gitignored, so they never dirty `git status`. Sandboxed containers may deny deleting them; that is harmless. |
| 5 | Sandbox denials in agent containers | Writes outside the repo/scratchpad may be blocked. If a command fails with a permission error, check the sandbox before blaming the code. |
| 6 | The `chronos` console script is NOT the CLI | `chronos = "chronos.app:main"` (pyproject.toml:46-47) is the **legacy Streamlit dashboard's** main — it calls `st.set_page_config` (src/chronos/app.py:13-14) and misbehaves outside Streamlit. Run the dashboard as `.venv/bin/streamlit run src/chronos/app.py`; the actual CLI is `.venv/bin/python -m chronos.cli`. |
| 7 | `PYTHONPATH=src` is a fallback, not the sanctioned route | With the src layout, `PYTHONPATH=src python -m pytest` works without `pip install -e .` (how this library's authoring env ran read-only). Fine for a throwaway venv you refuse to install into; the sanctioned routes install the package (§1), which also exercises packaging (e.g. the terminal static files, pyproject.toml:58-59, ship as package-data and would 404 only in an installed env if broken). |
| 8 | CWD-relative default paths | DB, halt file, audit log, research data resolve from the current directory (§6). Run from the repo root or pass explicit `--halt-file`/`--audit-file`/`--url` flags. |
| 9 | DEPLOYMENT.md "Future work — NOT IMPLEMENTED" (lines 133-158) is stale | `python -m chronos.service --mode shadow` exists and is tested. Trust code/README; see chronos-docs-map. The *install* sections of DEPLOYMENT.md (§1 Route B here) are accurate. |
| 10 | CI forces demo/no-transmit env | ci.yml:18-21 (§4). Never "fix" a CI failure by loosening those three values — that violates the fail-closed posture (see chronos-change-control). |

## 9. Provenance and maintenance

All facts verified 2026-08-02 against branch `claude/chronos-skills-library-bfbj29`
(HEAD 47a8d72). Volatile facts and their one-line re-verification commands:

| Volatile fact (2026-08-02) | Re-verify with |
|---|---|
| Gate baselines (~2489 passed / 1 skipped; mypy ~218; format ~379 — authoritative home: chronos-validation-and-qa §2) | `.venv/bin/python -m pytest -q` ; `.venv/bin/mypy src/chronos` ; `.venv/bin/ruff format --check .` |
| Key pins (ib-async 2.1.0, fastapi 0.139.2, …) | `grep -E "^(ib-async\|fastapi\|streamlit\|pandas\|sqlalchemy\|alembic)==" requirements-dev.lock` |
| Lock generation command | `head -2 requirements-dev.lock` |
| CI steps / forced env / Python 3.12 / 10-min timeout | `cat .github/workflows/ci.yml` |
| Makefile targets hard-code `.venv/bin` | `cat Makefile` |
| `requires-python = ">=3.12"`; console script `chronos = "chronos.app:main"` | `grep -n "requires-python\|chronos =" pyproject.toml` |
| ibapi absent from lock; lazy-import guidance | `grep -c ibapi requirements-dev.lock` (expect 0 lines) ; `sed -n '202,206p' src/chronos/broker/official_ibkr.py` |
| aeventkit legitimacy note | `grep -n aeventkit docs/SECURITY.md` |
| Schema version 7; alembic head 0006; fresh-DB-no-alembic rule | `grep -n "SCHEMA_VERSION" src/chronos/persistence/database.py` ; `ls src/chronos/persistence/migrations/versions/` ; `head -4 alembic.ini` |
| node hard-fail under CI | `sed -n '57,68p' tests/safety/test_terminal_client.py` |
| Safety tripwires on live-capable `.env` | `sed -n '17,52p' tests/conftest.py` |

If any re-verification disagrees with this skill, the repo wins — update this file and
re-date it.
