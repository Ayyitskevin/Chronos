# Financial formulas

All monetary inputs are `Decimal`. The premium multiplier `m` comes from the qualified contract;
the independently verified share-only deliverable `d` determines assignment and coverage. The
MVP accepts a new short-option candidate only when the deliverable is verified and standard, so
`d = m`; it fails closed instead of modeling adjusted or mixed deliverables. Commissions remain
estimates until matched exactly to a broker commission report. Strategy calculations use a local
Decimal context sized from the inputs so an unrelated caller or unusually long value cannot
weaken a hard limit through rounding.

## Short put

For limit credit `p`, multiplier `m`, verified deliverable shares `d`, contracts `q`, strike `k`,
commission `c`, and scenario underlying price `s`:

- Gross premium: `p * m * q`
- Net premium: `gross premium - c`
- Gross assignment obligation: `k * d * q`
- Net cash deployed if assigned: `gross assignment obligation - net premium`
- Assigned shares: `d * q`
- Effective entry price: `k - net premium / assigned shares`
- Expiration P&L: `net premium - max(k - s, 0) * d * q`
- Cash-secured allocation: `gross assignment obligation / net liquidation value`

Cash-secured allocation is not broker margin. Broker what-if margin is displayed separately.

### Locked expiration-risk preview

The current dashboard preview fixes `q = 1`, uses the newly refreshed candidate bid for `p`, and
requires the operator to supply the total commission estimate `c`. The bid is labeled a
hypothetical credit, not a fill or limit-price guarantee. A zero estimate is permitted only as a
visibly fees-excluded assumption. The estimate is limited to 10,000 currency units, four normalized
fractional decimal places, and a compact bounded decimal representation. Chronos evaluates the
formula at four objective prices: observed spot, strike, effective entry, and zero. Equal prices
are combined into one labeled point.
Bid, underlying price, and effective entry are per-share values. Gross/net premium, commission,
assignment cash, obligation, and expiration P&L are one-contract totals.

These points describe expiration payoff only. They do not estimate broker margin, slippage, taxes,
early assignment, exercise behavior, volatility path, or the probability of reaching a price. The
preview is recomputed only after a fresh candidate evaluation uniquely revalidates the selected
contract; it never authorizes an order.

### Deterministic DEMO exact-limit rehearsal

The DEMO rehearsal fixes `q = 1`, uses the operator's explicit tick-aligned limit as `p`, and uses
the deterministic broker preview commission as `c`. It recomputes the same four expiration points
and totals from those exact terms. The earlier operator commission assumption remains visible only
for comparison:

- Commission variance: `broker estimated commission - operator commission assumption`

Initial-margin, maintenance-margin, and equity-with-loan changes are displayed separately from the
cash-secured allocation. They are DEMO broker outputs, not ingredients in the expiration-payoff
formula and not proof that an order is affordable or authorized. The receipt is neither persisted
nor an order-lifecycle transition; its terminal rehearsal status is `WHAT_IF_PREVIEWED`.

### Ephemeral DEMO approval rehearsal

Milestone 8 introduces no new payoff, capital, or margin formula. The operator must affirm the
fresh receipt's exact one-contract terms, including:

- Quantity: `q = 1`
- Exact option contract ID
- Exact limit credit: `p`
- Gross assignment obligation: `k * d * 1`

The typed canonical symbol and explicit risk acknowledgement are additional approval-rehearsal
inputs, not financial variables. The service treats every input as a scalar hint, reruns the full
Milestone 7 boundary, and requires exact agreement with the refreshed result. The resulting
`APPROVAL_REHEARSED` status records only an ephemeral DEMO rehearsal; it is not
`OrderLifecycle.USER_CONFIRMED`, an authorization, or a lifecycle transition. It changes none of
the receipt math and remains `STOPPED` and `LOCKED`. Only the affirmed scalar contract terms
cross the Milestone 8 result boundary; the refreshed payoff, margin output, full option contract,
and all parent results are discarded after validation.

Milestone 9 adds no financial formula. Its 15-minute monotonic display lease governs only
process-memory presentation. Once the receipt is expired, abandoned, or superseded, the terminal
tombstone retains no contract, price, obligation, payoff, capital, or margin term.

## Covered call

For stock quantity covered `d * q`:

- Gross premium and net premium use the formulas above.
- Called-away proceeds: `k * d * q`
- Stock P&L versus broker average cost `b`: `(k - b) * d * q`
- Total realized cycle P&L versus strategy-adjusted basis `a`:
  `(k - a) * d * q + net premium`
- Strategy-adjusted basis after premium: `a - net premium / (d * q)`
- Upside forfeited at scenario `s`: `max(s - k, 0) * d * q`
- Expiration downside exposure: `max(a - s, 0) * d * q - net premium`

The dashboard always labels `a` as **Strategy-Adjusted Basis — not tax basis** and never replaces
the broker's average cost.

For every short-option capital check, `d` is verified deliverable shares per contract, not an
assumption copied from a multiplier string. If the standard deliverable, underlying contract ID,
account fingerprint, or currency is unverified or mismatched, the candidate is ineligible.

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

Lower is better. Chronos normalizes delta error by the larger configured distance from the target
to a delta bound, spread by the maximum allowed spread, and DTE error by the larger configured
distance from target DTE to a DTE bound. Within the passing candidate set, each reported
liquidity field is normalized by that field's largest `log1p` value. The two normalized
components are always averaged with a fixed denominator; an absent observation contributes no
bonus and therefore cannot outrank a reported zero merely because it is missing. Volume or open
interest can independently satisfy the specification's OR filter, and an absent field remains
`None` for display and audit.

Spot is the positive underlying last price, falling back only to a valid positive non-crossed
midpoint. Candidate ranking permits fresh `LIVE`, `FROZEN`, and deterministic `DEMO` data;
`DELAYED`, `STALE`, and `UNKNOWN` reject. Only model delta is mandatory among the Greek fields;
gamma, theta, and implied volatility remain visibly unavailable when the broker omits them. No
Black-Scholes fallback exists.

Ties resolve deterministically by score, raw delta error, spread, DTE error, descending
liquidity, expiration, strike, and contract ID. These ranking rules do not make demo data or a
candidate eligible for transmission; the order boundary applies its own stricter gate.

## Strategy-adjusted basis ledger

The exact label is **Strategy-Adjusted Basis — not tax basis**. Broker average cost remains an
unchanged input. For reconciled eligible shares `h` and signed Chronos adjustments `a`:

```text
strategy_adjusted_basis = broker_average_cost + sum(a) / h
```

- An opening short-option premium is negative: `-price * multiplier * contracts`.
- A closing option debit is positive: `price * multiplier * contracts`.
- Commissions are positive adjustments.
- A reconciled actual commission replaces its matching provisional estimate only when amount and
  currency match broker commission evidence exactly; both records remain auditable.
- A premium with no commission estimate or actual report returns `PENDING` and no calculated
  adjusted basis. Commission evidence without its premium execution returns `MANUAL_REVIEW`.
- An explicit manual adjustment uses its supplied sign and requires a note.
- Assignment and called-away stock fills are evidence records; they never overwrite or
  reconstruct the broker's average cost, and they require a separately reconciled share
  allocation before basis can be projected.

Duplicate execution/type evidence, a currency mismatch, zero allocated shares, partial
assignment, split, symbol change, unmatched manual trade, or unexplained mismatch returns
`MANUAL_REVIEW` with no calculated adjusted basis.

## Assignment pressure

Assignment pressure is a heuristic, not a probability. Default policy uses near-zero extrinsic
`<= 0.05`, meaningful extrinsic `>= 0.10`, HIGH DTE `<= 3`, ELEVATED DTE `<= 5`, and absolute
delta `>= 0.50`. Following the product specification literally, near-zero remaining extrinsic
is independently HIGH; otherwise ITM near expiration is HIGH, ITM/high-delta/near-expiration is
ELEVATED, and OTM with meaningful extrinsic and more than five DTE is LOW. Missing required
stock price, strike, or DTE is UNKNOWN. Reliable near-term ex-dividend evidence escalates an ITM
call to HIGH only when the ex-dividend event falls no later than option expiration and expected
dividend strictly exceeds remaining extrinsic value.
