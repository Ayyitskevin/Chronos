# QQQ v1 constitution — risky-change evaluation

Date: 2026-08-25

## Scope of this evaluation

This is a pre-data governance evaluation, not a strategy-performance evaluation. The
change freezes owner choices and keeps the campaign blocked. Measuring performance now
would violate the purpose of the freeze and risk reusing the burned QQQ holdout.

## Assumptions checked against live repository state

| Assumption | Live check | Result |
|---|---|---|
| Short QQQ is executable today | `src/chronos/supervisor/compiler.py` and its safety tests | False: `SHORT_EQUITY` is deliberately refused without borrow capability. |
| A strategy is already selected | `docs/STRATEGY_SELECTION.md` | False: selected candidates remain `NONE`. |
| Trusted QQQ campaign data already exists | vision plan, limitations, certification runbook | False: the in-repo corpus is not a certified campaign release. |
| The prior QQQ final window is reusable | holdout disclosures | False: 2022-01-01 through 2024-01-10 is burned. |
| USD 3,000 is funded now | current-truth documents | False: USD 3,000 is a research reference and conditional future target; live allocation is USD 0. |
| Owner thresholds have empirical support | conversation record and source review | False: they are policy choices to test, not validated parameters. |

## External research check

- Moskowitz, Ooi, and Pedersen motivate testing time-series trend persistence; their
  multi-asset futures evidence does not validate a QQQ implementation.
- Daniel and Moskowitz document momentum crash asymmetry, especially on short legs;
  this supports separate short-side preregistration, not a short signal.
- Moreira and Muir motivate testing volatility scaling; later literature and Chronos's
  own protocol require an out-of-sample comparison rather than assuming benefit.

No paid research service was used. The fleet policy requires local/built-in research
before DeepAPI, and no paid result could validate an owner risk preference or replace
the campaign's future certified evidence.

## Measurement decision

No strategy trial, holdout read, broker mutation, or production measurement was run.
That is the safe result: the manifest remains `blocked_before_first_data_read`, carries
zero trials, null selection, USD 0 live risk, and no promotion authority. The first live
measurement belongs to the owner-run read-only gateway campaign; the first economic
measurement belongs to the future registered campaign after every blocker is resolved.

## Human sign-off captured

The owner explicitly selected QQQ long/short as the target; research/shadow with USD 0
live risk; conditional USD 3,000 funding after holdout, 90-day shadow, and supervised
paper; the benchmark and four-point hurdle; 10% drawdown, 100% gross exposure, 2% daily
loss, 1.5% CVaR; daily cadence; strategy sequence; six-to-ten-instrument robustness;
and USD 0 recurring data/software budget.

## Verification

- `sha256sum research/qqq_v1_constitution.json` →
  `4c99ce9d09f43a418c7342b0e40a0795b253bf3f1cd0e37d29419498b3008d56`.
- `.venv/bin/pytest -q tests/safety/test_qqq_v1_constitution.py` → 4 passed.
- `make gates` → Ruff, format, and both mypy lanes passed; pytest finished with
  3,995 passed, 1 skipped, and 13 failed. All 13 failures are the same pre-existing
  Streamlit `AppTest.from_file` relative-path failures measured before this change.
  The pre-change concurrent-ledger failure was transient and did not recur.
