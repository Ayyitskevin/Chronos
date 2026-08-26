# QQQ campaign readiness — owner and Chronos checklist

Status: **blocked before the first market-data read**. The machine-readable source is
`specs/qqq_campaign_readiness_v1.json`; `compile_qqq_campaign_readiness()` authenticates it
and the referenced repository artifacts. It cannot open data, register a trial, unlock a
holdout, contact a broker, construct or submit an order, or promote a strategy.

## Already locked in the repository

- QQQ v1 constitution and its zero-authority posture.
- Five-cell SMA control preregistration.
- Integrated Five-Tool Confluence candidate overlay.
- Default-off PAPER management policy and state machine.
- Default-off managed-position opening admission.

These are implementation identities, not evidence. The PAPER modules remain inert, have no
runtime consumer, and do not provide broker-held protection.

## What Kevin must provide

Do not send credentials, account identifiers, or market-data files through chat or commit
them to the repository.

1. Merge approval for the exact readiness identity after independent review.
2. Run the owner-only read-only six-symbol export in
   [certified_data_runbook.md](certified_data_runbook.md) for QQQ, SPY, IWM, DIA, GLD, and
   TLT.
3. Independently sample splits and dividends and complete the corporate-action attestation.
4. Approve a complete clean/seen/burned holdout map without opening the clean partition.
5. Approve the volatility-matched QQQ/cash benchmark definition and cash-leg source.
6. Approve the long-side all-in cost sources and assumptions.
7. Export the pinned TradingView traces needed for signal and execution-parity comparison.

None of those actions authorizes a trial or an order. The certified export and attestation
must exist before Chronos can freeze a release.

## What Chronos builds after those inputs

1. Certify and freeze the six-symbol release and ordinary catalog.
2. Freeze power-required N and the earliest possible pass date before opening data.
3. Bind evaluator, criteria, code commit, registry, and campaign identities.
4. Produce content-addressed TradingView parity evidence or preserve parity as a blocker.
5. Run the SMA control first. The Confluence candidate remains a separate campaign and may
   proceed only after the control result and its own base Five-Tool blockers are resolved.

## Two data identities must not be blended

The QQQ robustness release is the six-symbol set `QQQ, SPY, IWM, DIA, GLD, TLT`. The base
Five-Tool companion intake is the distinct seven-symbol set `GLD, IWM, QQQ, RSP, SPY, VIX,
VIX3M`. Evidence and catalog identity do not transfer between them. An overlapping future
release can satisfy both only through explicit, separately reviewed campaign bindings.

## Deliberately deferred

Short exposure remains unavailable until a new identity binds a short compiler, borrow
costs, shortability, account eligibility, legal/tax review, and fresh owner authority.
PAPER activation remains deferred until ongoing management evidence is authenticated, a
trusted management-event identity exists, broker-held protection semantics are decided,
and real restart/partial-fill/late-event/ambiguous-send lifecycles are observed.
