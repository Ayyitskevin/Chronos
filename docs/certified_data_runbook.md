# Certified-data runbook — turning an IBKR export into a Phase 3 dataset

Phase 3 (`docs/VISION_COMPLETION_PLAN.md` §8) requires certified data before any
research campaign, and D-29 stacks the whole deep-trading programme behind it. This
runbook is the executable half. The other half — running TWS and pulling the history —
is the owner's and cannot be automated.

## What "certified" means here

Four gates, frozen before collection and not tunable at a call site:

| Gate | Enforced by |
|---|---|
| At least 99.5% expected-session coverage | `research.certification` against `research.session_calendar` |
| Every gap and material move classified; zero unresolved conflicts | `MISSING_SESSION`, `UNEXPECTED_BAR`, `UNCLASSIFIED_MATERIAL_MOVE` findings |
| Corporate actions independently sampled **and** reconciled | attestation required (owner) + split/price reconciliation (code) |
| Clean/seen/burned holdout map, complete and content-addressed | `research.dataset_release` tiling + partition digests |

Every finding blocks. There are no warnings, and there is no flag that downgrades one.

## Step 1 — pull the history (owner)

```bash
python -m chronos.histdata bars --symbols SPY,QQQ,IWM,DIA,GLD,TLT --end-date 2026-08-21 --duration-days 9500
```

Prerequisites are in [histdata_runbook.md](histdata_runbook.md): the official TWS API
installed, a running gateway, and `IB_DATA_CLIENT_ID` distinct from `IB_CLIENT_ID`.
Bars land unadjusted in `research/data/history/bars/<SYMBOL>.csv` with per-symbol
provenance in `MANIFEST.json`.

**The hourly leg (ADR-0029):** hourly ingestion exists and runs chunked and paced:

```bash
python -m chronos.histdata bars --symbols SPY,QQQ,IWM,DIA,GLD,TLT \
    --bar-size 1h --end-date 2026-08-21 --duration-days 5000 --chunk-days 30
```

Bars land in `bars_1h/<SYMBOL>.csv` with real close timestamps. Two truths to hold
while planning: IBKR's intraday depth horizon is far shorter than its daily depth and
is not recorded anywhere in this repo — chunks before the horizon come back empty and
are skipped, and the certifier judges whatever window results, so **declare hourly
windows over what actually landed**, not over the daily range. And minute intervals
still refuse everywhere: vocabulary, no path. D-29's C1/C2 can begin on daily bars;
the hourly release is additive.

## Step 2 — sample the corporate actions independently (owner)

Code cannot do this. Take a sample of splits and dividends from a source unrelated to
the export, reconcile them by hand, and record what you did in the declaration's
`attestation` block. An export with no attestation is refused — self-consistency is not
a second source.

## Step 3 — write the declaration

One JSON file, deliberately explicit; nothing about a holdout map is inferred.

```json
{
  "dataset_id": "chronos-etf-daily-v1",
  "catalog_id": "chronos-etf-daily-v1-release-001",
  "source_id": "ibkr-tws-historical",
  "source_receipt_sha256": "<64 hex>",
  "attestation": {
    "source_id": "nasdaq-dividend-history-2026-08-21",
    "sampled_action_count": 12,
    "symbols": ["SPY", "QQQ"],
    "note": "owner reconciled 12 actions against a second source"
  },
  "windows": [{"symbol": "SPY", "start": "2000-01-03", "end": "2026-08-21"}],
  "holdout_map": [
    {"symbol": "SPY", "name": "train", "start": "2000-01-03", "end": "2021-12-31",
     "status": "seen", "reason": "ordinary research window"},
    {"symbol": "SPY", "name": "final-test", "start": "2022-01-03", "end": "2026-08-21",
     "status": "clean", "reason": "untouched final test"}
  ],
  "classified_moves": []
}
```

Rules the freeze enforces rather than trusting:

- The map must **tile** each symbol's range exactly once. A gap means undeclared dates,
  and undeclared is how a holdout gets read by accident; an overlap means a date with
  two classifications. Both refuse.
- `clean` becomes catalog `holdout` and is unaddressable by ordinary research. `seen`
  and `burned` become `ordinary` — both have already been exposed and neither can serve
  as an untouched final test again.
- A `burned` span must record **why** it was consumed. That sentence is what stops it
  being re-proposed as a holdout later. QQQ 2022-01 → 2024-01 is the known example
  (`docs/limitations.md`).

## Step 4 — certify

```bash
python scripts/certify_dataset.py certify --declaration research/data/certify.json
```

The declaration's `"interval"` field (`"1d"` default, `"1h"` for the hourly lane)
selects the store lane and the gate's granularity. Hourly certification counts BARS
against the calendar's expected close slots — a session holding one of its seven bars
is six named `MISSING_BAR` findings, and the 99.5% floor binds the bar ratio (D-32).
An hourly release is a separate dataset (`chronos-etf-hourly-v1`, its own catalog and
digest), never rows appended into a daily one.

Exits non-zero when the export does not certify, so it is usable as a gate. Read the
findings; do not tune around them. A genuine market event that no corporate action
explains is classified explicitly, by symbol and date, with a reason that ends up in the
evidence:

```json
{"symbol": "SPY", "session_date": "2020-03-16", "reason": "COVID-19 circuit-breaker session"}
```

## Step 5 — freeze the release

```bash
python scripts/certify_dataset.py freeze \
    --declaration research/data/certify.json \
    --output research/data/releases/etf-daily-001
```

Refuses outright over a failed verdict — a release digest is evidence, not a label. On
success it writes one partition file per (symbol, span), `catalog.json`, and
`release.json`, and prints two digests:

- **catalog sha256** — the out-of-band trusted digest a reader must be handed.
  `CertifiedDatasetCatalog.from_manifest` authenticates the manifest bytes against it,
  so a tampered catalog fails to open rather than resolving.
- **release digest** — one digest over the certification, the catalog, and the map. This
  is the D2 artifact the preregistration revision (H-DT-001/H-DT-002) binds to.

`data_version` equals the partition's content SHA-256 throughout — D-27's
bytes-are-the-label applied to data, so re-freezing identical bytes reproduces an
identical manifest and digest.

## Step 6 — hand the digest to the research plane

The release digest is what unblocks the next artifact in D-29's ordering: the
preregistration revision, then `research/deep/`. Nothing about the release makes a
strategy valid — it makes the *data* admissible. That is the only claim it carries.

## What this does not do

- It does not sample a second source for you (step 2 is irreducibly owner work).
- It does not make transcribed or rounded data trustworthy. The in-repo GLD/IWM/TLT
  files are markdown-transcribed to 2 decimals and remain unfit regardless of verdict.
- It does not count a trial or authorize a campaign. The brokered reader and blocked
  campaign posture are untouched.
