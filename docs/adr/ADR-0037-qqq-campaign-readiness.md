# ADR-0037 — Authentication-only QQQ campaign readiness

Status: **accepted design — owner-gated at merge, 2026-08-25. Blocked before the first
market-data read; no trial, holdout, PAPER, order, or promotion authority.** Index entry:
DECISIONS.md D-51.

## Context

ADR-0031 through ADR-0036 froze the QQQ constitution, SMA control, integrated Confluence
candidate, inert PAPER management policy, and authenticated opening-admission capability.
Each artifact correctly remained fail-closed, but their local blocker lists no longer gave a
single current answer. In particular, the Confluence overlay still describes its PAPER
lifecycle as absent even though an inert implementation now exists; implementation still is
not real PAPER evidence or broker protection.

The next safe step is therefore not a data read or a trial. It is a content-addressed current
readiness identity that authenticates what exists, separates who must supply each missing
input, and preserves all capability boundaries.

## Decision

### 1. One public operation reports readiness and owns no capability

`compile_qqq_campaign_readiness()` is the sole public operation. It reads only the exact
readiness/specification and source artifacts. Its result is immutable metadata with literal
false data-read, trial-registration, holdout-unlock, execution, order, and promotion flags.

The module may directly import only the two existing authentication-only QQQ research
compilers. A repository AST guard and fresh-process import probe refuse any data, registry,
holdout, broker, order, persistence, execution, supervisor, service, network, or database
capability from entering its dependency graph.

### 2. Current repository identities are authenticated, not inferred

The readiness artifact pins the exact bytes of the constitution, SMA control, Confluence
candidate, PAPER management module, and opening-admission module. It also records the
semantic PAPER policy digest. The child compilers must retain their complete blocker sets,
zero trials, no authority, and blocked-before-data status. Any source drift, authority
expansion, or dropped blocker refuses the aggregate report.

This deliberately distinguishes an implemented inert capability from evidence. The PAPER
code remains default-off and runtime-unwired; its presence cannot satisfy the real PAPER
lifecycle, trusted management-event, or broker-held protection requirements.

### 3. The requirement ledger separates responsibility and scope

Every remaining requirement is typed as owner action, Chronos build work, unavailable, or
deferred activation. Owner actions are the six-symbol export, independent corporate-action
attestation, clean/seen/burned map approval, benchmark/cash-leg approval, long-cost approval,
TradingView trace export, and approval of this exact identity at merge. Chronos must still
freeze the release/catalog, power analysis, evaluator/criteria/code/registry/campaign bundle,
parity evidence, and the Confluence base bindings.

Short-side evidence is explicitly unavailable and does not block a long-only campaign.
PAPER activation requirements are explicit but deferred; research readiness cannot satisfy
them.

### 4. QQQ and base Five-Tool data identities do not transfer

The QQQ robustness release is exactly `QQQ, SPY, IWM, DIA, GLD, TLT`. The base Five-Tool
companion intake is exactly `GLD, IWM, QQQ, RSP, SPY, VIX, VIX3M`. They overlap but are not
the same identity. A catalog or result from one cannot silently satisfy the other; any future
shared release needs explicit bindings in both campaigns.

## Consequences

The owner can now see exactly what must be supplied without handing Chronos credentials or
opening evidence. The compiler is deterministic and content-addressed, and realistic
artifact-mutation tests show every referenced byte identity fails closed.

The result advances no evidence gate. It does not prove IBKR completeness, data quality,
statistical power, TradingView parity, edge, PAPER behavior, funding, or execution safety.
Exact source hashes intentionally require a new reviewed readiness identity after a material
implementation change.

## Sources

- [IBKR historical bar data](https://interactivebrokers.github.io/tws-api/historical_bars.html)
  — historical trades are filtered; `TRADES` is split-adjusted but not dividend-adjusted.
- [IBKR historical-data limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html)
  — request pacing, unavailable history, throttling, and provider limits remain external
  constraints that repository identity cannot certify.
- [The Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
  — supports preserving multiple-testing identity; this ADR changes no frozen threshold.
