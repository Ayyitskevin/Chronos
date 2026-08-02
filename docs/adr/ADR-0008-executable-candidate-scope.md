# ADR-0008 — Executable candidates limited to daily-bar, long-only ETF strategies

Status: Accepted (2026-07-17). Index entry: DECISIONS.md D-12.

> **Amendment note, 2026-08-02 (record, not a rewrite — this ADR stands as accepted).**
> The capital premise below (~USD 3,000) is superseded as a statement of fact: the last
> documented account snapshot is approximately **USD 110**
> (`docs/VISION_COMPLETION_PLAN.md` §2). The decision this ADR records — daily-bar,
> long-only ETF candidates only — is *strengthened*, not weakened, by a smaller account,
> so nothing here is reopened. But the cost arithmetic quoted in Context and Consequences
> (≈3.3 bps/side, whole-share drag on USD 3,000) understates the drag at the observed
> balance by roughly 27x. Whether to fund toward the original premise or cut scope to
> match is a **live, unresolved owner decision** (plan §11); see ASSUMPTIONS A-10 and
> RISK_REGISTER R-10. Do not treat either figure as settled input for sizing or selection.

## Context

The Pine corpus (42 audited script artifacts, ASSUMPTIONS.md A-01) contains intraday systems (ORB,
session VWAP, RVOL screeners), options studies, and daily-bar systems. The target account is a
small IBKR cash account (~USD 3,000): pattern-day-trading rules make intraday impractical below
USD 25,000 in a margin account; cash settlement constrains turnover; the fixed ~USD 1.00 minimum
commission is ~3.3 bps per side on this equity, a cost floor that destroys high-frequency edges;
and no trustworthy intraday data was obtainable in this environment (A-31).

## Decision

Initial executable candidates are limited to daily-bar, long-only, non-leveraged ETF strategies
derived from the corpus:

- Markov regime-gated trend continuation family — `regime_trend_v1`
  (`src/chronos/strategies/regime_trend.py`, spec `specs/regime_trend_v1.yaml`);
- RSI-2-class mean reversion family — `mean_reversion_v1`
  (`src/chronos/strategies/mean_reversion.py`, spec `specs/mean_reversion_v1.yaml`).

Both are validated against simple baselines (`src/chronos/strategies/baselines.py`: buy-and-hold,
SMA trend, deterministic random entries). The candidate universe is restricted to highly liquid
US-listed ETFs (A-11). Intraday corpus scripts are classified research-only; options-related
scripts remain studies (the wheel dashboard stays decision-support only, A-12).

## Consequences

- Honesty over ambition: the platform ships with two executable strategy families, not 42.
- Whole-share sizing on USD 3,000 produces meaningful rounding drag; it is modelled, not hidden
  (A-22). *(Amended 2026-08-02 — see the amendment note at the top of this ADR: the last
  documented account snapshot is ≈ USD 110, so this drag is materially understated here.)*
- Research results must beat baselines net of the conservative cost floor (A-20/A-21) to matter;
  the quantitative validation report (TASKS.md "Next") is the evidence, and it does not exist yet.
- Expanding scope (single stocks, intraday, options execution) requires owner approval, new data,
  and new ADRs — not a config change.
