# Financial formulas

All monetary inputs are `Decimal`; contract multiplier comes from the qualified contract.
Commissions remain estimates until matched to a broker commission report.

## Short put

For limit credit `p`, multiplier `m`, contracts `q`, strike `k`, commission `c`, and scenario
underlying price `s`:

- Gross premium: `p * m * q`
- Net premium: `gross premium - c`
- Gross assignment obligation: `k * m * q`
- Net cash deployed if assigned: `gross assignment obligation - net premium`
- Assigned shares: `m * q`
- Effective entry price: `k - net premium / assigned shares`
- Expiration P&L: `net premium - max(k - s, 0) * m * q`
- Cash-secured allocation: `gross assignment obligation / net liquidation value`

Cash-secured allocation is not broker margin. Broker what-if margin is displayed separately.

## Covered call

For stock quantity covered `m * q`:

- Gross premium and net premium use the formulas above.
- Called-away proceeds: `k * m * q`
- Stock P&L versus broker average cost `b`: `(k - b) * m * q`
- Total realized cycle P&L versus strategy-adjusted basis `a`:
  `(k - a) * m * q + net premium`
- Strategy-adjusted basis after premium: `a - net premium / (m * q)`
- Upside forfeited at scenario `s`: `max(s - k, 0) * m * q`
- Expiration downside exposure: `max(a - s, 0) * m * q - net premium`

The dashboard always labels `a` as **Strategy-Adjusted Basis — not tax basis** and never replaces
the broker's average cost.

## Candidate score

After every hard eligibility filter passes:

```text
delta_error = abs(abs(delta) - target_abs_delta)
relative_spread = (ask - bid) / midpoint
dte_error = abs(dte - target_dte)
score = delta_weight * normalized_delta_error
      + spread_weight * normalized_relative_spread
      + dte_weight * normalized_dte_error
      - liquidity_weight * normalized_liquidity_bonus
```

Lower is better. Liquidity uses a bounded normalization of `log1p(volume)` and
`log1p(open_interest)`. Missing fields are rejection reasons, not zeros.
