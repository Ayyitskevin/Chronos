# Historical-data store (ADR-0011, AI Quant plan C1)

The go-forward, IBKR-sourced historical store, written by the read-only data process
(`python -m chronos.histdata`). It ships **empty**: C1 delivers the pipeline; the first
real backfill is an owner-run step against a live gateway.

## Layout

```
research/data/history/
  bars/<SYMBOL>.csv                 # UNADJUSTED as-traded daily OHLCV (never re-adjusted)
  corporate_actions/<SYMBOL>.json   # the split + cash-dividend event stream
  MANIFEST.json                     # per-symbol provenance (sha256, range, capture)
  HOLDOUTS.json                     # declared, default-embargoed holdout windows (C1-e)
```

## Contract

- **Bars are unadjusted.** Adjusted / total-return views are derived at read time from
  the corporate-action stream (`chronos.histdata.adjust`) and never written back, so the
  hash-pinned unadjusted series stays the single source of truth.
- **Dividends are stored in native as-of-ex-date basis** — never restated to a later
  split's terms — or the read-time factor double-counts the split (ADR-0011 §11.11).
- **Writes are idempotent and fail-closed.** A re-fetch that reproduces the stored rows
  is a no-op; a conflicting row aborts unless a deliberate, logged
  `allow_correction` supersede is passed. Every series passes the `marketdata` quality
  gate before write.
- **Not the legacy corpus.** `research/data/raw/` (the heterogeneous 5-ETF CSVs) is a
  separate, unchanged store; C1 does not migrate or reconcile it.

This is distinct from the trading plane: the data process opens no trading database,
holds no writer lease, and imports no order/broker module.
