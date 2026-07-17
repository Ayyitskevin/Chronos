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
  Practical consequence: only plain CSV/JSONL files could be retrieved
  byte-exactly. `hf_fs cat` **refuses binary files** (parquet/zip).
- **Second pass (same date):** a *different* MCP path — `hub_repo_details`
  with `operations=["dataset_preview"]` — reads Dataset-Viewer-converted
  **parquet** rows as markdown tables (config+split, offset+limit≤100).
  This unlocked adjusted ETF OHLCV that the text transport could not reach
  (IWM, GLD, TLT below). It is **not** byte-exact: rows are transcribed from
  the returned markdown and rounded to 2 decimals; fidelity is defended by
  per-page OHLC-invariant + monotonic-weekday-date checks and an independent
  close cross-check. This is a lower-fidelity transport than the byte-exact
  `cat` used for SPY/QQQ, and the manifest labels it as such.
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

### IWM.csv / GLD.csv / TLT.csv — 757 rows each, 2019-01-02 → 2021-12-31

- Source: `https://huggingface.co/datasets/P2SAMAPA/p2-etf-trendfolios-replication-data`,
  config **`ohlcv`** (`ohlcv/train-00000-of-00001.parquet`, 9.1 MB, 221,500
  rows, 38 ETF tickers, columns `date,ticker,open,high,low,close,volume`,
  sorted by ticker then date). Read via `dataset_preview` markdown paging,
  filtered to each ticker.
- **Adjusted status** (verified by spot values + cross-check):
  - **GLD — effectively NOMINAL.** GLD pays no distribution, so its adjusted
    series equals as-traded prices. Matches known nominal values to the penny
    (2019-01-02 close 121.33; Aug-2020 record-region close 193.89 (high 194.45); 2021-12-31 170.96).
  - **IWM — dividend-ADJUSTED** (back-adjusted to a ~2026 anchor). ~4–8% below
    nominal (2019-12-31 close 152.97 vs nominal ~165.8).
  - **TLT — heavily dividend-ADJUSTED.** ~19% below nominal at series start, ~15% by end-2021 (2019-01-02 close
    98.69 vs nominal ~122; 2021-12-31 126.28 vs nominal ~149). Still genuine
    internally-consistent OHLCV (open/high/low/close on the same scale) — **not**
    a close-only series.
- Transform: filter ticker + 2019-01-02…2021-12-31; drop ticker column; round
  OHLC to 2 dp and volume to integer; sort ascending.
- Validation: all three have **zero** OHLC-invariant / duplicate / out-of-order /
  weekend violations, and per-year trading-day counts **exactly** match the NYSE
  calendar (2019=252, 2020=253, 2021=252). Each captures the Feb–Mar 2020 COVID
  crash correctly (IWM low 88.87, GLD liquidation low 136.12, TLT flight-to-quality
  spike high 149.07).
- Cross-check: closes compared against the **independent** adjusted-close panel
  `P2SAMAPA/etf_trend_data/market_data.csv` (read byte-exact via `cat`). On
  2019-09-11/12: **GLD matches penny-exact** (141.03/141.32 both sides); IWM and
  TLT agree to a *constant* small scale factor (IWM ratio 1.0024, TLT 1.0039) —
  the exact signature of two dividend-adjusted series anchored to different
  download dates, confirming transcription correctness and adjusted status.
- **Known shortfalls**: (1) 2019–2021 window only — the source holds FULL history
  (IWM 2000→, GLD 2004→, TLT 2002→, all to 2026) extractable with the same method,
  not completed this session due to markdown-transport volume; (2) prices are
  ADJUSTED (except GLD which is nominal) and rounded to 2 dp — do NOT mix the IWM/TLT
  files with nominal series; (3) reconstructed by transcription, so bounded sub-cent
  O/H/L/V noise is possible (unlike the byte-exact SPY/QQQ files).

### DIA — still not acquired (confirmed ABSENT from the panel)

DIA sorts between AGG and EEM, but the `ohlcv` panel jumps straight from AGG to
EEM (no B/C/D tickers), so **DIA is not present**. It exists in
`paperswithbacktest/ETFs-Daily-Price` (gated: 404 to this account) and in
`siddharthmb/stocks-ohlcv` (unadjusted, but a 1.2 GB **date-major** single CSV —
one symbol is scattered across the whole file, so `cat` extraction would need
~15,000 chunks; impractical). No usable DIA OHLCV over this transport.

## Cross-check reference (not shipped as data)

`https://huggingface.co/datasets/zexianli/nasdaq_data`
(`price_movement/<SYM>.csv`) — derived from FNSPID (CC BY-NC-4.0 upstream,
yfinance price lineage). It carries only unadjusted **open/close** (high/low/
volume were dropped by its build script `main.py`), so it was used solely to
independently verify our rows, never as a data source.

## What could NOT be obtained (and why)

Updated after the second (parquet-preview) pass:

| Symbol | Status | Notes |
|--------|--------|-------|
| IWM    | ◑ 2019–2021 adjusted OHLCV delivered | `IWM.csv` (757 rows). Full 2000→ history extractable from the same `ohlcv` parquet via `dataset_preview`; not completed this session |
| GLD    | ◑ 2019–2021 (effectively nominal) OHLCV delivered | `GLD.csv` (757 rows); cross-check penny-exact. Full 2004→ extractable |
| TLT    | ◑ 2019–2021 adjusted OHLCV delivered | `TLT.csv` (757 rows). Full 2002→ extractable |
| DIA    | ✗ not acquired | **Confirmed absent** from the `ohlcv` panel (AGG→EEM directly). Present only in gated `paperswithbacktest/ETFs-Daily-Price` (404) and in date-major 1.2 GB `siddharthmb/stocks-ohlcv` (not per-symbol extractable via `cat`) |
| SPY 2019-11→2024 | ✗ (unadjusted) | An **adjusted** SPY 1999→2026 exists in the `ohlcv` parquet and is extractable via `dataset_preview`, but was not delivered to avoid conflicting with the existing UNADJUSTED `SPY.csv`; would need a separate clearly-named file |

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
