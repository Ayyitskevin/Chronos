# Historical-data plane runbook (C1 / ADR-0011)

The `chronos.histdata` process backfills IBKR historical daily bars into a local,
file-based store. It is **read-only** with respect to the trading plane: it opens no
trading database, holds no writer lease, and imports no order/broker module.

## What it is

- A standalone process: `python -m chronos.histdata`.
- Connects to TWS/Gateway with the dedicated **`IB_DATA_CLIENT_ID`** (default 18, must
  differ from `IB_CLIENT_ID`; id 0 is the TWS master id and is rejected).
- Writes **unadjusted** as-traded bars; adjusted / total-return views are derived at
  read time from the corporate-action stream, never written back.

## Prerequisites (owner)

1. Install the official TWS API (`ibapi`) — it is not on PyPI and not a dependency;
   see `docs/ibkr_setup.md`. Without it the process reports a clear "ibapi not
   installed" per-symbol error rather than crashing.
2. A running TWS or IB Gateway (paper or live — historical bars are read-only either
   way), reachable at `IB_HOST:IB_PORT`.
3. Set `IB_DATA_CLIENT_ID` to a value not used by the trading backend.

## Run

```bash
python -m chronos.histdata --symbols SPY,QQQ --end-date 2024-12-31 --duration-days 365
```

Each symbol is paced (a conservative rolling budget + per-key cooldown), fetched,
quality-gated, and written idempotently. Output is one JSON line per symbol
(`{symbol, rows, added, error}`); a non-zero exit means at least one symbol failed.

## Store layout (`research/data/history/`)

| Path | Contents |
|---|---|
| `bars/<SYMBOL>.csv` | unadjusted daily OHLCV (`date,open,high,low,close,volume`) |
| `corporate_actions/<SYMBOL>.json` | split + cash-dividend event stream (native basis) |
| `MANIFEST.json` | per-symbol provenance: sha256, date range, capture time, corrections |
| `HOLDOUTS.json` | declared, default-embargoed holdout windows |

## Contract & safety

- **Idempotent + fail-closed.** A re-fetch that reproduces stored rows is a no-op; a
  *conflicting* row for an existing date aborts. A genuine vendor correction is applied
  only with an explicit, logged `allow_correction` supersede — history is never
  silently rewritten.
- **Quality-gated.** Every series passes `marketdata.quality.validate_series` before
  write; a blocking issue (impossible OHLC, non-positive price, duplicate, non-finite)
  aborts the write.
- **Corporate actions are native-basis.** Store a dividend as the raw as-declared
  amount at its own ex-date, never restated to a later split's terms.
- **Isolation is enforced by tests** (`tests/safety/test_histdata_isolation.py`): the
  package imports nothing from the order/broker/persistence/lease planes or
  `sqlalchemy`/`sqlite3`, and `ibapi` stays lazy.

## First-backfill verification (owner, once)

The official client is unexercised in CI; on the first real run confirm:

1. Bars land in `bars/<SYMBOL>.csv` with plausible OHLCV and the expected date range.
2. Volume units look right (TWS historical volume can be in lots for some feeds) — if
   not, the owner records the adjustment before relying on volume.
3. Bar dates parse as `YYYYMMDD` (the process assumes `formatDate=1` daily bars).
4. No pacing violations from the gateway across a multi-symbol run.
5. `MANIFEST.json` records a sha256 and range for each symbol.
