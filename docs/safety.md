# Safety model

Chronos is personal decision-support and paper-trading software. The safest valid result is often
`NO_TRADE` or `MANUAL_REVIEW`.

## Hard boundaries

The MVP cannot transmit live-money orders. Enabling live trading requires a later code change,
new tests, documentation, and explicit human authorization; an environment variable alone is
insufficient. Market orders are rejected. Automated rolling, exercise, assignment handling,
unattended execution, and broker-wide global cancellation are not implemented.

Every new short option requires a verified standard share-only deliverable tied to the exact
underlying contract ID and currency; every short call must additionally be covered by currently
held, unencumbered shares in the same pseudonymous account scope. A qualified multiplier or
matching ticker alone is not deliverable or coverage evidence. Missing, stale, delayed, crossed,
inconsistent, or unauthorized data locks opening actions. Chronos never estimates absent broker
quotes, Greeks, volume, open interest, deliverables, or positions.

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

## Planned kill-switch contract

A later kill switch must block new Chronos orders and attempt to cancel only orders whose
Chronos-owned correlation references are known locally. It must record each request and response
and must never invoke a broker-wide global cancel by default. No kill-switch service is wired in
the current milestone, and every order method remains locked.

## Secrets and logs

Chronos never requests IBKR credentials. TWS or IB Gateway performs authentication. `.env` is
ignored; `.env.example` contains only non-secret placeholders. Account identifiers are masked in
the UI and logging filters. The ledger stores a deterministic pseudonymous fingerprint rather than
the raw account ID; it is a scope check, not encryption or anonymization. SQLite and rotating log
files are restricted to the local owner. Full technical errors stay local in rotating logs.

## Human responsibility

The operator must verify account, symbol, contract, quantity, limit price, obligation, and
coverage. Strategy-adjusted basis is an internal premium allocation and is explicitly not tax
basis. Assignment-pressure labels are heuristics, never probabilities.
