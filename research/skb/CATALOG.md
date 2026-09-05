# Strategy Knowledge Base — catalog

Generated from `research/skb/skb.json` by `scripts/build_skb.py` (`chronos.skb.docs`). Do not hand-edit — regenerate.

- Pine scripts: **42**
- Derived strategies: **2**
- Corpus hash: `94482faffd7205055363beb209b0ee86611a3588917b3637490f5af79fe67d8a`

## Dispositions

| Disposition | Count | Meaning |
|---|---:|---|
| `ported` | 2 | translated to a canonical Python spec |
| `deferred` | 4 | executable standalone strategy, portable, not yet ported |
| `blocked_on` | 1 | cannot be assessed/ported until a dependency clears |
| `rejected` | 35 | not a standalone tradable strategy |

## ported (2)

| # | Title | Family | Direction | Integrity | Reason |
|---|---|---|---|---|---|
| 01 | Markov Regime BULL+ v1.1 | regime_detection | long | PASS_WITH_CONSTRAINTS | ported_to_spec |
| 11 | Mean-Reversion Extremes Study v1.1 | mean_reversion | bidirectional | NON_EXECUTABLE_INDICATOR | ported_to_spec |

## deferred (4)

| # | Title | Family | Direction | Integrity | Reason |
|---|---|---|---|---|---|
| 00 | Five-Tool Confluence AIO v3.6 | regime_detection | bidirectional | PASS_WITH_CONSTRAINTS | executable_strategy_not_yet_ported |
| 02 | Markov Regime BEAR+ v1.1 | regime_detection | short | PASS_WITH_CONSTRAINTS | executable_strategy_not_yet_ported |
| 0A | Confluence Swing Strategy v1.0 (ARCHIVED) | trend_following | bidirectional | PASS_WITH_CONSTRAINTS | executable_strategy_not_yet_ported |
| 16 | Pullback-to-Value Playbook v1.0 | trend_following | bidirectional | PASS_WITH_CONSTRAINTS | executable_strategy_not_yet_ported |

## blocked_on (1)

| # | Title | Family | Direction | Integrity | Reason |
|---|---|---|---|---|---|
| 08 | Gap & Overnight Risk Classifier v1.1 | statistical_readout | none | REQUIRES_REWRITE | requires_rewrite |

## rejected (35)

| # | Title | Family | Direction | Integrity | Reason |
|---|---|---|---|---|---|
| 03 | KLQuant Shared Library v1.1 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 04 | SIP RVOL Screener v1.1 | volume_orderflow | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 05 | Session Structure & ORB v1.1 | market_structure | bidirectional | PASS_WITH_CONSTRAINTS | not_a_standalone_strategy |
| 06 | Noise-Area Bands v1.1 | market_structure | bidirectional | PASS_WITH_CONSTRAINTS | not_a_standalone_strategy |
| 07 | Squeeze & Exhaustion Sentinel v1.1 | volatility | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 09 | Breadth & Internals Proxy v1.1 | regime_detection | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 10 | Volume Profile Lite v1.1 | volume_orderflow | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 12 | Expectancy Journal Module v1.1 | journaling_validation | long | PASS_WITH_CONSTRAINTS | strategy_addon_not_standalone |
| 13 | SMC / ICT Concepts Study v1.1 | market_structure | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 14 | Fibonacci Confluence Mapper v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 15 | Buffett Desk Lens v1.0 | valuation_context | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 17 | HMM Regime Probability Filter v1.0 | regime_detection | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 18 | Volatility Regime Switching — Markov Chain v1.0 | regime_detection | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 19 | Markov Property Regime Persistence Gauge v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 20 | Adaptive Regime Transition Probability Bands v1.0 | regime_detection | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 21 | Kalman Filter MR Detector v1.0 | mean_reversion | bidirectional | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 22 | Kelly Criterion Sizing Overlay v1.0 | risk_overlay | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 23 | Regime-Dependent Vol Clustering Overlay v1.0 | volatility | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 24 | Dynamic Squeeze Exhaustion w/ Regime Context v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 25 | Walk-Forward Regime Stability Analyzer v1.0 | journaling_validation | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 26 | Time-of-Day RVOL Regime Filter v1.0 | volume_orderflow | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 27 | Profitable vs Tradable Robustness Tester v1.0 | journaling_validation | long | PASS_WITH_CONSTRAINTS | strategy_addon_not_standalone |
| 28 | Backtest Realism Simulator — Cost Stress v1.0 | journaling_validation | long | PASS_WITH_CONSTRAINTS | strategy_addon_not_standalone |
| 29 | Regime-Aware Risk Parity Allocator v1.0 | portfolio_allocation | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 30 | Drawdown-Controlled Regime Exit Engine v1.0 | risk_overlay | long | PASS_WITH_CONSTRAINTS | strategy_addon_not_standalone |
| 31 | IV Regime Detector & Crush Proxy v1.0 | regime_detection | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 32 | Tail-Risk Volatility Regime Filter v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 33 | Overnight Gap Regime Transition Detector v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 34 | Session Structure w/ Markov Probability Bands v1.0 | market_structure | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 35 | Statistical Edge Validation Dashboard v1.0 | journaling_validation | long | PASS_WITH_CONSTRAINTS | strategy_addon_not_standalone |
| 36 | Regime-Conditioned Kalman MR v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 37 | Adaptive MR w/ Regime Probability v1.0 | mean_reversion | bidirectional | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 38 | Value Zone Reversion Confluence Scanner v1.0 | mean_reversion | bidirectional | PASS_WITH_CONSTRAINTS | not_a_standalone_strategy |
| 39 | Monte Carlo Option-Inspired Equity Probability Bands v1.0 | statistical_readout | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |
| 40 | Black-Scholes Regime-Adjusted Implied Edge Gauge v1.0 | valuation_context | none | NON_EXECUTABLE_INDICATOR | non_executable_indicator |

## Source-measured properties

Read from the Pine source line by line (issue #181); no corpus input states these. Every other script is **unmeasured** — `unknown` here means nobody looked, not that the property is absent.

- Measured: **5** of 42
- Timeframe binding: `chart_tf` 5, `unknown` 37

| # | Title | Max concurrent positions | Timeframe binding | Evidence |
|---|---|---:|---|---|
| 00 | Five-Tool Confluence AIO v3.6 | 1 | chart_tf | max_concurrent_positions=1 from 00_five_tool_confluence_aio.pine:1469 (strategy.position_size == 0), 00_five_tool_confluence_aio.pine:32 (pyramiding = 3); timeframe_binding=chart_tf from 00_five_tool_confluence_aio.pine:743 (timeframe.period), 00_five_tool_confluence_aio.pine:33 (calc_on_every_tick = false). pyramiding is leg-splitting, not concurrency: entry is gated on flat, and the same-bar strategy.entry calls place legs of one scaled entry. The script pins no timeframe; it evaluates on the chart's, at bar close. |
| 01 | Markov Regime BULL+ v1.1 | 1 | chart_tf | max_concurrent_positions=1 from 01_markov_regime_bull_plus.pine:1086 (strategy.position_size == 0), 01_markov_regime_bull_plus.pine:52 (pyramiding = 3); timeframe_binding=chart_tf from 01_markov_regime_bull_plus.pine:712 (timeframe.period), 01_markov_regime_bull_plus.pine:53 (calc_on_every_tick = false). pyramiding is leg-splitting, not concurrency: entry is gated on flat, and the same-bar strategy.entry calls place legs of one scaled entry. The script pins no timeframe; it evaluates on the chart's, at bar close. |
| 02 | Markov Regime BEAR+ v1.1 | 1 | chart_tf | max_concurrent_positions=1 from 02_markov_regime_bear_plus.pine:1076 (strategy.position_size == 0), 02_markov_regime_bear_plus.pine:55 (pyramiding = 3); timeframe_binding=chart_tf from 02_markov_regime_bear_plus.pine:715 (timeframe.period), 02_markov_regime_bear_plus.pine:56 (calc_on_every_tick = false). pyramiding is leg-splitting, not concurrency: entry is gated on flat, and the same-bar strategy.entry calls place legs of one scaled entry. The script pins no timeframe; it evaluates on the chart's, at bar close. |
| 0A | Confluence Swing Strategy v1.0 (ARCHIVED) | 1 | chart_tf | max_concurrent_positions=1 from 0A_confluence_swing_strategy_archived.pine:356 (strategy.position_size == 0), 0A_confluence_swing_strategy_archived.pine:78 (pyramiding = 0); timeframe_binding=chart_tf from 0A_confluence_swing_strategy_archived.pine:259 (timeframe.period), 0A_confluence_swing_strategy_archived.pine:79 (calc_on_every_tick = false). The only pyramiding = 0 script: one strategy.entry, scaled OUT in three strategy.exit tranches. The script pins no timeframe; it evaluates on the chart's, at bar close. |
| 16 | Pullback-to-Value Playbook v1.0 | 1 | chart_tf | max_concurrent_positions=1 from 16_pullback_to_value_playbook.pine:310 (strategy.position_size == 0), 16_pullback_to_value_playbook.pine:311 (strategy.position_size == 0), 16_pullback_to_value_playbook.pine:32 (pyramiding = 3); timeframe_binding=chart_tf from 16_pullback_to_value_playbook.pine:192 (timeframe.period), 16_pullback_to_value_playbook.pine:33 (calc_on_every_tick = false). Both the long and short gate require flat. pyramiding is leg-splitting, not concurrency: entry is gated on flat, and the same-bar strategy.entry calls place legs of one scaled entry. The script pins no timeframe; it evaluates on the chart's, at bar close. |

## Derived strategies

| id | version | family | status | candidate | from | runs |
|---|---|---|---|---|---|---:|
| mean_reversion_v1 | 1.0.0 | short_term_mean_reversion | research_prototype | yes | 11 | 68 |
| regime_trend_v1 | 1.0.0 | regime_gated_trend_continuation | research_prototype | yes | 01 | 78 |
