# ADR-0036 — Authenticated admission of filled QQQ PAPER openings

Status: **accepted design — owner-gated at merge, 2026-08-25. Default-off and
not runtime-wired; no PAPER or LIVE authority.** Index entry: DECISIONS.md D-50.

## Context

ADR-0035 deliberately left two activation blockers: typed callers could attest
opening-fill evidence without a reviewed broker adapter, and nothing enforced a
one-opening-order-to-one-managed-stream identity. Calling the management state
machine directly would therefore let a structurally valid object stand in for
broker truth.

IBKR also does not offer a single historical query whose absence proves an order
never filled. Execution-query history is bounded by the current TWS/IB Gateway
session and configuration, while active/open-order queries omit cancelled and
fully filled orders. Broker positions are account/contract aggregates rather
than Chronos-strategy lots. Admission must therefore require positive execution
evidence and reconcile it with Chronos's own terminal lifecycle and the complete
aggregate position; it may never infer a fill from an order disappearing.

## Decision

### 1. One narrow operation derives every economic fact

`ManagedPositionAdmission.admit_opening(opening_order_ref, now)` is the sole
public operation. Its caller supplies only a Chronos opening-order reference and
an aware clock value. Account, environment, contract, side, quantity, protected
entry price, risk evidence, executions, and position facts come from bound local
state or the canonical read-only broker port.

The local intent must be a risk-bound, terminal PAPER QQQ stock `BUY OPEN`. A
locally `FILLED` order must reconcile to its full intended quantity; a
`CANCELLED` order may admit only its proven terminal partial fill. Fractional
shares refuse.

### 2. Entry risk is authorizing evidence, not inert metadata

The order risk evidence provider may attach a versioned
`QQQPositionManagementRiskEvidence`. When present, the ordinary order risk
engine independently applies the exact D-48 candidate identity, 1%/USD 30 native
stop budget, 1.5%/USD 45 CVaR budget, USD 3,000 gross/capital ceiling, and frozen
signal geometry to the protected entry terms. A named
`qqq_position_management_risk` check must pass and is persisted with the risk
decision.

Admission accepts no replacement risk values. It revalidates the stored decision,
named check, candidate/source evidence, timing, entry-reconciliation provenance,
and risk projection. The managed plan is then derived from actual fill quantity
and VWAP while preserving the frozen signal-time risk distance. If actual broker
truth breaches the envelope, ADR-0035's state records the exposure and latches a
flatten proposal rather than hiding the position.

### 3. Admission requires one stable, positive broker proof

The service captures two consecutive read-only snapshots through Chronos's
canonical `Broker` interface: connection status and server time around positions,
executions, and open orders. Admission refuses unless:

- reconciliation is ready before and after, with the same session, generation,
  and reconciliation timestamp;
- both broker reads identify the same connected PAPER managed account and stable
  economic state;
- one or more positive executions identify the exact account, order reference,
  QQQ contract, buy side, positive broker order ID, positive permanent ID, and
  valid time window;
- those identities and cumulative fill quantity agree with Chronos's durable
  order lifecycle;
- the opening order is no longer active, no other QQQ order can change the same
  aggregate, and the broker QQQ position equals already-managed remaining
  quantity plus this candidate fill.

Missing executions always refuse. Absence from open orders is corroborating
state, never fill evidence.

### 4. Schema v11 makes binding atomic and replayable

`managed_position_bindings` stores only the account fingerprint and enforces
unique `(account_fingerprint, opening_order_ref)` and
`(account_fingerprint, position_id)` identities. It binds the local risk decision,
broker/permanent order identities, fill/risk/candidate/policy digests, and
reconciliation generation/session.

The binding row and ADR-0035 hash-chain registration commit in one transaction.
The position ID is deterministically derived from account fingerprint and opening
reference. Exact retries rehydrate the same stream without another broker read;
contradictory or corrupt durable state returns a typed refusal. Migration 0010
creates an empty table and performs no historical backfill because old rows lack
the required authenticated evidence.

### 5. The capability remains inert

No production module imports `position_admission`; it imports no concrete IBKR,
submission, execution, runtime, preview, modify, or cancel capability. It cannot
schedule itself, submit a management proposal, place protection, or create an
authority grant. A future runtime integration is a separate owner-gated change.

## Consequences

ADR-0035 activation blockers 1 and 2 are code-mitigated: the opening registration
now has a reviewed broker-read seam and a database-enforced unique binding. They
are not operationally closed without a real PAPER gateway run.

The remaining blockers are unchanged and material: authenticated ongoing
management observations/outcomes, a trusted retry-safe management queue,
broker-held protective semantics across disconnects and gaps, bounded scheduling,
and real PAPER restart/ambiguous-send evidence. IBKR's bounded execution-history
window and account-level position aggregation also mean admission can refuse when
truth is incomplete; it cannot reconstruct missing history or assign unexplained
manual exposure to Chronos.

## Broker-source notes

- [IBKR execution and commission reporting](https://interactivebrokers.github.io/tws-api/executions_commissions.html)
- [IBKR open-order behavior](https://interactivebrokers.github.io/tws-api/open_orders.html)
- [IBKR account-position aggregation](https://interactivebrokers.github.io/tws-api/positions.html)
- [IBKR paper-account limitations](https://ibkrcampus.com/campus/glossary-terms/paper-trading-account/)
