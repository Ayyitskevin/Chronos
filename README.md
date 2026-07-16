# Chronos

Chronos is a local-first, semi-automated dashboard for managing the Wheel Strategy with
Interactive Brokers. It reconciles broker state, explains option candidates, previews risk,
and requires a person to approve every paper order.

Chronos is decision-support software. It is not an autonomous trading bot, investment adviser,
performance-prediction engine, or promise of profitable trading. Options can produce rapid and
substantial losses. Paper fills do not prove live execution quality.

## Current milestone

Milestone 3 adds the pure Wheel state engine, transparent option resolver, capital checks,
Decimal scenario calculations, assignment-pressure heuristic, and append-only
Strategy-Adjusted Basis ledger. Candidate decisions have stable rejection codes and return
typed `NO_TRADE`; unsafe state or basis ambiguity returns `MANUAL_REVIEW`. The dashboard wiring,
startup reconciliation coordinator, and paper-order workflow remain locked for later guarded
milestones.

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

Chronos is pre-release software. The current strategy engines are tested but are not yet wired
into the dashboard or a complete startup/reconnect reconciliation coordinator. Stock allocation
valuation still requires a current underlying quote at the service layer. Dividend, borrow, and
corporate-action inputs are optional because the broker port does not provide them yet. The
IBKR adapter records the exact underlying contract ID but does not claim that multiplier metadata
proves an option's complete share-only deliverable. It therefore fails closed on every new IBKR
short-option candidate until a trustworthy deliverable source verifies a standard contract;
deterministic demo contracts carry explicit verified deliverables. The real-network smoke path
was not run without a configured TWS or IB Gateway, and no order can be submitted from this
milestone.

Schema v2 will not silently adopt account-specific rows from an unscoped database, heal a drifted
schema, or fabricate provenance for legacy strategy-basis rows. Startup never upgrades v1 in
place. Preserve and back up any existing v1 file, then configure a fresh `DATABASE_URL` until an
explicit operator-reviewed import exists.
