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
| `IB_ENVIRONMENT` | `paper` | `paper` or `live`. Live + transmission raises. |
| `IB_HOST` | `127.0.0.1` | Keep loopback. |
| `IB_PORT` | `7497` | TWS paper. IB Gateway paper is 4002. |
| `IB_CLIENT_ID` | `17` | Must be unique per connected API client. |
| `IB_ACCOUNT_ID` | empty | Required before paper order transmission. |
| `ALLOW_ORDER_TRANSMIT` | `false` | Paper transmission opt-in (wheel subsystem). |
| `ALLOW_LIVE_TRADING` | `false` | Setting `true` raises: hard-disabled. |
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

## Future work — shadow/paper service (NOT IMPLEMENTED)

There is currently no long-running service entry point: nothing wires live bar ingestion, a broker
adapter, reconciliation evidence gathering, and the mode lock into a daemon. What exists today is
the one-shot `shadow-scan` CLI command above (run manually after the close). The CLI docstring
references a separate paper-capable service entry point; it does not exist in this build. The
components exist and are tested individually. When such a service is written and reviewed, a
systemd user unit like the following would be the shape of it — do not create this unit today, it
has nothing to run:

```ini
# FUTURE WORK — no such entry point exists in this build.
# ~/.config/systemd/user/chronos-shadow.service
[Unit]
Description=Chronos shadow-mode service (future)
After=network-online.target

[Service]
WorkingDirectory=%h/Chronos
ExecStart=%h/Chronos/.venv/bin/python -m chronos.service --mode shadow   # does not exist
Restart=on-failure
# The process re-reads the persistent halt file on start; restart never clears a halt.

[Install]
WantedBy=default.target
```

Until then, everything is run manually in the foreground, which is appropriate for the current
research/backtest phase.
