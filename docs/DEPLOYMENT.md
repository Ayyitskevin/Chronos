# Deployment — Reproducible Local Setup

Target: one Linux machine, one operator, Python 3.12. There is no server, container, or cloud
component for the trading path.

## Prerequisites

- Python 3.12 (`requires-python = ">=3.12"` in `pyproject.toml`).
- git.
- For any broker connectivity: TWS or IB Gateway installed and operated by you
  (docs/IBKR_RUNBOOK.md). Not needed for research/backtest.

## Install

```bash
git clone <your-remote> Chronos && cd Chronos
python3 -m venv .venv                                        # python 3.12+
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m pip install -e . --no-deps               # the project itself
```

This is the same pinned, hash-verified path CI uses (`.github/workflows/ci.yml`):
`requirements-dev.lock` pins the full runtime+dev transitive closure to exact versions and
SHA-256 hashes (docs/SECURITY.md). A quick unpinned dev install
(`pip install -e '.[dev]'`) also works but is not reproducible — prefer the lock for
anything you intend to keep running.

Verify the toolchain the same way CI does:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/chronos
.venv/bin/pytest -q
```

Reproducibility record: even with the lock, record the resolved environment at deployment
time (the build backend and pip itself are outside the hash gate — docs/SECURITY.md):

```bash
.venv/bin/pip freeze > deploy-freeze-$(date +%F).txt   # keep with your backups
```

## Initialize directories and state

The platform creates `data/` on demand (halt store, ledger, and audit log all `mkdir -p` their
parent), but creating things explicitly keeps first-run behavior obvious:

```bash
mkdir -p data logs
.venv/bin/python scripts/initialize_database.py   # wheel dashboard DB (data/chronos.db)
.venv/bin/python -m chronos.cli status            # platform: shows HALTED (NEVER_ARMED)
```

A fresh deployment reports `TRADING HALTED | reason: NEVER_ARMED`. That is correct: a new
deployment starts halted until the operator arms it once
(`src/chronos/control/halt.py`). Note the CLI's default paths (`data/platform_halt.json`,
`data/platform_audit.jsonl`) are relative to the current directory — run it from the repository
root, or pass `--halt-file`/`--audit-file` explicitly.

## Environment variables

All loaded from the environment or `.env` by `src/chronos/config/settings.py` (pydantic-settings;
unknown variables ignored, invalid values refuse startup). These settings drive the wheel
dashboard subsystem; the platform CLI reads none of them (it takes paths and a YAML risk policy).
Safe defaults below are the values with no `.env` at all.

| Variable | Default | Notes |
|---|---|---|
| `BROKER_MODE` | `demo` | `demo` or `ibkr`. Demo is deterministic, no network. |
| `DEMO_PROFILE` | `safety_cases` | `safety_cases` or `empty_account`. |
| `IB_ENVIRONMENT` | `paper` | `paper` or `live`. `live` + transmission without `ALLOW_LIVE_TRADING` raises (ambiguous intent); the full ADR-0009 conjunction is required for a live-capable config. |
| `IB_HOST` | `127.0.0.1` | Keep loopback. |
| `IB_PORT` | `7497` | TWS paper. IB Gateway paper is 4002. |
| `IB_CLIENT_ID` | `17` | Must be unique per connected API client. |
| `IB_ACCOUNT_ID` | empty | Required before paper order transmission. |
| `ALLOW_ORDER_TRANSMIT` | `false` | Paper transmission opt-in (wheel subsystem). |
| `ALLOW_LIVE_TRADING` | `false` | *(Corrected 2026-07-25.)* `true` is honored only under the full ADR-0009 nine-conjunct configuration, else startup refuses naming every unmet conjunct. At run time an order still walks the ten-gate live stack; autonomous operation additionally requires an active AutonomyMandate (ADR-0016). |
| `ALLOW_OUTSIDE_RTH` | `false` | |
| `SYMBOL_ALLOWLIST` | `AAPL,MSFT,SPY` | Comma-separated, alphanumeric, no duplicates. |
| `DATABASE_URL` | `sqlite:///data/chronos.db` | Wheel ledger only (platform ledger is separate). |
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL. |
| `LOG_FILE` | `logs/chronos.log` | Rotating local file. |
| `MARKET_TIMEZONE` | `America/New_York` | Must be an installed IANA zone. |

The remaining variables in `.env.example` (delta/DTE bands, candidate caps, resolver weights,
assignment heuristics) tune the wheel dashboard's candidate logic; their defaults are validated
ranges in `settings.py`. The test-only flag `CHRONOS_RUN_IBKR_SMOKE=1` opts in to the read-only
IBKR smoke test (docs/TEST_PLAN.md).

## Running things

Research / backtest CLI (no broker, no network):

```bash
.venv/bin/python -m chronos.cli backtest --strategy regime_trend_v1 --symbol SPY \
  --data-dir research/data/raw --policy config/risk.example.yaml --cash 3000 --slippage-bps 2
```

Requires `research/data/raw/SPY.csv` (the data-acquisition pipeline populates this directory with
a provenance `MANIFEST.json`; as of this writing it is being produced — an absent file fails with
a clear error). A one-shot shadow evaluation of the latest closed bars (no orders possible):

```bash
.venv/bin/python -m chronos.cli shadow-scan
```

Full command list: docs/IBKR_RUNBOOK.md section 8.

Wheel dashboard (existing README):

```bash
.venv/bin/streamlit run src/chronos/app.py
```

Streamlit binds locally; do not expose it.

## No container for the trading path — deliberate

No Dockerfile or compose file is provided for the trading path, on purpose:

- TWS/IB Gateway is a GUI application requiring interactive login and 2FA; containerizing the
  Python side splits it from the gateway across a network boundary and encourages exposing the
  API socket beyond localhost.
- The platform's safety posture depends on durable local files (`data/platform_halt.json`,
  ledger, audit log); container filesystem lifecycles make it too easy to destroy halt state by
  recreating a container.
- One venv on one machine is simpler to reason about, back up, and audit for a single operator.

If you containerize the research-only path yourself someday, never mount the same `data/`
directory into more than one running instance (SQLite + halt-file semantics assume one process).

## Shadow/paper service

*(Corrected 2026-08-02. This section previously read "Future work — shadow/paper service (NOT
IMPLEMENTED)" and stated that no service entry point existed. That was true of an earlier build
and is false today: `python -m chronos.service` ships and is tested —
`src/chronos/service/__main__.py`, `tests/platform_unit/test_service.py`.)*

The entry point runs the startup gate (halt read, ledger hydration, reconciliation) and then one
or more decision cycles:

| Flag | Default | Meaning |
|---|---|---|
| `--mode` | `shadow` | `shadow` or `paper` only. Live/canary are refused in code and not selectable. |
| `--watch` | off | Loop instead of running once. Without it the process runs a single cycle and exits. |
| `--interval` | `3600.0` | Seconds between cycles when `--watch` is set. |
| `--symbols` | `SPY,QQQ` | Comma-separated. |
| `--strategies` | `regime_trend_v1,mean_reversion_v1` | |
| `--equity` | `3000.0` | **Carries the superseded ~USD 3,000 premise** — the last documented account snapshot is ≈ USD 110 and the capital question is an open owner decision (ASSUMPTIONS A-10). Pass `--equity` explicitly rather than accepting this default. |
| `--policy` | `config/risk.example.yaml` | |
| `--halt-file` / `--audit-file` / `--data-dir` | see `--help` | |

Default mode is SHADOW (capability `NO_ORDERS`): the loop reports would-be intents and cannot
submit. SIGINT/SIGTERM request a clean stop after the current cycle; an unexpected exception
persists a `STRATEGY_EXCEPTION` halt and exits nonzero; a restart re-enters startup and never
resumes trading automatically.

What still does **not** exist is live bar ingestion wired into this loop, so it is not yet a
substitute for supervised operation, and **no real gateway has ever been connected** in this
project's history (`docs/VISION_COMPLETION_PLAN.md` §2). Daemonizing it is therefore a reviewed
step, not a default. If and when that review happens, a systemd user unit would be the shape of
it — the `ExecStart` line below is now a valid command:

```ini
# ~/.config/systemd/user/chronos-shadow.service
# Do not enable without the review described above.
[Unit]
Description=Chronos shadow-mode service
After=network-online.target

[Service]
WorkingDirectory=%h/Chronos
ExecStart=%h/Chronos/.venv/bin/python -m chronos.service --mode shadow --watch
Restart=on-failure
# The process re-reads the persistent halt file on start; restart never clears a halt.

[Install]
WantedBy=default.target
```

Everything else is run manually in the foreground, which remains appropriate for the current
research/backtest phase.
