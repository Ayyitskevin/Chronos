# Chronos

Chronos is a local-first, semi-automated dashboard for managing the Wheel Strategy with
Interactive Brokers. It reconciles broker state, explains option candidates, previews risk,
and requires a person to approve every paper order.

Chronos is decision-support software. It is not an autonomous trading bot, investment adviser,
performance-prediction engine, or promise of profitable trading. Options can produce rapid and
substantial losses. Paper fills do not prove live execution quality.

## Current milestone

Milestone 1 provides the validated configuration layer, typed domain and broker boundaries,
deterministic demo broker, SQLite schema initialization, structured rotating logs, and a
Streamlit shell. IBKR integration, candidate resolution, complete scenario analysis, and paper
order transmission are intentionally disabled until their guarded milestones are implemented.

## Safety posture

- Live-money order transmission is hard-disabled in code.
- Demo mode is the default and needs no brokerage account.
- Chronos never asks for or stores an IBKR username or password.
- Missing broker data is represented as missing; it is never fabricated.
- Market orders, unattended execution, and naked short calls are outside the MVP boundary.
- Order transmission defaults off, including for paper accounts.

See [docs/safety.md](docs/safety.md) for the threat model and fail-closed rules.

## Setup

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python scripts/initialize_database.py
```

Do not commit `.env`; it is ignored. The defaults launch deterministic demo mode:

```bash
.venv/bin/streamlit run src/chronos/app.py
```

Or use:

```bash
.venv/bin/python scripts/run_demo.py
```

## Wheel state model

Broker positions, open orders, executions, and the Chronos ledger reconcile into one of:
`FLAT`, `SHORT_PUT_PENDING`, `SHORT_PUT_OPEN`, `LONG_STOCK`, `SHORT_CALL_PENDING`,
`SHORT_CALL_OPEN`, `CLOSING`, or `MANUAL_REVIEW`. UI selections never own strategy state.

`MANUAL_REVIEW` is the safe outcome when partial assignment, a corporate action, a manual
trade, or an unexplained mismatch makes the state ambiguous.

## Development gates

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/chronos
```

The separately marked IBKR smoke test will be skipped by default and must remain read-only.
See [docs/ibkr_setup.md](docs/ibkr_setup.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Safety model](docs/safety.md)
- [Financial formulas](docs/formulas.md)
- [IBKR setup](docs/ibkr_setup.md)

## Current limitations

Chronos is pre-release software. The current milestone does not connect to IBKR, resolve an
option chain, calculate a strategy-adjusted basis, or submit any order. Those capabilities are
introduced only alongside their reconciliation and guardrail tests.
