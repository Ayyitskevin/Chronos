# Chronos

Chronos is a local-first, semi-automated dashboard for managing the Wheel Strategy with
Interactive Brokers. It reconciles broker state, explains option candidates, previews risk,
and requires a person to approve every paper order.

Chronos is decision-support software. It is not an autonomous trading bot, investment adviser,
performance-prediction engine, or promise of profitable trading. Options can produce rapid and
substantial losses. Paper fills do not prove live execution quality.

## Current milestone

Milestone 2 provides the validated configuration and domain layers, deterministic demo broker,
SQLite initialization, structured rotating logs, Streamlit shell, and a strictly read-only
`ib_async` adapter for account, position, execution, open-order, contract, option-chain, and
bounded market-data access. Candidate resolution, complete scenario analysis, and paper-order
transmission remain disabled until their guarded milestones are implemented.

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

The separately marked IBKR smoke test is skipped by default and remains strictly read-only.
See [docs/ibkr_setup.md](docs/ibkr_setup.md) for the opt-in wrapper and required TWS/Gateway
configuration.

## Documentation

- [Architecture](docs/architecture.md)
- [Safety model](docs/safety.md)
- [Financial formulas](docs/formulas.md)
- [IBKR setup](docs/ibkr_setup.md)

## Current limitations

Chronos is pre-release software. The current milestone can read IBKR state and market data, but
the real-network smoke path was not run without a configured TWS or IB Gateway. Chronos does not
yet rank option candidates, calculate a strategy-adjusted basis, reconcile restart state, or
submit any order. Those capabilities are introduced only alongside their reconciliation and
guardrail tests.
