# Strategy Catalog — Pine Quant Library

Source of truth: Notion → Command Center → Trading Library → Pine Quant
Library — Master Index, fetched byte-exact on 2026-07-17 (hashes in
`research/strategy_registry.yaml`; verify with
`python -m chronos.cli verify-corpus`).

**Corpus size note:** the build brief described "approximately 77 Pine
scripts". The authoritative Master Index catalogs **42 artifacts**
(00-40 plus archived 0A). No other Pine sources exist in the Trading Library.
See ASSUMPTIONS.md A-01.

## Integrity status distribution

| Status | Count |
|---|---|
| `NON_EXECUTABLE_INDICATOR` | 28 |
| `PASS_WITH_CONSTRAINTS` | 13 |
| `REQUIRES_REWRITE` | 1 |

## Family distribution

| Family | Count |
|---|---|
| statistical_readout | 9 |
| regime_detection | 8 |
| journaling_validation | 5 |
| market_structure | 4 |
| mean_reversion | 4 |
| volume_orderflow | 3 |
| volatility | 2 |
| trend_following | 2 |
| valuation_context | 2 |
| risk_overlay | 2 |
| portfolio_allocation | 1 |

## Catalog

| # | Title | Kind | Family | Direction | Pine | Lines | Integrity |
|---|-------|------|--------|-----------|------|-------|-----------|
| 00 | Five-Tool Confluence AIO v3.6 | strategy | regime_detection | bidirectional | 6 | 2443 | `PASS_WITH_CONSTRAINTS` |
| 01 | Markov Regime BULL+ v1.1 | strategy | regime_detection | long | 6 | 1516 | `PASS_WITH_CONSTRAINTS` |
| 02 | Markov Regime BEAR+ v1.1 | strategy | regime_detection | short | 6 | 1506 | `PASS_WITH_CONSTRAINTS` |
| 03 | KLQuant Shared Library v1.1 | library | statistical_readout | none | 6 | 288 | `NON_EXECUTABLE_INDICATOR` |
| 04 | SIP RVOL Screener v1.1 | indicator | volume_orderflow | none | 6 | 337 | `NON_EXECUTABLE_INDICATOR` |
| 05 | Session Structure & ORB v1.1 | indicator | market_structure | bidirectional | 6 | 324 | `PASS_WITH_CONSTRAINTS` |
| 06 | Noise-Area Bands v1.1 | study | market_structure | bidirectional | 6 | 240 | `PASS_WITH_CONSTRAINTS` |
| 07 | Squeeze & Exhaustion Sentinel v1.1 | indicator | volatility | none | 6 | 154 | `NON_EXECUTABLE_INDICATOR` |
| 08 | Gap & Overnight Risk Classifier v1.1 | indicator | statistical_readout | none | 6 | 222 | `REQUIRES_REWRITE` |
| 09 | Breadth & Internals Proxy v1.1 | indicator | regime_detection | none | 6 | 187 | `NON_EXECUTABLE_INDICATOR` |
| 0A | Confluence Swing Strategy v1.0 (ARCHIVED) | strategy | trend_following | bidirectional | 6 | 529 | `PASS_WITH_CONSTRAINTS` |
| 10 | Volume Profile Lite v1.1 | indicator | volume_orderflow | none | 6 | 232 | `NON_EXECUTABLE_INDICATOR` |
| 11 | Mean-Reversion Extremes Study v1.1 | study | mean_reversion | bidirectional | 6 | 209 | `NON_EXECUTABLE_INDICATOR` |
| 12 | Expectancy Journal Module v1.1 | strategy_addon | journaling_validation | long | 6 | 185 | `PASS_WITH_CONSTRAINTS` |
| 13 | SMC / ICT Concepts Study v1.1 | study | market_structure | none | 6 | 343 | `NON_EXECUTABLE_INDICATOR` |
| 14 | Fibonacci Confluence Mapper v1.0 | indicator | statistical_readout | none | 6 | 343 | `NON_EXECUTABLE_INDICATOR` |
| 15 | Buffett Desk Lens v1.0 | indicator | valuation_context | none | 6 | 161 | `NON_EXECUTABLE_INDICATOR` |
| 16 | Pullback-to-Value Playbook v1.0 | strategy | trend_following | bidirectional | 6 | 577 | `PASS_WITH_CONSTRAINTS` |
| 17 | HMM Regime Probability Filter v1.0 | indicator | regime_detection | none | 6 | 333 | `NON_EXECUTABLE_INDICATOR` |
| 18 | Volatility Regime Switching — Markov Chain v1.0 | indicator | regime_detection | none | 6 | 265 | `NON_EXECUTABLE_INDICATOR` |
| 19 | Markov Property Regime Persistence Gauge v1.0 | indicator | statistical_readout | none | 6 | 390 | `NON_EXECUTABLE_INDICATOR` |
| 20 | Adaptive Regime Transition Probability Bands v1.0 | indicator | regime_detection | none | 6 | 275 | `NON_EXECUTABLE_INDICATOR` |
| 21 | Kalman Filter MR Detector v1.0 | indicator | mean_reversion | bidirectional | 6 | 332 | `NON_EXECUTABLE_INDICATOR` |
| 22 | Kelly Criterion Sizing Overlay v1.0 | display_overlay | risk_overlay | none | 6 | 218 | `NON_EXECUTABLE_INDICATOR` |
| 23 | Regime-Dependent Vol Clustering Overlay v1.0 | study | volatility | none | 6 | 257 | `NON_EXECUTABLE_INDICATOR` |
| 24 | Dynamic Squeeze Exhaustion w/ Regime Context v1.0 | study | statistical_readout | none | 6 | 296 | `NON_EXECUTABLE_INDICATOR` |
| 25 | Walk-Forward Regime Stability Analyzer v1.0 | study | journaling_validation | none | 6 | 283 | `NON_EXECUTABLE_INDICATOR` |
| 26 | Time-of-Day RVOL Regime Filter v1.0 | indicator | volume_orderflow | none | 6 | 292 | `NON_EXECUTABLE_INDICATOR` |
| 27 | Profitable vs Tradable Robustness Tester v1.0 | strategy_addon | journaling_validation | long | 6 | 279 | `PASS_WITH_CONSTRAINTS` |
| 28 | Backtest Realism Simulator — Cost Stress v1.0 | strategy_addon | journaling_validation | long | 6 | 248 | `PASS_WITH_CONSTRAINTS` |
| 29 | Regime-Aware Risk Parity Allocator v1.0 | indicator | portfolio_allocation | none | 6 | 244 | `NON_EXECUTABLE_INDICATOR` |
| 30 | Drawdown-Controlled Regime Exit Engine v1.0 | strategy_addon | risk_overlay | long | 6 | 215 | `PASS_WITH_CONSTRAINTS` |
| 31 | IV Regime Detector & Crush Proxy v1.0 | indicator | regime_detection | none | 6 | 229 | `NON_EXECUTABLE_INDICATOR` |
| 32 | Tail-Risk Volatility Regime Filter v1.0 | study | statistical_readout | none | 6 | 233 | `NON_EXECUTABLE_INDICATOR` |
| 33 | Overnight Gap Regime Transition Detector v1.0 | study | statistical_readout | none | 6 | 270 | `NON_EXECUTABLE_INDICATOR` |
| 34 | Session Structure w/ Markov Probability Bands v1.0 | indicator | market_structure | none | 6 | 276 | `NON_EXECUTABLE_INDICATOR` |
| 35 | Statistical Edge Validation Dashboard v1.0 | strategy_addon | journaling_validation | long | 6 | 245 | `PASS_WITH_CONSTRAINTS` |
| 36 | Regime-Conditioned Kalman MR v1.0 | indicator | statistical_readout | none | 6 | 362 | `NON_EXECUTABLE_INDICATOR` |
| 37 | Adaptive MR w/ Regime Probability v1.0 | study | mean_reversion | bidirectional | 6 | 387 | `NON_EXECUTABLE_INDICATOR` |
| 38 | Value Zone Reversion Confluence Scanner v1.0 | indicator | mean_reversion | bidirectional | 6 | 420 | `PASS_WITH_CONSTRAINTS` |
| 39 | Monte Carlo Option-Inspired Equity Probability Bands v1.0 | indicator | statistical_readout | none | 6 | 258 | `NON_EXECUTABLE_INDICATOR` |
| 40 | Black-Scholes Regime-Adjusted Implied Edge Gauge v1.0 | indicator | valuation_context | none | 6 | 246 | `NON_EXECUTABLE_INDICATOR` |

## Corpus composition and duplication analysis

The 42 scripts are **not 42 independent strategies**. The forensic audit's
`related_scripts` findings (research/pine_findings.json) show one coherent
"5T Pine Suite" family built around a shared core, with most artifacts being
satellites, variants, or measurement companions rather than distinct edges:

- **Shared math core**: script 03 (KLQuant library) supplies the Wilson
  confidence-interval, time-of-day matrix, RVOL, Markov-stay, gap-class, and
  squeeze primitives that 04, 05, 06, 07/24, 08/33, 12, and the BULL+/BEAR+
  pair all cite as their source (explicit line-referenced credit in the
  audit findings, not inferred).
- **One flagship, one mirrored pair**: script 00 (Five-Tool Confluence AIO
  v3.6) is the superset system; scripts 01 (BULL+) and 02 (BEAR+) are an
  explicit long/short mirror pair "ported from AIO v3.6" (their own headers
  say so) sharing the identical regime engine, Markov gate, and AVWAP
  machinery. Script 0A (archived Confluence Swing Strategy) is an earlier,
  simpler ancestor of the same family with its own dependency chain
  (Regime Label, RS Leader, AVWAP, R-Planner components).
- **Regime-export satellite cluster** (11 scripts): 17, 18, 19, 20, 23, 24,
  25, 26, 29, 31, 32, 33, 34, 36 all either *consume* a `REGIME_EXPORT`
  {-1,0,+1} signal produced by the flagship/BULL+/BEAR+ engines via
  `input.source`, or re-implement the identical "volatility-normalized
  window-return z-score" fallback classifier when no link is wired. These
  are measurement/readout lenses on one regime signal, not independent
  regime-detection strategies.
- **Journaling/validation-graft family** (5 scripts): 12 (Expectancy
  Journal), 27 (Profitable vs Tradable), 28 (Cost Stress), 30
  (Drawdown-Controlled Exit), 35 (Statistical Edge Validation Dashboard) all
  self-describe as the same "strategy-shell graft" pattern intended to be
  copied into a host strategy, sharing the L1P/S1F entry-ID convention, the
  Wilson-lower-bound doctrine, and a throwaway demo strategy for
  self-contained testing.
- **Mean-reversion/Kalman lineage** (4 scripts): 21 (Kalman Filter MR
  Detector) is the core; 36 (Regime-Conditioned Kalman MR) explicitly
  carries its logic "verbatim" and adds regime ledgers; 11 (MR Extremes
  Study) and 37 (Adaptive MR w/ Regime Probability) share the same
  volatility-normalized stretch/z-score family and Wilson-bound scoring.
- **Session/gap/RVOL structure family** (6 scripts): 04, 05, 06, 08, 26, 33,
  34 share day-anchored session machinery, gap-vs-ATR construction, and
  time-of-day RVOL slotting, each a different lens on the same underlying
  session-structure primitives from KLQuant.
- **Standalone/context tools** (5 scripts): 13 (SMC/ICT vocabulary study,
  explicitly thin-evidence by its own design), 14 (Fibonacci confluence),
  15 (Buffett valuation lens), 16 (Pullback-to-Value teaching combo), 22
  (Kelly sizing overlay), 39 (Monte Carlo bands), 40 (Black-Scholes gauge)
  are each genuinely distinct tools with no direct sibling, though several
  still consume the suite's `REGIME_EXPORT` convention.

**Estimated genuinely distinct strategies (executable trading systems, not
readouts): 4** — the flagship AIO (00), the BULL+/BEAR+ mirror pair (01/02,
arguably one engine with a sign flip), and the archived predecessor (0A).
Everything else in the 13 `PASS_WITH_CONSTRAINTS` scripts is a strategy-shell
graft (add-on) or a bidirectional indicator/study with entry-like logic but
no independent thesis. The remaining 28 scripts are explicitly
`NON_EXECUTABLE_INDICATOR` by design — readouts, filters, and studies that
feed the four systems above rather than trading on their own. This matches
the corpus's own stated doctrine ("one math core, written once"; "the gate
is the strategy") and is why Chronos derived only two independent executable
candidates (`regime_trend_v1`, `mean_reversion_v1`) rather than translating
all 42 files — see docs/STRATEGY_SELECTION.md for why even those two did not
clear the research bar.

Detailed per-script findings: [PINE_AUDIT.md](PINE_AUDIT.md).
