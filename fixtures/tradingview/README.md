# TradingView reference fixtures (empty — owner action required)

To upgrade parity from "verified against specification" to "verified against
TradingView" (docs/PARITY_REPORT.md), export from TradingView using the exact
pinned script versions in `research/strategy_registry.yaml`:

1. **Indicator series**: add the script to a chart (symbol + timeframe of
   interest), open the Data Window, and export the `*_EXPORT` plots
   (e.g. `PSTAY export`, `RSI2_EXPORT`, `MR_LONG_FLAG_EXPORT`) bar by bar
   (chart → Export chart data → CSV). Save as
   `fixtures/tradingview/<catalog>_<symbol>_<tf>_series.csv`.
2. **Trade lists**: for strategy scripts, Strategy Tester → List of trades →
   export CSV. Save as `fixtures/tradingview/<catalog>_<symbol>_<tf>_trades.csv`.
3. Record for each export: script catalog number and SHA-256, symbol,
   timeframe, data subscription type (adjusted?), export date, and the exact
   input/settings values, in a sibling `<name>.meta.json`.

The parity suite will then compare bar-by-bar values and trade sequences and
produce mismatch reports per docs/PARITY_REPORT.md.
