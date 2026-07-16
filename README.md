# Chronos

Chronos is a local-first, semi-automated dashboard for managing the Wheel Strategy with
Interactive Brokers. It reconciles broker state, explains option candidates, previews risk,
and requires a person to approve every paper order.

Chronos is decision-support software. It is not an autonomous trading bot, investment adviser,
performance-prediction engine, or promise of profitable trading. Options can produce rapid and
substantial losses. Paper fills do not prove live execution quality.

## Current milestone

Milestone 9 adds an immutable, process-memory-only disposition envelope around the scalar DEMO
approval receipt. A retained receipt has a fixed 15-minute display lease measured with the process
monotonic clock; this lease is presentation hygiene, not market-evidence freshness or approval
authority. At the exact deadline, the envelope becomes `EXPIRED`. The operator can instead choose
**Abandon historical DEMO rehearsal receipt**, which produces `ABANDONED`, and any newer ancestor
evidence attempt produces `SUPERSEDED` before that work begins.

Each terminal disposition replaces the receipt with a tombstone containing no business or evidence
fields beyond its canonical reference, symbol, bounded timing, disposition reason, and literal
false/locked safety flags. Contract identity, limit, obligation, and every other receipt term are
physically dropped. The first terminal disposition wins,
ordinary reruns only apply the pure display lease, and a failed newer attempt cannot restore the
old receipt. These presentation statuses are not `OrderLifecycle` values. They create no authority,
persistence, broker request, cycle, draft, guardrail decision, lifecycle transition, or order
control; restarting the session loses them.

Milestone 8 established a fresh-evidence, ephemeral DEMO approval rehearsal after the locked what-if
boundary. It is a fourth explicit operator action, available only after a current Milestone 7
receipt. The operator must type the exact canonical symbol and quantity `1`, then affirm the exact
option contract ID, limit credit, and gross assignment obligation shown by the current receipt,
together with a risk acknowledgement. Pressing **Refresh evidence & rehearse locked DEMO
approval** supplies only those scalar hints to the service; the service reruns the complete
Milestone 7 boundary rather than treating any candidate, risk, what-if, or UI session object as
authority.

A successful result stops at the rehearsal-specific status `APPROVAL_REHEARSED`. It is explicitly
not `OrderLifecycle.USER_CONFIRMED`, confirmation authority, or permission to submit. The workspace
continues to display `Progression: STOPPED` and keeps actions `LOCKED`. On success, the workspace
discards the candidate, risk, what-if, and typed-approval widgets and retains only a strict scalar
receipt in process memory. That receipt carries no full option contract, broker descriptive text,
or parent result. It persists no evidence, creates no Wheel cycle, draft, guardrail decision, or
lifecycle transition, and calls no submit, modify, or cancel method. There is no IBKR approval path.
Live trading remains hard-disabled.

Milestone 7 established the deterministic short-put order what-if rehearsal. It is available only
when both configuration and the concrete adapter are DEMO. The operator first obtains a current
`READY` risk result, then enters an explicit one-contract limit credit inside the fresh bid/ask and
aligned to the contract tick. Pressing **Refresh evidence & run locked DEMO what-if** repeats the
complete Milestone 6 risk refresh; no session-memory candidate or risk object is accepted as
authority.

One serialized broker window then double-reads connection, account, positions, open orders,
executions, and server time around exactly one deterministic `preview_order` call. Chronos requires
the account and exposure to remain identical, the echoed non-transmitting request to match exactly,
and the commission, margin, and timestamp evidence to be complete and finite. The
presentation-safe receipt stops at `WHAT_IF_PREVIEWED`, replaces broker text with a generic warning
count, and uses the broker commission estimate to recompute the exact-limit expiration payoff. It
contains no raw account ID or broker request. No candidate, risk result, receipt, Wheel cycle,
order draft, or guardrail decision is persisted; there is no confirmation or submit control. The
IBKR adapter's order methods and every submission, modification, or cancellation path remain
unconditionally locked.

Milestone 6 established the locked short-put expiration-risk preview. It freshly revalidates the
selected contract, verified deliverable, chain routing, currency, empty reconciliation scope,
cash-secured finite capital evidence, quote quality, exact underlying stock contract, and bounded
timestamps before modeling one contract from the fresh bid. Its output remains an expiration
payoff—not a forecast, probability, broker what-if, or order authorization.

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

The optional risk and DEMO what-if results are presentation-safe historical evidence. Changing any
parent input or evidence generation clears every dependent child; changing the selected contract,
commission assumption, or limit also clears its descendants. A successful approval rehearsal
replaces that lineage and its typed widgets with only the retained scalar receipt envelope
described above. Starting a new ancestor attempt or changing the workspace symbol terminally
supersedes its terms. A fresh failed attempt retains only bounded, sanitized feedback and cannot
reopen the tombstone. Ordinary Streamlit reruns perform no candidate, risk, what-if, or approval
refresh and issue no related broker call.

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

The default `DEMO_PROFILE=safety_cases` shows a deliberately conflicted portfolio and keeps the
opening journey locked. To exercise the complete candidate → risk → what-if →
approval-rehearsal decision-support path against an explicitly empty local fixture, set
`DEMO_PROFILE=empty_account` in the untracked `.env` and restart Chronos. This changes only
deterministic fixtures; submission remains impossible.

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
default `safety_cases` demo profile intentionally contains positions, an order, and an execution,
so it demonstrates the whole-account capital-provenance lock rather than publishing an eligible
AAPL trade. The supported `empty_account` profile exposes one coherent AAPL path with zero broker
exposure for the locked M5–M9 journey. Candidate evidence is
not written to a strategy repository because a flat symbol has no legitimate Wheel cycle, and
creating one solely for an evaluation would manufacture strategy state. The dashboard activates
only the one-contract short-put expiration preview, deterministic DEMO what-if, and ephemeral DEMO
approval rehearsal; covered-call scenarios remain blocked on complete stock-allocation provenance,
and strategy basis, arbitrary quantities, real-broker margin, IBKR order what-if, and IBKR approval
are not wired. Stock allocation valuation
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
