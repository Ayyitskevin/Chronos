# Quantitative Research Report (Phase 6, re-run on a broadened universe)

## Verdict up front

**No strategy met the frozen validation criteria. Zero candidates are
selected for promotion beyond research.** This verdict is unchanged after
broadening the data universe from two symbols to five (SPY, QQQ, IWM, GLD,
TLT). The single binding failure is the same for every candidate on every
symbol: **C4's floor of ≥ 20 closed trades on the validation window is never
met** — the largest closed-trade count for any candidate on any symbol is 18
(regime_trend_v1 on QQQ). The brief explicitly allows — and prefers — this
outcome over manufactured confidence. The platform, data pipeline, and
research harness are sound and reusable; the two derived strategy cores, as
specified from the corpus defaults, did not demonstrate a defensible edge on
the available data under the criteria frozen in
`research/selection_manifest.json` **before** validation results were
computed (and re-frozen, again before new results, when the three symbols
were added).

## What the broadened data changed, and what it did not

- **Did not change:** the top-line verdict (zero selected) or the binding
  reason (the ≥ 20-trade sample floor, which fails universally).
- **Did change, and is reported honestly:** `mean_reversion_v1`, which in the
  two-symbol run failed C1 outright (net-negative on both SPY and QQQ), is
  **net-positive on IWM (+16.2%, PF 3.35, Sharpe 0.95) and marginally on TLT
  (+2.9%)** in the 2019–2021 window. That is the single strongest-looking
  cell in the entire study. It is **not** evidence of an edge: it rests on 12
  closed trades, on one small-cap symbol, in a dividend-adjusted, favorable,
  three-year window, and it is the best of ten (5 symbols × 2 candidates)
  cells — exactly where noise is expected to peak. The frozen
  `multiple_testing_guard` requires reading it as a re-test hypothesis, not a
  result.

## Method

- **Engine**: every run goes through the production path (portfolio sizing →
  independent risk engine → execution engine → simulated broker), not a
  vectorized shortcut. Determinism is test-asserted, and the SPY/QQQ numbers
  reproduced to the digit against the prior two-symbol run.
- **Costs**: USD 0.005/share with USD 1.00 minimum per order (IBKR Pro fixed
  assumption) + 2 bps/side slippage baseline. On a USD 3,000 account the
  commission floor alone is ≈ 3.3 bps/side — material, and included.
- **Partitions** (chronological, frozen): development ≤ 2017-12-31;
  validation 2018-01-01..2021-12-31; final test 2022-01-01.. reserved. The
  three new symbols cover 2019–2021 only, so they contribute to the
  **validation window exclusively** — no development, no final test.
- **Criteria**: C1–C6 frozen and committed before any validation-window
  computation, then **re-frozen unchanged** (only the data inventory and
  disclosures were updated) before the three-symbol results were computed —
  commit order proves both.
- **Baselines**: buy-and-hold-with-49%-disaster-stop; SMA 50/200 trend; a
  deterministic random-entry twin (fixed-seed LCG, ~5%/bar entry
  probability, 10-bar hold) carrying identical costs.
- **Data (five symbols, heterogeneous provenance — this matters)**:
  - SPY 2000-01..2019-11 — **unadjusted**, byte-exact.
  - QQQ 1999-11..2024-01 — unadjusted OHLC + adjusted close, byte-exact.
  - IWM 2019-01..2021-12 — **dividend-ADJUSTED**, markdown-transcribed
    (2-decimal), independently cross-checked.
  - GLD 2019-01..2021-12 — **effectively nominal** (no distributions),
    markdown-transcribed, cross-check penny-exact.
  - TLT 2019-01..2021-12 — **dividend-ADJUSTED (heavily)**,
    markdown-transcribed, cross-checked.
  - DIA — **not acquired** (confirmed absent from the source panel), excluded
    rather than fabricated.
  All five pass the project data-quality validator with zero blocking issues.
  Full provenance, adjustment status, and fidelity caveats are in
  `research/data/raw/MANIFEST.json`.

### Metric conventions

`net_pnl_usd`/PF/win-rate derive from **closed** trades; `total_return`
includes marked open positions. The "buy & hold" baseline carries the
platform's mandatory protective stop (at the 49% cap), so in crash windows
it becomes stop-out-and-reenter; its returns are reported with that caveat
rather than pretending the platform can express a stopless position. Every
validation cell in this run is flagged `low_sample` by the metrics module —
the trade counts are small enough that point metrics are noisy by
construction.

## Results — development window (exploration; ≤ 2017; SPY/QQQ only)

| Strategy | Sym | Trades | Net ret | MaxDD | PF | Sharpe | Exposure |
|---|---|---|---|---|---|---|---|
| regime_trend_v1 | SPY | 86 | +10.2% | 17.4% | 1.05 | 0.12 | 28% |
| regime_trend_v1 | QQQ | 88 | +34.1% | 15.5% | 1.37 | 0.24 | 30% |
| mean_reversion_v1 | SPY | 78 | +1.3% | 12.4% | 1.03 | 0.04 | 4% |
| mean_reversion_v1 | QQQ | 81 | +18.9% | 18.4% | 1.40 | 0.21 | 5% |
| baseline_sma_trend | SPY | 8 | +199.9% | 18.6% | 14.4 | 0.66 | 54% |
| baseline_sma_trend | QQQ | 12 | +209.8% | 26.4% | 3.01 | 0.55 | 56% |
| baseline_random_entries | SPY | 141 | +71.8% | 20.3% | 1.55 | 0.37 | 31% |
| baseline_random_entries | QQQ | 142 | +96.5% | 54.5% | 1.50 | 0.34 | 30% |

The new symbols have no bars before 2019 and so do not appear in
development. As before, both candidates trail the trivial SMA baseline by a
wide margin and roughly match or trail their random twin at comparable
exposure — most of their profit is market drift captured while long, not
signal skill.

## Results — validation window (2018–2021; new symbols 2019–2021 only)

| Strategy | Sym | Trades | Net ret | MaxDD | PF | Sharpe |
|---|---|---|---|---|---|---|
| regime_trend_v1 | SPY | 5 | −2.5% | 8.5% | 0.42 | −0.27 |
| regime_trend_v1 | QQQ | 18 | +33.0% | 11.7% | 2.64 | 0.74 |
| regime_trend_v1 | IWM | 10 | +11.0% | 11.3% | 1.70 | 0.38 |
| regime_trend_v1 | GLD | 11 | −3.8% | 10.4% | 0.73 | −0.15 |
| regime_trend_v1 | TLT | 9 | +1.2% | 14.9% | 1.25 | 0.09 |
| mean_reversion_v1 | SPY | 8 | −7.0% | 7.5% | 0.20 | −1.20 |
| mean_reversion_v1 | QQQ | 15 | −9.8% | 13.1% | 0.36 | −0.67 |
| mean_reversion_v1 | IWM | 12 | +16.2% | 4.7% | 3.35 | 0.95 |
| mean_reversion_v1 | GLD | 9 | −3.9% | 6.5% | 0.55 | −0.43 |
| mean_reversion_v1 | TLT | 12 | +2.9% | 5.8% | 1.52 | 0.36 |
| baseline_buy_hold | IWM | 0 | +67.6% | 39.0% | — | 0.78 |
| baseline_buy_hold | GLD | 0 | +38.0% | 17.9% | — | 0.81 |
| baseline_buy_hold | TLT | 0 | +25.6% | 20.2% | — | 0.57 |
| baseline_sma_trend | IWM | 4 | +10.5% | 32.0% | 0.08 | 0.28 |
| baseline_random_entries | IWM | 19 | +2.6% | 17.8% | 1.11 | 0.13 |
| baseline_random_entries | GLD | 20 | +1.2% | 12.8% | 1.05 | 0.09 |

Cost/parameter robustness on the cells that were net-positive:

- regime_trend_v1 QQQ: +31.6% at 2× commissions; +29.9% at 10 bps; +22.6% at
  25 bps; all ten sensitivity variants positive.
- regime_trend_v1 IWM: +10.3% at 2× commissions; +9.5% at 10 bps; **10/10**
  sensitivity variants positive.
- mean_reversion_v1 IWM: +15.4% at 2× commissions; +14.0% at 10 bps; **7/8**
  sensitivity variants positive.
- regime_trend_v1 TLT and mean_reversion_v1 TLT are barely positive and
  cost-fragile (TLT regime ≈ 0% at 10 bps). GLD is net-negative for both
  candidates and mostly negative under sensitivity.

Candidate daily-return correlation (validation): SPY 0.38, QQQ 0.20, IWM
0.23, GLD 0.03, TLT 0.003.

**Buy-and-hold dominates total return on every symbol in this window** (QQQ
+135.5%, IWM +67.6%, GLD +38.0%, TLT +25.6%, SPY +13.5% on its truncated
slice) — the 2019–2021 window was kind to simply holding, which is the
backdrop against which every candidate number should be read.

## Criteria application (frozen order)

The binding gate is **C4: profit factor ≥ 1.1 with ≥ 20 closed trades on the
validation window**. No candidate reaches 20 closed trades on any single
symbol — the maximum is 18 (regime_trend_v1, QQQ); the new symbols top out at
12. **Both candidates therefore fail C4 on every symbol, and per the manifest,
failing any of C1–C5 → `not_eligible`.** We do not bend a frozen criterion
after seeing results — that is exactly the selection bias the floor exists to
prevent. Detail:

- **regime_trend_v1** — passes C1–C3 on **QQQ** (+33.0%, beats the random twin
  on return and Sharpe, far lower drawdown than baselines) and now also on
  **IWM** (+11.0%, PF 1.70, beats IWM's random twin +2.6%/0.13, drawdown 11.3%
  vs the SMA baseline's 32%, 10/10 sensitivity). It is net-negative on GLD and
  SPY and barely/cost-fragile on TLT. **FAILS C4** on every symbol (18, 10, 11,
  9, 5 trades). Status: `not_eligible`.
- **mean_reversion_v1** — in the two-symbol run it failed C1 outright; the
  broadened data flips that: it passes C1–C3 on **IWM** (+16.2%, PF 3.35,
  beats IWM's random twin, drawdown 4.7% vs SMA's 32%, 7/8 sensitivity). It is
  net-negative on SPY, QQQ, GLD and only marginally positive on TLT. **FAILS
  C4** on every symbol (max 12 trades). Status: `not_eligible`.

**Multiple-testing discount (frozen guard applied).** The universe grew from 2
to 5 symbols, so C1 ("net-positive on at least one symbol") is mechanically
easier to satisfy, and both candidates now have at least one net-positive
symbol. Read against ten (symbol × candidate) cells in a favorable window —
where even the random-entry twin posts PF 1.05–1.11 on some symbols — a lone
strong cell (mean_reversion_v1 on IWM) is weak evidence, not validation. The
short, dividend-adjusted 2019–2021 windows for the new symbols make a
≥ 20-trade sample structurally unreachable, so their role is corroboration,
not a fresh chance to manufacture a pass.

**Disclosure — the trade counts are cap-dependent (raised by an independent
review, retained here).** The research risk policy in
`scripts/run_research.py` uses deliberately wide caps (bot capital / notional
/ aggregate exposure at USD 10M, per-trade risk 0.50) so a fixed USD 3,000
notional ceiling does not become binding as equity compounds and silently
suppress trades. This was set in the same commit that froze the criteria. The
wide-cap number measures the strategy's natural trade frequency unclipped; a
tight USD 3,000 / 0.25 cap would yield fewer trades still (QQQ regime: 7, not
18). **The C4 pass/fail outcome is identical either way** — no candidate
clears 20 under either cap — and zero candidates are selected regardless.

**C6 / final test — corrected disclosure (raised by the M5 independent
review).** An earlier revision of this report claimed the final window
(2022-01-01..) was "not consumed and remains pristine." **That was wrong.**
The harness's `--stage all` default computes the final stage, and the run that
produced this report's results also produced final-window numbers, which are
committed in `research/results/research_all.json`. Hiding or deleting them
would compound the error, so they are reported here (QQQ only — the sole
symbol whose data reaches past 2022-01; window 2022-01-03..2024-01-10):

| Strategy | Trades | Net ret | MaxDD | PF | Sharpe |
|---|---|---|---|---|---|
| regime_trend_v1 | 3 | +16.0% | 4.7% | 6.56 | 1.05 |
| mean_reversion_v1 | 7 | +0.3% | 2.3% | 1.10 | 0.06 |
| baseline_sma_trend | 0* | +34.1% | 9.9% | — | 1.58 |
| baseline_buy_hold | 0 | +1.5% | 33.2% | — | 0.15 |
| baseline_random_entries | 13 | −2.7% | 24.0% | 0.85 | −0.03 |

(*open position marked, no closed round trip.)

What these numbers **did not** do: influence selection. Rejection is driven
entirely by C4's ≥ 20-trade floor on the *validation* window, decided before
any final-window figure was read — and the final window's own samples (3 and
7 trades) are even thinner. What they **did** do: consume QQQ's one-shot
holdout. A future "run once, blind" final test on QQQ is no longer possible;
any re-test of these candidates on QQQ must treat 2022–2024 as seen data and
reserve a *new* untouched window (data after 2024-01, or IBKR-sourced fresh
history). The new symbols' data ends 2021-12, so their final windows remain
genuinely untouched — but only because the data stops, not by discipline.
Process fix adopted: the harness's default stage should not include `final`;
running the final stage should require the explicit flag
(`--stage final`), which `scripts/run_research.py` now enforces.

## Honest interpretation

1. The ≥ 20-trade sample floor, not a lack of positive cells, is what stops
   every candidate. The broadened data confirms this is **structural** (low
   trade frequency of both derived daily cores), not a QQQ-specific artifact.
2. `regime_trend_v1` remains the cleanest re-test hypothesis: net-positive and
   cost/parameter-robust on the two liquid equity indices where it fired
   enough (QQQ, IWM), lower drawdown than baselines — but negative on GLD/SPY,
   fragile on TLT, and always sample-starved.
3. `mean_reversion_v1`'s IWM result is the most eye-catching number in the
   study and the most likely to be noise: best-of-ten cell, 12 trades,
   small-cap, adjusted prices, favorable window. It is a hypothesis for a
   longer small-cap re-test, nothing more.
4. Simple baselines are hard to beat. Buy-and-hold outperformed every
   candidate on total return on every symbol in this window; the corpus's own
   doctrine ("the gate is the strategy"; "backtest is the autopilot floor,
   not a promise") is consistent with what we measured.
5. USD 3,000 + IBKR minimum commissions is a hostile cost environment: every
   marginal edge must clear ≈ 6.6 bps round-trip in commissions alone plus
   slippage.

## Confidence limitations

- **Heterogeneous provenance**: SPY/QQQ unadjusted and byte-exact; IWM/TLT
  dividend-adjusted (TLT heavily); GLD nominal. Adjusted and unadjusted series
  differ in level and total return, so cross-symbol comparisons of absolute
  return are not apples-to-apples. Each symbol is judged against its own
  baselines on its own series (comparable within-symbol); cross-symbol reading
  carries this caveat.
- **Fidelity**: IWM/GLD/TLT were transcribed from a markdown parquet preview
  and rounded to 2 decimals — research-grade, independently cross-checked, but
  lower-fidelity than the SPY/QQQ byte-exact series.
- **Window**: the new symbols span 2019–2021 only (validation-window-only, no
  development, no final test), a period unusually favorable to long equity,
  gold, and long-duration bonds until the 2022 turn that this window excludes.
- **SPY ends 2019-11**: SPY contributes dev + partial validation only, no
  final test. Unadjusted SPY understates buy-side total return by ~ the
  dividend yield.
- Parameters came from the corpus author's defaults, which may embed a look at
  overlapping history (recorded, uncontrollable here).
- No intraday validation was possible; intraday corpus scripts are unassessed
  on merit (A-31). Single-path backtest per configuration; no bootstrap
  confidence intervals were computed for the thin samples (the thin samples
  are themselves why C4 fails). Re-run against IBKR historical data before any
  promotion (RISK_REGISTER R-08).

## Recommended next research iteration (owner decisions)

1. Source **IBKR historical daily data** (or another trusted, uniformly
   adjusted feed) for 6–10 liquid ETFs covering 2000–present, so the whole
   universe shares one adjustment convention and reaches the reserved final
   window. This replaces the current heterogeneous, transcribed 2019–2021
   patch.
2. Re-run this harness; note QQQ's 2022–2024 holdout is now consumed (see the
   C6 disclosure), so reserve a fresh untouched window for any re-test.
3. Prioritize the two hypotheses this run surfaced: `regime_trend_v1` on
   liquid equity indices, and `mean_reversion_v1` on **small-caps** (IWM), the
   only place its daily reduction looked alive. A candidate is promotable only
   if it then clears all of C1–C5 including a ≥ 20-trade sample on ≥ 2 symbols.
4. Extend IWM/GLD/TLT to full history from a byte-exact source before treating
   any of their 2019–2021 numbers as more than a hypothesis.
