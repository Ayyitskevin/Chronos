# ADR-0038 — QQQ power arithmetic uses daily active returns, not a mixed-unit trade count

Status: **accepted design — owner-gated at merge, 2026-08-26. Relative power arithmetic
is frozen; the absolute earliest pass date remains blocked pending the owner-approved clean
window. No data, trial, holdout, order, funding, or promotion authority.** Index entry:
DECISIONS.md D-52.

## Context

The QQQ constitution requires at least four percentage points of annualized post-cost alpha,
a positive 95% alpha lower bound, a preregistered power requirement, and at least 100 OOS
closed positions. Its current shorthand says `max(power-required N, 100 positions)`, but it
never defines N's observation unit, the variance used by the power calculation, or the clean
window's first session. A number chosen without those facts would be numerology.

NIST's prospective mean-test formula requires the type-I error, type-II error/power,
dispersion, and effect size. Andrew Lo shows why ordinary IID volatility is not a safe
substitute for long-run financial-return variance when returns are serially correlated.
Chronos already uses stationary block bootstrap inference after observation; power remains a
pre-data design calculation and must not inspect the eventual returns.

## Decision

### 1. Power the primary economic estimand on completed daily active returns

The sole confirmatory cell is `qqq-sma200-immediate-primary`. One observation is one completed
OOS daily session's net strategy return minus the volatility-matched QQQ/cash benchmark
return. The alternative at the power point is the constitution's minimum useful annualized
alpha, 4%. The null is non-positive annualized mean active return.

The four preregistered neighbor/robustness cells are not selectable substitutes for the
powered primary. Every attempt still counts for registry multiplicity, DSR, and the frozen
FWER/FDR gate, but a favorable neighbor cannot rescue a primary-cell failure. Cross-instrument
pooling is forbidden because the QQQ robustness panel's dependence has not been modeled.

### 2. Freeze the recommended design assumptions explicitly

The design uses a one-sided 95% lower confidence bound (`alpha = 0.05`), 80% prospective
power, 252 sessions per annualization year, and an 8% ceiling on annualized **long-run**
tracking error. The 8% figure is a design judgment, not a measured QQQ fact: paired with the
4% economic hurdle it requires an information ratio of at least 0.5. Owner merge approval is
approval of that design point.

“Long-run” is load-bearing. It means the annualized standard deviation relevant to the mean
must incorporate serial dependence. An IID sample standard deviation cannot be substituted.
If the later preregistration or realized evidence needs tracking error above 8%, this power
identity is invalid and cannot be loosened after results.

For a one-sided normal-approximation test of a mean with a preregistered variance ceiling,

```text
N = ceil((z_(1-alpha) + z_power)^2
         * (annualized_long_run_tracking_error / annualized_alpha)^2
         * sessions_per_year)

  = ceil((z_.95 + z_.80)^2 * (0.08 / 0.04)^2 * 252)
  = 6,233 completed OOS daily session returns
  = 24.7302289281 year-equivalents
```

The eventual evaluator still owes dependence-aware inference, DSR, FWER/FDR, PBO, cost
stress, dominance removal, regime/instrument coverage, and every other constitution gate.
Prospective normal arithmetic does not replace those verdicts.

### 3. Daily observations and closed positions remain separate gates

The powered unit is a daily benchmark-relative return. The economic lifecycle floor is a
closed position. They are not commensurate, so taking a numeric maximum is forbidden.
Campaign binding must require **both** 6,233 completed OOS daily returns and 100 OOS closed
positions. This v1 artifact does not edit the frozen control/candidate documents; a successor
campaign schema must bind the two typed requirements explicitly.

### 4. Freeze the relative earliest-pass arithmetic and refuse an invented date

If the first owner-approved clean completed session counts as observation one, the power
horizon ends 6,232 completed sessions after it. The absolute date remains `null`: neither the
clean-window identity nor its first session exists, and Chronos's pinned research session
calendar currently ends in 2026 because future ad-hoc closures cannot be known. A later
content-addressed campaign binding must freeze the absolute earliest pass date from the
approved clean start and an appropriately covered calendar. Until then the campaign remains
blocked before reading market data.

## Consequences

Chronos now has reproducible prospective arithmetic rather than a free-floating “100 trades
is enough” claim. The result is deliberately demanding: at the minimum useful 4% alpha and
the chosen 8% long-run tracking-error ceiling, statistical power needs about 24.73 independent
year-equivalents. This does not prohibit owner-capped experimentation under ADR-0025; it
prohibits calling a shorter record statistically validated under the QQQ constitution.

The identity is pragmatic partial progress. It cannot clear readiness until the clean-window
start, future session coverage, and successor dual-unit campaign binding exist. It reads no
market data and grants no capability.

## Sources

- [NIST, “Sample sizes required”](https://www.itl.nist.gov/div898/handbook/prc/section2/prc222.htm)
  — the prospective one-sided mean-test formula and the inputs that must be assumed.
- [Lo, “The Statistics of Sharpe Ratios”](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=377260)
  — serial correlation changes financial-return uncertainty and time aggregation.
- [Politis and Romano, “The Stationary Bootstrap”](https://www.stat.purdue.edu/docs/research/tech-reports/1991/tr91-03.pdf)
  — dependence-preserving resampling used by Chronos's post-observation inference.
- [Bailey and López de Prado, “The Sharpe Ratio Efficient Frontier”](https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf)
  — track-record length, non-normality, and the requirement to sample no more frequently
  than the independence assumption permits.
