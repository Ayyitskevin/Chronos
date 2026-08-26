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

For the reviewed six-symbol QQQ campaign packet, use
[`qqq_certified_data_wizard.sh`](../scripts/qqq_certified_data_wizard.sh) and the complete
workflow in [certified_data_runbook.md](certified_data_runbook.md). The direct command below
is the generic exporter; by itself it does not supply corporate actions, an independent
attestation, a holdout map, certification, or a frozen release.

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

### Before you schedule it: run the preflight

```bash
python scripts/preflight_options_capture.py               # offline; opens no socket
python scripts/preflight_options_capture.py --connect     # adds one bounded read-only chain fetch
```

It checks every prerequisite above — `ibapi` importable, settings loadable,
`IB_DATA_CLIENT_ID` distinct from `IB_CLIENT_ID`, the gateway accepting connections, the
history root writable, and the session-label hazard below — reports *all* of them rather
than stopping at the first, names the fix for each, and exits non-zero while anything is
unmet. `--print-units` emits ready-to-install systemd user units.

### The session-label hazard — read this before choosing a schedule

`--session` defaults to the **UTC** date. A job scheduled in a local timezone west of UTC
can therefore cross UTC midnight and file the session under the wrong day. This is not
hypothetical, and it is invisible after the fact — a mislabeled snapshot looks exactly
like a real one, and the data cannot be re-fetched to correct it.

```
$ systemd-analyze calendar "Mon..Fri 21:15"        # unpinned == LOCAL time
    Next elapse: Mon 2026-08-24 21:15:00 EDT
       (in UTC): Tue 2026-08-25 01:15:00 UTC       <-- next UTC day: session filed as tomorrow

$ systemd-analyze calendar "Mon..Fri 21:15 UTC"    # pinned
    Next elapse: Mon 2026-08-24 17:15:00 EDT       <-- 75 min after the close
       (in UTC): Mon 2026-08-24 21:15:00 UTC       <-- same UTC day: session labeled correctly
```

On an `America/New_York` host the unpinned form files Thursday's chain as Friday, and
**Friday's as Saturday — a date on which no session exists**. Either pin the schedule to
UTC, or pass `--session` explicitly. The old cron example in this runbook was the unpinned
form and is corrected below.

### Schedule it — systemd user timer (preferred)

Preferred over cron because the run's JSON output lands in the journal, where a failed or
missed capture is visible after the fact rather than mailed into the void (R12:
automation must stay observable).

```bash
python scripts/preflight_options_capture.py --print-units --symbols SPY,QQQ,IWM
# write the two blocks to ~/.config/systemd/user/, then:
systemctl --user daemon-reload
systemctl --user enable --now chronos-options-capture.timer
systemctl --user list-timers chronos-options-capture.timer   # confirm the next elapse
sudo loginctl enable-linger $USER                            # unattended hosts only
journalctl --user -u chronos-options-capture -n 50           # after the first run
```

The timer is deliberately **not** `Persistent=true`. A missed session cannot be recovered
— IBKR keeps no history for expired options — so a catch-up run would fetch *today's*
chain and file it under the missed date. A visible gap beats a plausible wrong row.

Equivalent cron, if you prefer it — note the explicit UTC handling, which the schedule
cannot express on its own:

```cron
# 21:15 UTC Mon-Fri. CRON_TZ is honored by cronie/vixie-cron; without it this line means
# 21:15 LOCAL and mislabels the session on any host west of UTC.
CRON_TZ=UTC
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
- **Certification binds action semantics, not provider completeness.** The v3 report records
  each declared symbol's distinct in-window action count and order-invariant semantic digest;
  duplicate events and inflated independent-sample counts refuse. The owner still verifies
  sponsor completeness and the independent source on the first real capture.
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
6. Each corporate-action manifest count equals its parsed file, no exact event repeats, and
   the independent sampled count does not exceed the supplied distinct events.

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
