# Owner Data Export Runbook

How the owner assembles, attests, and verifies a market-data delivery that
`chronos data verify` will accept. Every step here is **run by the owner**.

**The boundary, precisely.** No agent or seat working in this repository runs any of it, and
no seat holds broker or vendor credentials. The capture process in §6 is different: when
**you** run it, it does connect to **your** gateway and read market data. That is an owner
action on owner credentials, and it is read-only — it opens no trading database, holds no
writer lease, and places no order.

The verifier is read-only: it writes no evidence, no release, and no corpus state. Running
it costs nothing and can be repeated while an export is being fixed.

## 1. First decide which lane the export is for

There are **two different data identities** in this repository and they are not
interchangeable. Delivering an export to the wrong one wastes the export.

| | **Six-symbol QQQ-robustness lane** | **Seven-symbol base Five-Tool lane** |
|---|---|---|
| symbols | `QQQ, SPY, IWM, DIA, GLD, TLT` — all six required | tradable `GLD, IWM, QQQ` + companion-only `RSP, SPY, VIX, VIX3M` (optional `ADD, TICK, VOLD`) |
| what accepts it | `chronos data verify` (this runbook) | the Five-Tool campaign's own intake |
| purpose | the robustness panel behind the QQQ readiness path | the base Five-Tool campaign |

**Cross-dataset identity transfer is forbidden in code.** A certified six-symbol release
does not become a Five-Tool input, and passing the gates in this runbook is *not*
sufficient to run the base Five-Tool campaign — that lane requires its own seven-symbol
dataset and catalog identity. See `docs/FIVE_TOOL_SEMANTICS.md` and
`docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md` for the Five-Tool side.

**This runbook covers the six-symbol lane only.**

Note the checked-in `research/data/raw/` corpus is *neither* identity: it holds
`SPY, QQQ, IWM, GLD, TLT` — missing `DIA` for the six-symbol contract and
`RSP, VIX, VIX3M` for the seven-symbol one. It is heterogeneous historical research data
and is not a delivery.

## 2. What to deliver

A directory containing at least these **required paths** (the parser reads exactly these and
ignores any other file you leave alongside them):

```
<delivery>/
  INTAKE.json
  bars/QQQ.csv   bars/SPY.csv   bars/IWM.csv   bars/DIA.csv   bars/GLD.csv   bars/TLT.csv
  corporate_actions/QQQ.json    ... one per symbol, same six ...
```

**All six symbols are required.** The verifier compares the `symbols` keys against its own
list and refuses a delivery that adds or omits one. A five-symbol delivery is not a partial
pass; it is `UNVERIFIED`.

> **`DIA` is the known blocker.** It has never been acquired — `research/data/raw/DATA_SOURCES.md`
> records it as confirmed absent from the panel that supplied the others. An export that
> cannot include DIA cannot satisfy this lane, and that is worth knowing before paying for
> data rather than after.

### `bars/<SYMBOL>.csv`

**Required columns** `date,open,high,low,close,volume`, one row per session, ascending, no
duplicate or weekend rows. Header names are normalised (case, surrounding space, spaces to
underscores) and column order does not matter; unknown extra columns are ignored rather than
refused. A *missing* required column fails the load. `date` is `YYYY-MM-DD` with **no time component** — a timestamped
cell is refused rather than truncated. An `adj_close` column is tolerated but unused.

Prices must be **unadjusted / as-traded**. Adjusted views are derived at read time from the
corporate-action stream; storing adjusted prices breaks the hash-pinned provenance the whole
contract rests on. See `research/data/history/README.md` for the store's side of this.

### `corporate_actions/<SYMBOL>.json`

A JSON array of split and cash-dividend events. This is load-bearing, not paperwork: any
close-to-close move at or beyond the material threshold that no action explains blocks
certification, and a split whose observed return does not match the declared ratio blocks it
separately. **Dividends must be in native as-of-ex-date basis**, never restated to a later
split's terms.

A symbol with genuinely no actions in its window still needs an entry — see §4.

### `INTAKE.json`

Keys are **exact**: a missing or unknown key is `UNVERIFIED`, not a warning.

```json
{
  "schema_version": 1,
  "delivery_id": "owner-2026Q3-sixsym-daily",
  "supersedes": null,
  "interval": "1d",
  "adjustment_policy": "unadjusted_as_traded",
  "provenance": {
    "source_id": "<vendor or export tool identity>",
    "source_receipt_sha256": "<64 hex chars: digest of the vendor's own receipt or export log>",
    "retrieved_at": "2026-09-05T00:00:00Z",
    "retrieval_method": "<UI export | API pull | download>",
    "license_note": "<redistribution status, in your own words>"
  },
  "symbols": {
    "QQQ": {
      "window": {"start": "2016-01-04", "end": "2026-06-30"},
      "bars_sha256": "<64 hex chars of bars/QQQ.csv>",
      "bar_count": 2637,
      "corporate_actions_sha256": "<64 hex chars of corporate_actions/QQQ.json>",
      "corporate_action_count": 42
    }
  },
  "corporate_action_attestation": { "...": "see §4" },
  "classified_moves": [
    {"symbol": "QQQ", "session_date": "2020-03-16", "reason": "COVID crash, not a corporate action"}
  ],
  "holdout_map": [
    {"symbol": "QQQ", "name": "qqq-clean-2025h2", "start": "2025-07-01", "end": "2026-06-30", "status": "clean"}
  ]
}
```

- `interval` must be `1d` and `adjustment_policy` must be `unadjusted_as_traded`; both are
  declarations, not options.
- `supersedes` is `null` for a first delivery, otherwise the prior release digest.
- `bar_count` and `corporate_action_count` are deliberately redundant with the files. They
  are independent claims the verifier can contradict, which is the point.
- `classified_moves` is the documented seam for real market events — one exact symbol, one
  date, one reason that ends up in the evidence.
- `holdout_map` entries take `symbol`, `name`, `start`, `end`, `status` and `reason`;
  `status` is `clean`, `seen`, or `burned`. `clean` is the untouched holdout; `seen` and
  `burned` have both been exposed to research already. **`reason` is optional for `clean` and
  `seen` but required for `burned`** — a burned span must record why it was consumed, and one
  without a reason is refused.

## 3. Per-symbol windows — request more history than the window you care about

Each symbol declares its own `window`, so an instrument that did not exist for the whole
range states its listing date instead of having a head truncation read as an acceptable
absence.

Two constraints decide what to actually buy:

**(a) Warm-up must precede the evaluation window, not eat it.** Indicators consume the
longest lookback before the first legal signal. The derived strategy specs in `specs/`
declare required lookbacks of a few hundred bars, and a benchmark-relative term adds a
200-period average of the price ratio on top. When history exists *before* the window,
warm-up is drawn from it and the whole window stays usable; when a file *starts* inside the
window, warm-up comes out of the window itself. The existing corpus shows the cost: `IWM`,
`GLD`, and `TLT` begin 2019-01-02, so a few hundred of their bars inside the frozen
2018–2021 validation window are spent warming up rather than being evaluated.

> **Request every symbol from 2016-01-01 or earlier** even if you only care about 2018
> onward. Roughly two years of lead-in covers a 200-bar warm-up with margin and costs
> almost nothing at daily resolution.

**(b) `SPY` must span the full evaluation window, because it is the benchmark.** Any
strategy with a relative-strength term reads `SPY` alongside the candidate, so a bar only
counts when **both** series have a close on it. The existing `SPY.csv` stops at 2019-11-14
— an export-cap artifact recorded in its manifest — which removes the last two years of the
frozen window for every benchmark-dependent strategy. Combined with (a) the two effects
compound: joined against that truncated `SPY` and then warmed up, `IWM`, `GLD`, and `TLT`
retain roughly twenty usable bars each inside the frozen window, and the four-symbol panel
retains a few hundred where it should hold a few thousand. Closing the `SPY` hole is the
single highest-value part of this export.

> **`SPY` must cover 2018-01-01 through 2021-12-31 continuously, plus the pre-2018 lead-in
> from (a).** Treat a gap in SPY as a gap in every symbol.

**Calendar coverage bounds.** Windows must fall inside the session calendar's covered range,
which currently starts in 1998 and ends at the close of 2026. A window outside it is
`UNVERIFIED` rather than silently accepted; extending the horizon is a separate reviewed
change to the calendar's own tables.

## 4. The independent corporate-action attestation

Code cannot do this half. It can only refuse to certify without it, and it does.

Sampling a **second, unrelated source** and confirming the action stream against it is an
owner act. If the export vendor and the attestation source are the same organisation, the
attestation attests nothing.

Two forms, and the distinction matters:

**One attestation covers the whole delivery**, so both forms must span all six symbols. A
one-symbol attestation is not a partial pass — it produces blocking findings for the five it
omits.

**Sampled actions** — you checked some actions against an independent source. Its `symbols`
must include **every** certified symbol; any symbol absent from that list gets a
`MISSING_ATTESTATION` finding of its own:

```json
{"kind": "sampled_actions", "source_id": "<the INDEPENDENT source>",
 "sampled_action_count": 12,
 "symbols": ["QQQ", "SPY", "IWM", "DIA", "GLD", "TLT"],
 "note": "<what you sampled and how>"}
```

**Reviewed, no actions** — an independent source confirms exact windows genuinely contain no
actions. Its `windows` must **exactly equal** the six `symbols[].window` ranges in
`INTAKE.json` — same symbols, same start and end dates, no more and no fewer. Any difference
is a `NO_ACTION_ATTESTATION_MISMATCH`:

```json
{"kind": "reviewed_no_actions", "source_id": "<the INDEPENDENT source>",
 "windows": [{"symbol": "QQQ", "start": "2016-01-04", "end": "2026-06-30"},
             {"symbol": "SPY", "start": "2016-01-04", "end": "2026-06-30"},
             {"symbol": "IWM", "start": "2016-01-04", "end": "2026-06-30"},
             {"symbol": "DIA", "start": "2016-01-04", "end": "2026-06-30"},
             {"symbol": "GLD", "start": "2016-01-04", "end": "2026-06-30"},
             {"symbol": "TLT", "start": "2016-01-04", "end": "2026-06-30"}],
 "note": "<what was reviewed>"}
```

This form is also contradicted by evidence: if the delivery supplies **any** corporate action
at all, a `reviewed_no_actions` attestation earns a
`NO_ACTION_ATTESTATION_CONTRADICTED` finding. In practice a six-symbol equity-ETF delivery
over a multi-year window will have dividends, so `sampled_actions` is the form you will
almost certainly use.

Do **not** use the sampled form with a count against an empty action panel. That combination
is refused on purpose: an unexpectedly empty multi-decade capture needs separately reviewed
evidence naming exact windows, not a free-form note. Use `reviewed_no_actions`.

## 5. Verify the delivery

```
chronos data verify --delivery /path/to/delivery
```

Three outcomes, and the distinction between the last two is the important one:

| output | exit | meaning | what to do |
|---|---|---|---|
| `CERTIFIED <path>: <digest>` | 0 | the certification gates passed — see the scope note below | record the digest |
| `NOT_CERTIFIED <path>: N blocking finding(s): ...` | 1 | the gates **ran** and something failed | fix the data or supply the missing classification |
| `UNVERIFIED <path>: <reason>` | 2 | the gates **could not run** | fix the delivery, then re-run |

`UNVERIFIED` is not a softer failure — it means no judgement was possible, so nothing about
the data has been established either way. Its common causes:

- `INTAKE.json` missing, unparseable, or carrying an unexpected or missing key
- the `symbols` set is not exactly the six
- a declared `bars/<SYMBOL>.csv` or `corporate_actions/<SYMBOL>.json` that is absent or
  unreadable
- a `holdout_map` span that is malformed — a bad date, an unknown `status`, or a `burned`
  span with no `reason`
- a `bars_sha256` or `corporate_actions_sha256` that disagrees with the file on disk
- a `bar_count` or `corporate_action_count` that disagrees with what parsed
- a CSV that is unparseable, or a date cell carrying a time component
- a window outside the session calendar's covered range

A hash mismatch is `UNVERIFIED` rather than `NOT_CERTIFIED` deliberately: the bytes on disk
are not the bytes attested to, so any verdict would describe a different artifact than the
manifest does.

### What `CERTIFIED` does and does not cover

Two limits, stated because both are easy to over-read:

**It is not a check of your holdout map.** `data verify` parses every `holdout_map` span and
refuses a malformed one, but it does **not** pass the map to the certification gates, so
**complete tiling is not checked here**. A delivery whose map covers one symbol's window and
omits the rest still reports `CERTIFIED`. The requirement that the map tile each symbol's
window exactly once is enforced when a certified delivery is frozen into a release — a later
step, not this one.

**The printed digest identifies the certification report, not your bar content.** It is a
digest over the evidence — coverage, findings, the attestation, the corporate-action
summary, the classified moves — and carries no hash of the OHLCV values. Two deliveries with
different bar values but identical coverage and no findings print the **same** digest, so it
is not a fingerprint of the data and must not be used as one. What binds the raw bytes is the
per-symbol `bars_sha256` and `corporate_actions_sha256` in `INTAKE.json`, which are
recomputed and compared during the run — a mismatch there is `UNVERIFIED`. The digest that
identifies a specific frozen dataset is the release digest, minted at the freeze step.

Read `CERTIFIED` as: *the bytes I attested to are the bytes that were judged, and they passed
the frozen coverage, corporate-action and quality gates.* Nothing more.

`NOT_CERTIFIED` findings name their kind and symbol — missing sessions, coverage below the
floor, an unclassified material move, an unreconciled split, a blocking quality issue, or a
missing/contradicted attestation. Every finding blocks; there are no warnings.

## 6. A candidate export path you already have: IBKR TWS/Gateway

You hold an IBKR account with a TWS/Gateway entitlement, so the historical daily bars this
lane needs may be obtainable without a new subscription. **This is a candidate for you to
evaluate and run, not a recommendation and not something any seat here can do.** No seat has
broker credentials and no seat runs this command. When you run it, it does connect to your
gateway and read market data — read-only, on your own credentials.

The repository ships a read-only capture process for this:

```
python -m chronos.histdata bars --symbols QQQ,SPY,IWM,DIA,GLD,TLT --bar-size 1d \
    --end-date <YYYY-MM-DD> --duration-days <N>
```

It runs as a standalone read-only process against your own gateway with a dedicated client
id, opens no trading database, holds no writer lease, and imports no order or broker module.
Set-up, ports, and reconnect handling are in `docs/IBKR_RUNBOOK.md`; pacing is handled by the
process.

**Two honest caveats before you rely on it:**

1. **It populates the store, not a delivery.** That command writes into
   `research/data/history/` with the store's own manifest. It does **not** produce an
   `INTAKE.json` delivery directory, so assembling §2's layout from its output is a manual
   step today. The two paths converge later, not here.
2. **Whether IBKR can supply the full window for all six symbols is unverified.** Duration
   limits, pacing, and per-instrument availability are properties of your entitlement, and
   `DIA` in particular has never been obtained from any source in this repository. Pull a
   short range for one symbol first and check it against §2 before planning around it.

If IBKR does cover it, that is the cheapest path available and it uses an entitlement you
already pay for. If it does not, the export becomes a purchasing decision — and §3's two
constraints are what that purchase has to satisfy.

## 7. What this runbook does not do

It does not make data trustworthy, authorize a campaign, count a trial, unlock a holdout, or
select a strategy. `chronos data verify` reports a verdict over bytes you supplied and writes
nothing. Freezing a certified delivery into an immutable release, and everything downstream
of that, is separate work that runs only after a delivery certifies.
