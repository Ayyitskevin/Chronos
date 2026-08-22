# Historical-data plane runbook (C1 / ADR-0011, C0 / ADR-0012)

The `chronos.histdata` process ingests IBKR data into a local, file-based store: it
backfills historical daily **bars** and captures forward option-chain **snapshots**.
It is **read-only** with respect to the trading plane: it opens no trading database,
holds no writer lease, and imports no order/broker module.

## What it is

- A standalone process with two subcommands: `python -m chronos.histdata bars ...` and
  `python -m chronos.histdata options ...`.
- Connects to TWS/Gateway with the dedicated **`IB_DATA_CLIENT_ID`** (default 18, must
  differ from `IB_CLIENT_ID`; id 0 is the TWS master id and is rejected).
- Bars are written **unadjusted**; adjusted / total-return views are derived at read
  time from the corporate-action stream, never written back.

## Prerequisites (owner)

1. Install the official TWS API (`ibapi`) — it is not on PyPI and not a dependency;
   see `docs/ibkr_setup.md`. Without it the process reports a clear "ibapi not
   installed" per-symbol error rather than crashing.
2. A running TWS or IB Gateway (paper or live — historical bars are read-only either
   way), reachable at `IB_HOST:IB_PORT`.
3. Set `IB_DATA_CLIENT_ID` to a value not used by the trading backend.

## Run — historical bars

```bash
python -m chronos.histdata bars --symbols SPY,QQQ --end-date 2024-12-31 --duration-days 365
```

Hourly bars are their own lane (ADR-0029): add `--bar-size 1h` (and optionally
`--chunk-days`, default 30 — conservative under IBKR's per-bar-size duration caps,
which are unverified in this repo; raise it only after the first real run confirms
the actual cap). The hourly path requests `formatDate=2` (epoch) and RTH only, paces
at a stricter 4/min, runs oldest-first, records empty pre-horizon chunks by end-date
in the per-symbol JSON line (`empty_chunks`), and never ingests a bar that has not
closed yet — so a backfill run during market hours cannot store the forming bar as
the session's closing print. First-run verification items for hourly: actual depth horizon per symbol,
bars arriving start-stamped 09:30…15:30 with the final bar spanning to the close,
half-days ending at 13:00, and volume units at intraday resolution.

Each symbol is paced (a conservative rolling budget + per-key cooldown), fetched,
quality-gated, and written idempotently. Output is one JSON line per symbol
(`{symbol, rows, added, error}`); a non-zero exit means at least one symbol failed.

## Run — options forward capture (deploy ASAP)

```bash
python -m chronos.histdata options --symbols SPY,QQQ [--session YYYY-MM-DD] \
    [--horizon-days 120] [--strike-window-pct 0.20]
```

Captures one **immutable EOD snapshot** per underlying per day of a **bounded** slice
of the chain — expirations within the horizon and strikes within the band of spot,
both rights — with each row labeled by its quote's `DataQuality` and a per-snapshot
staleness histogram recorded. **Schedule this daily** (cron/systemd-timer against your
gateway): IBKR keeps *no* history for expired options, so every un-captured session is
unrecoverable. Output is one JSON line per underlying
(`{underlying, rows, worst_quality, reason, error}`). The $0 tier is delayed/EOD
quality — captured and labeled `DELAYED`, never presented as live.

Example daily cron (owner machine, after the US close):

```cron
15 21 * * 1-5  cd /path/to/Chronos && .venv/bin/python -m chronos.histdata options --symbols SPY,QQQ,IWM
```

## Store layout (`research/data/history/`)

| Path | Contents |
|---|---|
| `bars/<SYMBOL>.csv` | unadjusted daily OHLCV (`date,open,high,low,close,volume`) |
| `corporate_actions/<SYMBOL>.json` | split + cash-dividend event stream (native basis) |
| `options/<SYMBOL>/<YYYY-MM-DD>.json` | one immutable EOD option-chain snapshot |
| `MANIFEST.json` | per-symbol provenance: sha256, ranges, capture time, corrections, option snapshots |
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

## First-run verification (owner, once)

Both official clients are unexercised in CI. On the first real **bars** run confirm:

1. Bars land in `bars/<SYMBOL>.csv` with plausible OHLCV and the expected date range.
2. Volume units look right (TWS historical volume can be in lots for some feeds) — if
   not, the owner records the adjustment before relying on volume.
3. Bar dates parse as `YYYYMMDD` (the process assumes `formatDate=1` daily bars).
4. No pacing violations from the gateway across a multi-symbol run.
5. `MANIFEST.json` records a sha256 and range for each symbol.

On the first real **options** capture confirm:

1. A snapshot lands at `options/<SYMBOL>/<DATE>.json` with rows only inside the
   configured horizon/strike window (anything outside is absent by policy).
2. `worst_quality` reflects your data tier honestly — `DELAYED`/`DELAYED_FROZEN` on the
   $0 tier, `LIVE` only with a paid subscription. If it reads `LIVE` unexpectedly,
   verify the account's option market-data permissions before trusting it.
3. `implied_volatility`/greeks are populated where the gateway returned them and `null`
   where it did not — none are fabricated.
4. The official option client's `reqSecDefOptParams` / `reqMktData` greek wiring is
   owner-completed on the live gateway; verify tick semantics on this first run.
