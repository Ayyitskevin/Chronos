# Chronos

Chronos is a local-first, semi-automated dashboard for managing the Wheel Strategy with
Interactive Brokers. It reconciles broker state, explains option candidates, previews risk,
and requires a person to approve every paper order.

Chronos is decision-support software. It is not an autonomous trading bot, investment adviser,
performance-prediction engine, or promise of profitable trading. Options can produce rapid and
substantial losses. Paper fills do not prove live execution quality.

## Current milestone

Milestone 6 adds a locked short-put expiration-risk preview after the read-only candidate boundary.
The operator first captures a candidate evaluation, selects one ranked contract, enters an explicit
total commission estimate, and presses **Refresh evidence & calculate locked risk**. The preview
service does not trust the historical session result: it runs a fresh candidate evaluation
internally, requires the selected contract ID to resolve uniquely in the new eligible set, and
rechecks the exact verified deliverable, chain routing, currency, empty reconciliation scope,
cash-secured finite capital evidence, quote quality, an exact underlying stock contract, and
timestamps against the service clock with a hard 30-second ceiling. Reported option age is added
to elapsed evaluation time at the final decision point. It models one contract from the fresh bid
at observed spot, strike, effective entry, and zero, deduplicating coincident points. The fresh bid
and effective entry are labeled per share; premiums, commission, cash, obligation, and payoff are
labeled as totals. The commission is an operator estimate bounded to 10,000 currency units, four
fractional decimal places, and a compact decimal representation. The output is an expiration
payoff—not a forecast, probability, broker what-if, or order authorization. Broker margin remains
unavailable because no broker order method is called. Every action stays locked, and neither the
candidate nor risk preview is persisted.

Milestone 5 established read-only short-put evaluation behind the reconciliation boundary. It
requests no market data unless a fresh portfolio snapshot proves the entire account exposure-free
and the target symbol uniquely `RECONCILED` and `FLAT`. One serialized market observation then
rechecks account scope and capital values, force-refreshes a bounded put universe, and delegates
every contract-level eligibility and ranking decision to the tested resolver after service
prerequisites pass. Results can say `ELIGIBLE` for decision support, but every candidate and order
action remains
unconditionally locked. No candidate result or evidence is written to SQLite or an audit
repository, and no order method is called.

The operator must press **Run read-only evaluation** to start that observation. Entering the
symbol page, ordinary Streamlit reruns, and changing the selector do not request candidate data.
The UI may retain one presentation-safe result in session memory, labels it historical, clears it
on symbol change or a raised refresh error, and never feeds it back into a service or action.
Configuration defaults to at most 6 expirations by 12 strikes; hard ingress limits allow no more
than 8 expirations, 20 strikes per expiration, or 80 requested option contracts in total.

The optional risk result is also presentation-safe and historical. Changing the symbol, selected
contract, or commission assumption clears it. If a selected contract disappears during the fresh
refresh, the locked result remains visible until the operator changes a control. A fresh candidate
evaluation or failed risk refresh clears prior payoff evidence before work begins, so an old payoff
cannot survive a newer failed request. Ordinary Streamlit reruns perform neither candidate nor risk
refreshes.

The portfolio dashboard still obtains its own read-only reconciliation. Each run double-reads
account values, positions, open orders, and executions, bounds the broker window and full local
evidence read with independent clocks, and compares them with one atomic local transaction.
Unstable or incomplete evidence returns `PENDING`; unresolved exposure returns `MANUAL_REVIEW`.

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

Chronos is pre-release software. Reconciliation runs when the portfolio page renders; startup,
reconnect, order/fill-event, and periodic scheduling are not implemented yet. The concrete local
reader conservatively marks every persisted cycle, strategy state, draft, fill, or basis symbol
unresolved, so only locally empty flat symbols can currently publish `RECONCILED`; positions and
owned working orders remain locked for manual review until complete allocation provenance exists.
The symbol workspace invokes the resolver only through the guarded candidate service. The
default deterministic demo account intentionally contains positions, an order, and an execution,
so it demonstrates the whole-account capital-provenance lock rather than publishing an eligible
AAPL trade; isolated fully flat fixtures exercise the ranking path in tests. Candidate evidence is
not written to a strategy repository because a flat symbol has no legitimate Wheel cycle, and
creating one solely for an evaluation would manufacture strategy state. The dashboard activates
only the one-contract short-put expiration preview; covered-call scenarios, strategy basis,
arbitrary quantities, broker margin, and order what-if are not wired. Stock allocation valuation
still requires a current underlying quote at the service layer. Dividend, borrow, and
corporate-action inputs are optional because the broker port does not provide them yet. The IBKR
adapter records the exact underlying contract ID but does not claim that multiplier metadata
proves an option's complete share-only deliverable. Candidate
evaluation can safely bootstrap a newly connected IBKR session from `UNKNOWN` to rankable market
quality, but it still fails closed before option quote fanout when no qualified contract has a
verified standard deliverable. Deterministic demo contracts carry explicit verified deliverables.
The real-network smoke path was not run without a configured TWS or IB Gateway, and no order can
be submitted from this milestone.

Schema v2 will not silently adopt account-specific rows from an unscoped database, heal a drifted
schema, or fabricate provenance for legacy strategy-basis rows. Startup never upgrades v1 in
place. Preserve and back up any existing v1 file, then configure a fresh `DATABASE_URL` until an
explicit operator-reviewed import exists.
