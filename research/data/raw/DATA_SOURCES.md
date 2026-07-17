# Data Sources — research/data/raw

Honest record of how the daily OHLCV data in this directory was acquired,
what was verified, and what could **not** be obtained. Machine-readable
details (hashes, row counts, per-file validation) are in `MANIFEST.json`.

## Acquisition context

- Date: 2026-07-17.
- The sandbox egress proxy returns 403 for all finance sites **and** for
  direct `huggingface.co` HTTPS. The only working channel to external data
  was the server-side Hugging Face MCP connector (`hf_fs` tools).
- `hf_fs cat` is a **text** transport with an 80,000-byte-per-call cap.
  Practical consequence: only plain CSV/JSONL files could be retrieved.
  Parquet, zip, and gated datasets were out of reach, which excluded the
  richest ETF datasets on the Hub (see "What could not be obtained").
- Files were downloaded in 80 KB chunks and reassembled **byte-exactly**:
  each assembly was verified by (a) exact match of total byte count against
  the repo-reported file size and (b) `cmp`-identical overlap regions
  between independently fetched chunks. No row was ever re-typed, edited,
  interpolated, or synthesized.

## Acquired

### SPY.csv — 5,000 rows, 2000-01-03 → 2019-11-14 (UNADJUSTED)

- Source: `https://huggingface.co/datasets/mmirmomeni/spy_daily`
  (`train.jsonl`, 629,596 bytes, sha256 `e20cd976…b930fa`).
  Investing.com-style export (fields `Date, Price, Open, High, Low, Vol., Change %`).
- Transform: rename `Price`→`close`; `Vol.` `"52.00M"` → integer shares
  (×1e6; quantized to ~10k shares); sort ascending.
- License: none declared by the uploader; treat as research-use only.
- Validation: zero OHLC-invariant violations, zero duplicate/out-of-order/
  weekend dates, only calendar gap is the 9/11 closure; per-year row counts
  match the NYSE calendar. Spot checks match famous values exactly
  (GFC bottom close 68.11 on 2009-03-09; 113.33 on 2010-01-04; 146.06 on
  2013-01-02).
- Cross-check: 8 dates compared against an independent lineage
  (`zexianli/nasdaq_data`, FNSPID/yfinance-derived): open & close agree to
  the penny (max deviation 1.3e-05, i.e. float32 noise).
- **Known shortfall**: the series stops at 2019-11-14 (exactly 5,000 rows —
  almost certainly an Investing.com export cap hit by the uploader). The
  2019-11-15 → 2024 tail was NOT acquired (see below). Prices are not
  dividend-adjusted and there is no `adj_close` column.

### QQQ.csv — 6,087 rows, 1999-11-01 → 2024-01-10 (unadjusted OHLC + adjusted close)

- Source: `https://huggingface.co/datasets/Maxim37/timeseries-QQQ-1d-25yr`
  (`QQQ_data.csv`, 446,687 bytes, sha256 `b583e11b…7105a2`).
  Alpha Vantage `TIME_SERIES_DAILY_ADJUSTED` layout (includes
  `adjusted_close`, `dividend_amount`, `split_coefficient`).
- Transform: sort ascending; keep OHLCV + `adjusted_close` (as `adj_close`);
  dividend/split columns used for validation then dropped.
- License: none declared by the uploader; treat as research-use only.
- Validation: zero invariant violations; the historical 2:1 split on
  2000-03-20 is present with a continuous adjusted series across it;
  78 dividend ex-dates; only calendar gap is the 9/11 closure. Spot checks
  match known values (409.52 on 2023-12-29; 170.46 COVID bottom on
  2020-03-23; 25.74 GFC bottom on 2009-03-09).
- Cross-check: 7 dates in Dec-2023 against `zexianli/nasdaq_data`
  (independent yfinance lineage): open & close agree to the penny.
- Caveats: ends 2024-01-10; `adj_close` anchored to that download date;
  volume not independently cross-checked.

## Cross-check reference (not shipped as data)

`https://huggingface.co/datasets/zexianli/nasdaq_data`
(`price_movement/<SYM>.csv`) — derived from FNSPID (CC BY-NC-4.0 upstream,
yfinance price lineage). It carries only unadjusted **open/close** (high/low/
volume were dropped by its build script `main.py`), so it was used solely to
independently verify our rows, never as a data source.

## What could NOT be obtained (and why)

| Symbol | Status | Best leads found (all unusable over text-only transport) |
|--------|--------|----------------------------------------------------------|
| IWM    | ✗ no OHLCV | `P2SAMAPA/etf_trend_data` `market_data.csv` has **adjusted close only** 2008→2026; OHLCV exists only in parquet (`P2SAMAPA/*`, `younginpiniti/us-stocks-daily-all`) or gated (`paperswithbacktest/ETFs-Daily-Price`, gated: manual) |
| DIA    | ✗ no OHLCV | Not even in the adjusted-close panels found; open/close-only history likely in `zexianli/nasdaq_data` |
| GLD    | ✗ no OHLCV | Adjusted close only, 2008→2026, in `P2SAMAPA/etf_trend_data`; otherwise parquet/gated as above |
| TLT    | ✗ no OHLCV | Same as GLD |
| SPY 2019-11→2024 | ✗ | Open/close-only continuation to 2023-12-28 in `zexianli/nasdaq_data`; full OHLCV only in parquet/gated datasets |

Datasets that would close every gap if a binary-capable transport (or gate
approval) ever becomes available:

- `paperswithbacktest/ETFs-Daily-Price` — all US ETFs, daily, parquet, **gated (manual)**
- `younginpiniti/us-stocks-daily-all` — Ticker/Date/OHLCV parquet, 799k rows
- `P2SAMAPA/P2-ETF-DQN-ENGINE-DATASET` (`data/etf_prices.parquet`) and
  `P2SAMAPA/fi-etf-macro-signal-master-data` — ETF OHLCV panels incl. GLD/TLT/IWM
- `Zihan1004/FNSPID` (`Stock_price/full_history.zip`, 590 MB) — per-symbol
  full-history CSVs (inside a zip), CC BY-NC-4.0

**No synthetic data was created.** Missing symbols/periods are simply absent.

## Intraday side-finding

Real US-equity intraday data exists on the Hub in plain CSV:
`Maxim37/timeseries-1m-QQQ-5y` (1-minute QQQ OHLCV 2019→2024, 65.5 MB,
includes pre-market; an 800-byte sample was inspected and looked correct).
Also: 15-minute per-sector US stocks (`brandonyeequon/stock-market-data-warehouse`),
1-minute S&P 500 constituents (`jwigginton/timeseries-1mn-sp500`, parquet),
and SPX/MES futures + VIX (`thillsss/SPX-MES-VIX-data`).
