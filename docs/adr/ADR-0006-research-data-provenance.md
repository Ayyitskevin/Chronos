# ADR-0006 — Research data from public dataset mirrors, with per-file provenance

Status: Accepted (2026-07-17). Index entry: DECISIONS.md D-07.

## Context

No brokerage market data is reachable from this build environment: there are no IBKR credentials
and no running TWS/Gateway (ASSUMPTIONS.md A-30). Direct market-data vendor endpoints are likewise
unreachable. Quantitative validation of the candidate strategies still requires historical daily
OHLCV data with a defensible chain of custody.

## Decision

- Acquire historical daily OHLCV from public dataset mirrors through authenticated connector
  access, storing raw CSV files under `research/data/raw/` together with a `MANIFEST.json`
  recording, per file: source, retrieval method, SHA-256 hash, and integrity-validation results.
- Load data through the strict CSV provider (`src/chronos/marketdata/csv_provider.py`): missing
  required columns or unparseable rows fail the load; every load returns the file's SHA-256.
- Validate every series with the fail-closed quality checker (`src/chronos/marketdata/quality.py`);
  blocking issues (impossible OHLC, non-positive prices, duplicates, unclosed bars) lock order
  generation, and research runs record the quality report next to results.
- Stamp every research result with code commit, data hash, and risk-policy hash
  (`src/chronos/research/runner.py`), so a result that cannot name its inputs does not exist.
- IBKR historical data is the intended production source once the owner supplies credentials.

## Consequences

- All research conclusions carry an explicit data-provenance caveat: the owner should re-run
  research from IBKR historical data before any promotion beyond paper (A-30).
- Split/dividend handling follows the source's adjusted series where provided, with raw OHLC
  retained (A-32); this is an approximation and is documented with the results.
- Intraday data was not reliably obtainable, so intraday corpus strategies are research-only
  regardless of code quality (A-31, ADR-0008).
- As of this writing `research/data/raw/` is being populated by the data-acquisition task
  (TASKS.md "In flight"); backtests fail closed with a clear error when a symbol's CSV is absent.
