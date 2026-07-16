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

The first read-only short-put service treats capital provenance as a hard prerequisite. It will
not request candidate market data unless fresh reconciliation proves the entire account has no
positions, open orders, or executions and the target symbol is uniquely flat. It never infers zero
portfolio allocation from a flat ticker, substitutes buying power for cash, or creates a fake
Wheel cycle to persist an evaluation. A ranked result is evidence for a person to inspect, not
authorization to trade; opening actions remain locked even when the resolver says `ELIGIBLE`.
Candidate work begins only after the operator presses the explicit read-only evaluation button;
page entry, ordinary reruns, and symbol changes issue no candidate request. The one optional
session-memory result is labeled historical, is cleared on symbol change or a raised refresh
error, and is never accepted as service or order evidence.

Candidate narrowing is bounded before broker fanout: settings and narrowing cap the universe at 8
expirations, 20 strikes per expiration, and an 80-contract product; qualification and quote entry
points also reject any sequence over 80 contracts. Quote evidence must match its qualified
contract exactly, not merely share a contract ID, and contracts without a verified standard
deliverable are removed before quote requests. A new broker session may improve from `UNKNOWN` to
live or frozen quality during its first request; end-of-window status and each quote must still
pass the rankable-quality and freshness gates.

The expiration-risk preview does not trust the candidate stored in UI session memory. A request
contains only a symbol, contract-ID selection hint, and explicit finite nonnegative total
commission estimate. The service obtains a fresh candidate evaluation internally and withholds the
preview unless that ID resolves uniquely to a newly eligible one-contract short put with coherent
contract and exact chain routing, verified deliverable, currency, quote quality, and quote age. It
also bounds candidate, underlying, reconciliation, and account timestamps against its service
clock and a hard 30-second ceiling, adds reported option age to elapsed evaluation time, requires
the underlying evidence to be an exact stock contract, and re-proves the whole account empty and
the target uniquely reconciled and flat. Finite cash must be at least the gross obligation, and the
capital totals and allocation percentages must be internally consistent. Changing the symbol,
selected contract, commission assumption, or candidate generation clears the stored risk result.
If the contract disappears during refresh, its locked reason remains visible until a control
changes. A refresh failure clears the prior candidate and payoff evidence before it can be shown as
current.

Risk output uses the fresh bid only as a labeled hypothetical credit and models expiration points
at observed spot, strike, effective entry, and zero. The commission is visibly an operator
estimate; explicit zero is visibly fees-excluded. The model is not a forecast, probability,
broker-margin estimate, order what-if, or authorization. It fixes quantity at one, persists
nothing, creates no cycle or draft, calls no broker order method, and keeps opening actions locked.
Commission input is capped at 10,000 currency units, four normalized fractional decimal places, 16
decimal digits, and 32 UI characters; non-finite chart coordinates are withheld. Per-share and
one-contract total amounts are labeled separately.

The next boundary is a deterministic DEMO what-if rehearsal, not an order authorization. It rejects
the attempt before fresh-risk or broker calls unless both settings and the concrete adapter are
DEMO. It reruns the full risk gate, rebinds resolver and capital policy to current settings, limits
input to one positive tick-aligned contract price inside the refreshed spread, and validates the
first account/exposure observation before exactly one non-transmitting demo preview. A second
observation then detects drift. Any identity, capital, exposure, clock, request-echo, commission, or
margin ambiguity withholds the result.

The successful receipt has rehearsal status `WHAT_IF_PREVIEWED`; this is not an order-lifecycle
transition. It carries no raw account ID or broker request and replaces broker messages with a
warning count. It uses the broker's deterministic commission estimate for exact-limit payoff math
while displaying the operator-assumption variance. The limit input is capped at 1,000,000 currency
units, four normalized fractional places, 16 decimal digits, and 32 UI characters. Receipt and
parent evidence live only in session memory. There is no persistence, draft, guardrail decision,
user-confirmation control, submission control, or IBKR what-if path.

Milestone 8 adds one more DEMO rehearsal, not a user-confirmation boundary. A fourth explicit action
requires the operator to type the exact canonical symbol and strict quantity one, affirm the exact
contract ID, limit, and gross assignment obligation from the current receipt, and make an explicit
risk acknowledgement. The request carries only those scalar hints. The service reruns the complete
Milestone 7 what-if boundary and compares the fresh result with every affirmation; it does not
accept the UI-held receipt as evidence.

A successful approval rehearsal ends at `APPROVAL_REHEARSED`, a rehearsal-specific status that is
never `OrderLifecycle.USER_CONFIRMED`. It grants no confirmation authority and leaves the UI at
`Progression: STOPPED` with actions `LOCKED`. It persists nothing, creates no cycle, draft,
guardrail decision, or lifecycle transition, and invokes no submit, modify, or cancel method. There
is no IBKR approval path. On success, the UI discards all M5-M7 parent evidence and typed approval
widgets, retaining only a standalone scalar receipt with no full option contract, broker
descriptive text, raw account identity, or broker-margin output. Milestone 9 wraps that receipt in
the session-only envelope below. A new ancestor attempt or workspace-symbol change terminally
supersedes its terms. Ordinary reruns perform no approval refresh or related broker call, and a
failed newer attempt retains only sanitized feedback rather than leaving older evidence looking
current.

Milestone 9 bounds how long that scalar receipt can remain visible in one process session. The
default display lease is exactly 15 minutes on a monotonic process clock; it is not evidence
freshness and never extends approval authority. Expiry, explicit operator abandonment, or a newer
ancestor evidence attempt produces `EXPIRED`, `ABANDONED`, or `SUPERSEDED`, respectively. Those are
presentation dispositions, never order-lifecycle transitions.

Every terminal transition drops the receipt itself. Its tombstone retains no business or evidence
fields beyond the canonical approval reference, symbol, monotonic timing, a bounded reason enum,
and literal false/locked safety flags—no option contract, limit, obligation, broker text, account
identity, or parent evidence. Clock regression fails closed to expiry, the first terminal
disposition cannot be reopened or changed, and an exact replay cannot extend the lease. These
transitions invoke no broker or financial service and write no persistent state. Session restart
clears all records.

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
outcome. The UI cannot skip states. This is the contract for a later PAPER submission path; the
current DEMO rehearsal neither enters nor advances this lifecycle.

## Planned kill-switch contract

A later kill switch must block new Chronos orders and attempt to cancel only orders whose
Chronos-owned correlation references are known locally. It must record each request and response
and must never invoke a broker-wide global cancel by default. No kill-switch service is wired in
the current milestone. The UI exposes only the deterministic `DemoBroker` what-if and approval
rehearsals described above. No IBKR preview, approval, submission, modification, or cancellation is
exposed, and no order-lifecycle confirmation state follows either DEMO result. The expiration-risk
preview and rehearsals remain decision support; live-money submission stays hard-disabled.

## Secrets and logs

Chronos never requests IBKR credentials. TWS or IB Gateway performs authentication. `.env` is
ignored; `.env.example` contains only non-secret placeholders. Account identifiers are masked in
the UI and logging filters. The ledger stores a deterministic pseudonymous fingerprint rather than
the raw account ID; it is a scope check, not encryption or anonymization. SQLite and rotating log
files are restricted to the local owner. Broker errors crossing the UI boundary are reduced to a
generic operator message and an exception-type event; their raw text is written to neither the UI
nor the application log.

## Human responsibility

The operator must verify account, symbol, contract, quantity, limit price, obligation, and
coverage. Strategy-adjusted basis is an internal premium allocation and is explicitly not tax
basis. Assignment-pressure labels are heuristics, never probabilities.
