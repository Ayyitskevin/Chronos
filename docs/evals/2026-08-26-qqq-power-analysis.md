# QQQ power-analysis identity — risky-change evaluation

Date: 2026-08-26

## Task contract

```yaml
plan_phase: 0; Phase 3 prerequisite
primary_kpi: net_edge_confidence
gate_advanced: relative power arithmetic only; market-data access remains blocked
files: one content-addressed power spec, one no-data compiler, safety tests, ADR/index/risk/status docs
verification: primary-source review, 20-case sensitivity matrix, drift and invalid-input refusals, focused and full repository gates, independent non-author review
evidence_artifact: specs/qqq_power_analysis_v1.json and this evaluation
owner_gate: required at merge; statistical/economic design assumptions
open: owner-approved clean-window start, future session-calendar coverage, absolute earliest pass date, successor campaign/evaluator binding, certified data and every performance verdict
```

## Assumptions frozen before implementation

- The powered estimand is the primary SMA-200 cell's completed daily post-cost return minus
  its volatility-matched QQQ/cash benchmark return.
- The minimum detectable annualized alpha is the constitution's 4% economic hurdle.
- The design uses a one-sided 95% lower bound, 80% power, 252 sessions/year, and an 8%
  annualized long-run tracking-error ceiling (minimum information ratio 0.5).
- Long-run tracking error includes serial dependence; ordinary IID standard deviation is not
  an admissible substitute.
- Robustness cells are not selectable substitutes and instruments are not pooled for power.
- The 6,233 daily-return requirement and 100 closed-position floor must both pass; unlike
  units are never compared with a numeric `max`.
- No absolute date is invented before the clean OOS start exists.

## Primary-source check

NIST states that prospective sample size has no correct answer without alpha, beta/power,
dispersion, and an effect-size assumption, and gives the one-sided mean-test formula used
here. Lo derives return-statistic uncertainty under stationary and serially correlated
returns, so dependence must enter the long-run variance. Politis and Romano provide the
stationary bootstrap foundation used by Chronos after data exists. Bailey and López de Prado
show that track-record requirements depend on sample length, non-normality, and a sampling
frequency compatible with independence.

Sources: [NIST sample-size formula](https://www.itl.nist.gov/div898/handbook/prc/section2/prc222.htm),
[Lo](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=377260),
[stationary bootstrap](https://www.stat.purdue.edu/docs/research/tech-reports/1991/tr91-03.pdf),
and [Sharpe Ratio Efficient Frontier](https://www.davidhbailey.com/dhbpapers/sharpe-frontier.pdf).

## Measurement boundary

No broker credential, account, gateway, market-data byte, clean/seen/burned map, holdout,
trial, order, funded capital, PAPER runtime, or promotion artifact was accessed. The 20 cases
below are deterministic design sensitivities, not observed-market measurements and not
performance evidence. The risky-change measurement requirement is satisfied at the method
boundary because opening real QQQ data would violate the task's pre-data gate.

## Twenty-case sensitivity matrix

The compiler recomputed each case with the same prospective formula. The matrix spans the
assumptions most capable of making a design look falsely easy: dispersion, effect size,
power, significance, and observation frequency.

| Case | Annual alpha | Long-run TE | Tail alpha | Power | Obs/year | Required N |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 4% | 4% | 5% | 80% | 252 | 1,559 |
| 2 | 4% | 6% | 5% | 80% | 252 | 3,506 |
| 3 | 4% | 8% | 5% | 80% | 252 | 6,233 |
| 4 | 4% | 10% | 5% | 80% | 252 | 9,738 |
| 5 | 4% | 12% | 5% | 80% | 252 | 14,023 |
| 6 | 4% | 15% | 5% | 80% | 252 | 21,910 |
| 7 | 4% | 20% | 5% | 80% | 252 | 38,951 |
| 8 | 2% | 8% | 5% | 80% | 252 | 24,929 |
| 9 | 3% | 8% | 5% | 80% | 252 | 11,080 |
| 10 | 6% | 8% | 5% | 80% | 252 | 2,770 |
| 11 | 8% | 8% | 5% | 80% | 252 | 1,559 |
| 12 | 4% | 8% | 5% | 70% | 252 | 4,744 |
| 13 | 4% | 8% | 5% | 90% | 252 | 8,633 |
| 14 | 4% | 8% | 5% | 95% | 252 | 10,909 |
| 15 | 4% | 8% | 10% | 80% | 252 | 4,544 |
| 16 | 4% | 8% | 2.5% | 80% | 252 | 7,912 |
| 17 | 4% | 8% | 1% | 80% | 252 | 10,117 |
| 18 | 4% | 8% | 0.5% | 80% | 252 | 11,773 |
| 19 | 4% | 8% | 5% | 80% | 52 | 1,286 |
| 20 | 4% | 8% | 5% | 80% | 12 | 297 |

The invariants behaved in the required direction: N rises with dispersion, confidence, and
power; falls with a larger detectable effect; and changes observation count without changing
the implied 24.73 year-equivalent horizon. These cases are pinned in
`tests/safety/test_qqq_power_analysis.py`.

## Verification result

Preflight at exact `origin/main` `90c4e850e78eb0a5b296bb76c002d70ca244f64c`:

```text
make gates
# ruff: All checks passed
# format: 544 files already formatted
# mypy: 293 Chronos source files; worker strict: 10 source files
# pytest: 4,096 passed / 1 skipped / 13 failed
```

The 13 failures are the inherited Streamlit `AppTest.from_file` relative-path cluster in
`test_backend_ui_pages`, `test_monitor_streamlit_app`, and `test_streamlit_app`; every one
resolves `src/...` relative to `tests/integration/`. No repository byte had been changed.

Focused implementation evidence:

```text
.venv/bin/ruff check src/chronos/research/qqq_power_analysis.py \
  tests/safety/test_qqq_power_analysis.py
# All checks passed

.venv/bin/mypy src/chronos/research/qqq_power_analysis.py
# Success: no issues found in 1 source file

PYTHONPATH=src .venv/bin/pytest -q tests/safety/test_qqq_power_analysis.py
# 39 passed
```

Full post-change gates and exact-head independent review are recorded before the PR is
presented for owner merge.

Post-change at the completed implementation tree:

```text
make gates
# ruff: All checks passed
# format: 546 files already formatted
# mypy: 294 Chronos source files; worker strict: 10 source files
# pytest: 4,135 passed / 1 skipped / 13 failed
```

The failure set is byte-for-byte in the same three Streamlit test modules and has the same
relative-path cause as preflight. The delta is 39 additional passes, zero new skips, and zero
new failures. Independent review is recorded in the repository/PR handoff against the exact
reviewed commit rather than claimed prospectively in this pre-review evaluation.

## Result

**Pragmatic partial, owner-gated.** Relative sample arithmetic is now exact and
content-addressed. The campaign remains blocked because an owner-approved clean start,
future calendar coverage, absolute pass date, and successor dual-unit campaign binding do
not exist. No evidence or authority gate advances.
