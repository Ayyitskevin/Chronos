# Safety model

Chronos is personal decision-support and paper-trading software. The safest valid result is often
`NO_TRADE` or `MANUAL_REVIEW`.

## Hard boundaries

The MVP cannot transmit live-money orders. Enabling live trading requires a later code change,
new tests, documentation, and explicit human authorization; an environment variable alone is
insufficient. Market orders are rejected. Automated rolling, exercise, assignment handling,
unattended execution, and broker-wide global cancellation are not implemented.

Every short call must be covered by currently held, unencumbered shares using the qualified
contract multiplier. Missing, stale, delayed, crossed, inconsistent, or unauthorized data locks
opening actions. Chronos never estimates absent broker quotes, Greeks, volume, open interest, or
positions.

## Order boundary

Paper submission remains off unless all of these are true at the same decision point:

1. A fresh broker reconciliation succeeded.
2. The connected account matches the configured, masked account.
3. The environment is PAPER and the transmission setting is explicitly enabled.
4. A newly fetched quote is live or frozen, fresh, valid, and complete.
5. Duplicate, conflict, capital, concentration, and covered-call checks pass.
6. A broker what-if preview succeeded when supported.
7. The user typed the symbol, confirmed quantity, and chose the final submit action.

Any ambiguity fails closed. The lifecycle is append-only:
`DRAFT -> VALIDATED -> WHAT_IF_PREVIEWED -> USER_CONFIRMED -> SUBMITTED`, followed by a broker
outcome. The UI cannot skip states.

## Kill switch

The kill switch blocks new Chronos orders and attempts to cancel only orders whose Chronos-owned
correlation references are known locally. It records each request and response. It never invokes
a broker-wide global cancel by default.

## Secrets and logs

Chronos never requests IBKR credentials. TWS or IB Gateway performs authentication. `.env` is
ignored; `.env.example` contains only non-secret placeholders. Account identifiers are masked in
the UI and logging filters. Full technical errors stay local in rotating logs.

## Human responsibility

The operator must verify account, symbol, contract, quantity, limit price, obligation, and
coverage. Strategy-adjusted basis is an internal premium allocation and is explicitly not tax
basis. Assignment-pressure labels are heuristics, never probabilities.
