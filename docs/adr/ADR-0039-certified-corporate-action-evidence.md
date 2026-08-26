# ADR-0039 — Certified corporate-action evidence binds the events it claims to judge

Status: **accepted design — owner-gated at merge, 2026-08-26. This closes a false-
certification path in code; no dataset is thereby certified.** Index entry:
DECISIONS.md D-53.

## Context

The certification attestation previously carried an owner-typed
`sampled_action_count`, but code checked only that it was positive. The count was not
compared with the action stream, and the certification digest did not commit to that
stream. A six-symbol, multi-decade QQQ packet with six empty action files could therefore
claim 12 sampled actions and produce the same certification identity as a packet containing
real distributions. Cash distributions often do not cross the 20% material-move threshold,
so split reconciliation did not make them visible by accident.

The QQQ packet manifest also carried an action count without proving that it matched the
parsed file. The receipt could repeat a stale or inflated count over different bytes.

This matters independently of performance. IBKR documents `TRADES` historical bars as
split-adjusted but not dividend-adjusted, while `ADJUSTED_LAST` applies both adjustments.
Chronos deliberately captures unadjusted trade bars, so its separate point-in-time action
stream is load-bearing for total-return research.

## Decision

### 1. Certification v3 commits to corporate-action semantics

Every declared symbol receives a sorted, order-invariant semantic SHA-256 and a distinct
event count over actions inside its exact certification windows. Both enter the canonical
certification document and therefore its digest. Exact duplicate events are blocking; they
cannot inflate the available sample. Adding, removing, or changing a dividend or split now
changes the certification identity even when prices never cross the material-move threshold.

This is `chronos-dataset-certification-v3`. No production release digest existed under v2,
so no migration or compatibility alias is minted.

### 2. Positive sample claims cannot exceed supplied distinct events

A `CorporateActionAttestation` remains the positive owner record. Its
`sampled_action_count` must be no greater than the distinct supplied events inside the
certified windows. A positive attestation over an all-empty panel refuses.

A genuinely action-free short window is possible. It uses a separate
`NoCorporateActionAttestation`, naming the independent source and the exact symbol/start/end
windows reviewed. A free-form note cannot waive an empty panel, the attested windows must
equal the certified windows, and any supplied action contradicts the no-action declaration.

The frozen six-symbol QQQ helper does not accept that exception: an all-empty
`QQQ,SPY,IWM,DIA,GLD,TLT` panel over its multi-decade identity is a different campaign claim
and needs a separately reviewed identity. There is no command-line override.

### 3. The QQQ receipt proves counts from bytes before declaration

Receipt finalization reparses each canonical action file and requires its manifest count to
equal the parsed event count. Declaration construction then sums those validated counts,
refuses zero, and refuses an independent sample count above the supplied total. The source
receipt hash still binds the manifest and every action-file digest.

## Consequences

The known false-positive path is closed and the report now explains what action semantics it
judged. This does **not** prove provider completeness: a plausible non-empty but incomplete
stream can still pass structural checks, and the independent-source review remains an owner
act. The first real QQQ capture must reconcile sponsor histories and a separate sample source;
code verifies coherence and identity, not external truth.

No market data, broker, account, registry, holdout, trial, strategy, order, funding, or
promotion capability is added. D2 remains blocked until an owner-supplied real capture passes
all gates and review.

## Sources

- [IBKR TWS API, Historical Bar Data](https://interactivebrokers.github.io/tws-api/historical_bars.html)
  — `TRADES` is split-adjusted but not dividend-adjusted; `ADJUSTED_LAST` adjusts both.
- [State Street, SPDR ETF 2026 distribution schedule](https://www.ssga.com/library-content/products/fund-data/etfs/us/distribution/SPDR_Dividend_Distribution_Schedule.pdf)
  — official sponsor evidence that SPY has scheduled distributions.
- [iShares TLT fact sheet](https://www.ishares.com/us/literature/fact-sheet/tlt-ishares-20-year-treasury-bond-etf-fund-fact-sheet-en-us.pdf)
  — official sponsor material listing monthly distribution frequency.
- [Invesco QQQ dividend-payment composition](https://www.invesco.com/content/dam/invesco/hk/en/pdf/INV-Composition-of-dividend-payments-Invesco-QQQ-ETF.pdf)
  — official sponsor material documenting QQQ distributions.
