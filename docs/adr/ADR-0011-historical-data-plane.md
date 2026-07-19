# ADR-0011: The two-process historical-data plane (Milestone C1)

Status: accepted (two-reviewer design panel — isolation + quantitative — completed and
remediated; §11 records the findings)
Date: 2026-07-19

## Context

AI Quant plan C1 (`docs/AI_QUANT_GAME_PLAN.md`): stand up an IBKR historical-bar
pipeline in a **separate read-only data process** with its own gateway client id.
It never holds the writer lease and never imports `chronos.orders` (enforced by the
same AST + subprocess import-isolation tests that guard the UI,
`tests/unit/test_ui_no_broker_imports.py`). Pacing-compliant backfill; the store
keeps **unadjusted bars plus a corporate-action/dividend event stream**, deriving
adjusted / total-return views **at read time** — never incrementally appending to an
adjusted series (retroactive re-adjustment would silently break hash-pinned
provenance). Data-quality gate reuse. **Fresh holdout windows declared and embargoed
in tooling before any strategy sees the data.**

This ADR succeeds ADR-0006 (research data from public mirrors), whose stated
production source is "IBKR historical data once the owner supplies credentials," and
retires its A-32 approximation ("follow the source's adjusted series, raw OHLC
retained") in favor of the explicit event-stream + read-time-adjustment model below.

**Binding constraints from the invariants (§4 of the plan):**
- Invariant 1 — no order placed by any test/CI/dev workflow.
- Invariant 8 — no unofficial `ibapi` in requirements; the owner installs the
  official TWS API. So the **real `reqHistoricalData` path is owner-gated and
  unexercisable in this environment**; C1 is built and verified against a fake
  historical-data client, with the official client lazily importing `ibapi` exactly
  as `broker/official_ibkr.py` does today (never a pip dependency).
- Invariant 7 — localhost-only; never log credentials or account identifiers.

Historical bars are **entirely absent** from the code today (no `reqHistoricalData`
anywhere): this is greenfield, not a refactor.

## Decision

### 1. Two-process topology and the isolation boundary — file-based, lease-free

The data plane lives in a new package **`src/chronos/histdata/`** and runs as a
standalone process (`python -m chronos.histdata`). Its isolation from the trading
plane is made *structural*, not merely conventional:

- **File-based store, no trading DB — made structural (§11.7, gap 3).** The data
  process reads and writes only flat files under a dedicated tree (`§3`); it **never
  opens the trading SQLite database** and therefore cannot mutate the `writer_lease`
  row or construct a `WriterLease` (`utils/locking.py`). Because `chronos.config` is
  allowed and exposes `database_url` (`sqlite:///data/chronos.db`), "never opens the DB"
  is enforced not by hoping but by **forbidding `sqlalchemy` and `sqlite3` outright** —
  histdata is CSV/JSON only and has no legitimate use for either, so with no DB driver
  importable there is no path to the lease row even though its path is readable. Tests
  assert the package imports no `chronos.persistence` / `chronos.utils.locking` **and**
  no `sqlalchemy` / `sqlite3`, and constructs no `WriterLease`.
- **No order/broker-write reach — one pinned forbidden set (§11.6, gaps 1/2/4).**
  A single `FORBIDDEN` constant is the source of truth; both isolation layers derive
  from it. **AST-forbidden** (walked over every module in the package):
  `chronos.orders`, `chronos.api`, `chronos.services`, `chronos.service`,
  `chronos.execution`, `chronos.risk`, `chronos.control`, `chronos.persistence`,
  `chronos.utils.locking`, `chronos.broker`, `chronos.runtime`, `chronos.ui`,
  `sqlalchemy`, `sqlite3`. `chronos.broker`/`risk`/`control` are named explicitly
  (not left to incidental coupling — the whole broker plane imports clean, so it would
  otherwise be reachable undetected). **The `ibapi` / `ib_async` prefixes are
  deliberately excluded from the AST set** (`ast.walk` descends into function bodies
  and would false-fail on the official client's legitimate *lazy* `import ibapi`), and
  are enforced in the probe layer instead. The package may import only
  `chronos.marketdata`, `chronos.config`, and `chronos.utils.{time,logging}`.
- **Subprocess probe (§11.6).** A probe imports the package, **the official-client
  module, and the process entrypoint `chronos.histdata.__main__` directly** (not just
  the package `__init__` — mirroring how `test_ui_no_broker_imports.py` probes the leaf
  `api_client`, and so a top-level forbidden import living only in `__main__` or the
  client is caught), then asserts `sys.modules` leaked none of the `FORBIDDEN` set
  **plus `ibapi` and `ib_async`**. A **positive** assertion that importing the official
  client leaves `ibapi`/`ib_async` absent from `sys.modules` proves its ibapi import
  stayed lazy — the exact regression the probe targets. Stated limit: neither layer can
  catch a forbidden import that fires only at process **runtime** against a live gateway
  (owner-gated, never in CI); the AST walk is the sole backstop there, which is why it
  scans every module.
- **Separate gateway client id (§11.8).** New setting `ib_data_client_id`
  (`Field(ge=1)`, default `18`) with a cross-field validator that it **differ from
  `ib_client_id`** (TWS/Gateway rejects two live connections sharing a client id).
  `ge=1` — not `ge=0` — because client id 0 is TWS/Gateway's special *master* id
  (binds manual orders / receives all open orders), inappropriate for a read-only data
  plane. The real client connects `readonly=True` (as `broker/ibkr.py` already does).
  A collision is a connection-liveness failure, never an order-safety breach.

The data process is "read-only" with respect to the **trading/broker plane** (a
read-only gateway connection, no order writes, no writer lease); it does write its
own data files, which is its purpose.

**The allow-list was verified sound (§11.1):** the full transitive closure of the
permitted imports reaches no forbidden module — `config` → `config.limits` /
`domain.{accounts,enums,models}` → `utils.identifiers` (all leaves, no DB/broker);
`marketdata` → `bars` / `quality` / `csv_provider` (leaves); `utils.{time,logging}`
leaves; and `config` performs no module-level DB/broker construction. So importing
`chronos.histdata` cannot leak `persistence` / `locking` / `orders` / broker into
`sys.modules`, and the subprocess probe is meaningful rather than vacuous.

### 2. The historical-data port, the fake, and the owner-gated official client

- **Port:** `HistoricalDataClient` Protocol with one method,
  `fetch_daily_bars(symbol, end_date, duration_days) -> RawDailyBars` (unadjusted
  OHLCV + the source's own metadata), plus `connect()`/`disconnect()`. No adjusted
  data crosses this boundary — adjustment is a read-time concern (`§4`).
- **Fake:** `FakeHistoricalDataClient` (in `tests/support/`) records call counts and
  returns canned deterministic bars, following the `FakeMarketDataBroker`
  (`tests/unit/test_market_data.py`) / `FakeBroker` pattern. Every CI/dev path uses
  the fake; no real gateway is contacted.
- **Official:** `OfficialIBKRHistoricalClient` lazily imports `ibapi`
  (`_load_ibapi()` pattern) and issues `reqHistoricalData` with
  `whatToShow="TRADES"`, `useRTH=1`, `formatDate=1`, a `keepUpToDate=False`
  historical request. It is **structurally present but unexercised in CI** (invariant
  8); the owner runs it against a real gateway. The connection is `readonly=True`.

### 3. Store shape — unadjusted bars + a corporate-action event stream, provenance-stamped

Under a new tree `research/data/history/`:

```
research/data/history/
  bars/<SYMBOL>.csv                 # UNADJUSTED as-traded OHLCV (never re-adjusted)
  corporate_actions/<SYMBOL>.json   # the split + cash-dividend event stream
  MANIFEST.json                     # per-file provenance (source, sha256, capture, range)
```

- **Bars** reuse the existing daily schema (`date,open,high,low,close,volume`) and
  the `marketdata` `Bar`/`BarSeries` vocabulary; they are **as-traded, never
  adjusted**. Ingestion is **append-only forward in time and idempotent**: a re-fetch
  of an overlapping window must reproduce identical rows for already-stored dates
  (compared as **parsed numeric values with a canonical fixed formatting**, so a
  float-spelling change across API versions does not false-fail, and no tolerance masks
  a real one-tick drift — §11.13, D5) or the ingest **fails closed** — the store never
  silently rewrites history. A *genuine* vendor correction (IBKR revises a bad print) is
  therefore neither swallowed silently nor permanently un-fixable: it takes an
  **explicit, provenance-recorded owner re-baseline** (`--allow-correction`, which logs
  the old→new row and reason into the manifest as a supersede), never the automated
  append path. Fail-closed is the default; a correction is a deliberate, logged act
  (§11.4). Bars flow through `marketdata.quality.validate_series` before write; a
  blocking issue aborts the write (`§6`).
- **Corporate actions** are a typed JSON list per symbol (`§4`), each carrying its own
  provenance (source + capture note). Because **every derived view depends on them**,
  the actions stream gets the **same discipline as bars** (§11.14, D4): a SHA-256 of
  each actions file is recorded in `MANIFEST.json`; edits are **append-only with a
  logged supersede** (an in-place mutation that silently re-derives all
  `TOTAL_RETURN`/`SPLIT_ADJUSTED` history is exactly the provenance lie CLAUDE.md
  forbids); and **research-result provenance must pin both the bars hash and the actions
  hash** (extending ADR-0006's single data-hash stamp), so a result names *all* inputs
  that produced its numbers. **The store ships with empty action files** — C1 delivers
  the *mechanism*, not fabricated split/dividend history; real actions are
  captured/entered by the owner or a future capture task, never invented (honesty
  invariant). The legacy heterogeneous `research/data/raw/` corpus is **not** migrated
  or reprocessed by C1 (its mixed adjusted/unadjusted status per `DATA_SOURCES.md`
  would need real corporate-action data to reconcile) — the new store is the
  go-forward IBKR-sourced plane.
- **Provenance** follows ADR-0006: `MANIFEST.json` records per file the source,
  retrieval, SHA-256, row count, date range, and adjusted=false; the runtime loader
  recomputes the SHA-256 (as `csv_provider` does), so provenance is verifiable at read.

### 4. Corporate-action model and read-time adjustment — one cumulative-factor pass

`CorporateAction` (frozen): `kind` ∈ {`SPLIT`, `CASH_DIVIDEND`}, `ex_date`,
`value` (split **ratio** r for SPLIT — 4-for-1 ⇒ r=4, and `r > 0` is enforced so a
reverse split 1-for-10 ⇒ r=0.1 is valid but r ≤ 0 is rejected; **cash amount per
share** d for CASH_DIVIDEND, in **native as-of-ex-date basis** — see below), `source`,
`note`. Read-time views are derived by `adjust_series(unadjusted, actions, view) ->
BarSeries`, `view` ∈ {`RAW`, `SPLIT_ADJUSTED`, `TOTAL_RETURN`}:

- Apply an action only if `ex_date ≤ most_recent_bar.session_date` (§11.9, D3). A
  **future-dated** action (announced split/dividend whose ex-date is after the last
  stored bar) is **skipped with a warning** — otherwise it would multiply *today's*
  price and break the "most recent bar = as-traded" invariant the whole scheme rests on.
- Compute **cumulative price and volume factors** (kept as *separate* accumulators —
  splits move them in opposite directions) walking newest→oldest. Both start at 1.0 for
  the most recent applicable bar; when moving to bars strictly **before** an action's
  `ex_date`, multiply by that action's multiplier:
  - **SPLIT** (SPLIT_ADJUSTED and TOTAL_RETURN): price ×`1/r`, volume ×`r`.
  - **CASH_DIVIDEND** (TOTAL_RETURN only): price ×`(1 − d / C_ref)`, volume unchanged,
    where `C_ref` is the unadjusted close on the **last trading day strictly before
    `ex_date`**. If no bar precedes the ex-date the dividend is **skipped with a
    recorded warning** (there are also zero earlier bars to adjust, so nothing is lost).
    If `d ≥ C_ref` the factor would be ≤ 0 (corrupt data, units slip, or a
    return-of-capital exceeding price); the action is **rejected with a recorded
    warning** (and a warn threshold at `d/C_ref > 0.5`), and the view **never emits a
    non-positive adjusted price** — the adjustment asserts `adjusted_close > 0` for
    every emitted bar (the write-time quality gate ran on the *unadjusted* series only,
    so derived views carry their own positivity assertion; §11.10, D1).
  - `RAW` applies neither; both factors stay 1.0 (a straight copy).
- **Dividend basis (§11.11, D2).** `(1 − d/C_ref)` is correct **only** when `d` and
  `C_ref` share a basis. `C_ref` is unadjusted; therefore `d` MUST be stored as the raw
  **as-declared amount at its own ex-date, never restated to a later split's terms**
  (many vendor feeds publish split-restated dividends — a `$0.82` pre-split dividend
  shown as `$0.205` after a 4-for-1). Storing a split-restated `d` against an
  unadjusted `C_ref` understates the dividend by the split factor. This is a documented
  ingestion contract on the actions file, and a golden pins a dividend-then-later-split
  sequence asserting no split-sized drift.
- **Composition (§11.2).** `C_ref` and `d` are read from the **unadjusted** series, so a
  dividend's `(1 − d/C_ref)` is a *scale-free ratio*: invariant to any split between the
  dividend and today; the running product of split and dividend factors composes
  commutatively and correctly in one pass. The implementation MUST take `C_ref` from the
  raw bars, never from an in-progress adjusted array. Multiple actions sharing one
  `ex_date` compose as a product (order-independent). Factors accumulate in full
  precision; only the final emitted price is rounded: `adjusted_close =
  round(unadjusted_close × price_factor, 6)`, volume `round(unadjusted_volume ×
  volume_factor)` — no intermediate rounding, so many actions do not drift; the
  positivity assertion also guards the rare deep-back-adjustment round-to-zero case.
- The derived view returns a fresh `BarSeries` whose `.close` (and O/H/L) hold the
  **adjusted** price and whose `adjusted_close` field is left `None`, so consumers read
  the view's `.close` unambiguously and never cross RAW vs derived (§11.12).
- The derivation is **pure and deterministic** — no timestamps, no I/O — so it is
  golden-testable. Adjusted views are computed on read and **never written back** to
  the bars file (the invariant that keeps hash-pinned provenance intact). A future
  action prepended to the stream changes the derived view but not one byte of the
  stored unadjusted series.

Rationale for the direction: back-adjusting *historical* prices (not forward-adjusting
recent ones) keeps the most recent bar equal to the as-traded price, matching how
strategies and the operator read current levels; it is the CRSP/vendor convention.

**Honesty on the `TOTAL_RETURN` name (§11.3).** The `(1 − d/C_ref)` back-multiplier is
the standard CRSP/vendor **adjusted-close** convention. It is *exact* for splits and a
**first-order approximation** to a dividend-reinvested total-return index for dividends
(the adjusted series' simple return across an ex-date has denominator `C_ref − d`, a
true reinvested-TR return has denominator `C_ref` — they differ by O(d/C)). The view is
kept named `TOTAL_RETURN` to match the game-plan vocabulary, but the deliverable
(docstring, README, limitations) states plainly that it is the adjusted-close
approximation, **not** an exact reinvested total-return index.

### 5. Pacing — a conservative token bucket, honestly scoped

`reqHistoricalData` is rate-limited by IBKR (documented: no more than ~60
historical requests in any rolling 10-minute window, and identical
contract+bar-size+duration requests must not repeat within ~15s). A `PacingController`
(pure, driven by an injected clock like the existing `MutableClock` tests) enforces a
**conservative** budget: ≤ 6 requests / minute and a per-key cooldown, blocking (not
dropping) until a slot frees. It is unit-tested against the clock.

**Honesty (to limitations + runbook):** true *cross-process* coordination with the
trading backend (a shared pacing budget across both client ids) is **not** wired — the
data process self-paces conservatively under its own client id. Because the real
gateway is owner-gated, pacing cannot be validated here; the controller encodes the
documented limits and is the seam for a shared budget later.

### 6. Data-quality gate reuse

Every fetched series is validated by `marketdata.quality.validate_series` before it is
written. A **blocking** report (impossible OHLC, non-positive price, duplicate,
unclosed bar, non-finite) aborts the write and is surfaced with the symbol and issue;
advisory issues (weekend, large gap, extreme return) are recorded in the manifest, not
blocking. No partially-validated series is ever persisted.

### 7. Holdout declaration + embargo tooling — declare fresh, embargo by default

C1 delivers the *embargo mechanism* that C2's experiment registry will mediate. A
`holdout` module declares named holdout windows (symbol-scoped or global date ranges)
in a committed `research/data/history/HOLDOUTS.json`, and a read gate
`embargoed_view(series, symbol, *, unlocked=False) -> BarSeries` **excludes embargoed
windows by default** — strategy/research reads see the series with holdout dates
removed unless an explicit `unlocked=True` is passed. C1 does **not** implement the
once-only, owner-typed, logged unlock (that is C2's holdout guardian); it establishes
the declaration + default-masked read so a holdout can be declared *before* any
strategy sees the data, as the DoD requires. Default-closed: an undeclared caller gets
the masked view.

### 8. Honesty bounds — what cannot be proven in this environment

Recorded in the milestone report, `docs/limitations.md`, and a data-plane runbook:

- The real `reqHistoricalData` ingestion (official client, live gateway) is
  **owner-gated and unexercised** — invariant 8. CI proves the process, store,
  adjustment, pacing, quality gate, and isolation against the fake client only.
- **No corporate-action data is fabricated.** Adjustment correctness is proven with
  synthetic split/dividend fixtures; the shipped action files are empty until the
  owner captures real actions.
- Pacing compliance is coded to documented limits, **not** measured against a gateway;
  cross-process pacing coordination is not wired (`§5`).
- The legacy `research/data/raw/` corpus is unchanged; C1 does not reconcile its mixed
  adjustment status (`§3`).
- The `TOTAL_RETURN` view is the CRSP/vendor **adjusted-close approximation**, exact
  for splits and first-order for dividends — **not** an exact reinvested total-return
  index (§4, §11.3).
- The holdout embargo is a **default-masked read gate**, not yet a structural guardian:
  a caller that reads `bars/<SYMBOL>.csv` directly bypasses it. Enforced mediation
  (the once-only owner-typed unlock and registry-brokered reads) is **C2's** job; C1
  satisfies "declared and embargoed before any strategy sees the data" by providing the
  default-masked accessor and declaring windows up front, and says so plainly (§11.5).

### 9. Mandated build order (sub-milestones, each gate-green standalone)

1. **C1-b — corporate-action model + read-time adjustment** (pure, no process): the
   highest-value, most bug-prone core, landed first behind goldens.
2. **C1-c — port + fake + pacing controller**: `HistoricalDataClient`,
   `FakeHistoricalDataClient`, `PacingController`; the owner-gated official client
   skeleton (lazy `ibapi`, unexercised).
3. **C1-d — the data process + backfill coordinator + store writes + isolation
   tests**: the runnable process, idempotent append, quality-gated write, provenance
   manifests, `ib_data_client_id`, and the AST + subprocess isolation proofs.
4. **C1-e — holdout declaration + default-masked embargo read**.
5. **C1-f — docs (README, runbook, limitations, DECISIONS/ASSUMPTIONS), gates,
   adversarial review, PR, milestone report.**

### 10. What proves it

- Adjustment: golden tests — a 4-for-1 split quarters pre-ex prices and quadruples
  pre-ex volume; a reverse split (r<1) is handled; a known cash dividend adjusts pre-ex
  prices by exactly `(1 − d/C_ref)`; combined split+dividend composes; a
  dividend-then-later-split sequence shows **no split-sized drift** (native-basis `d`,
  D2); a **future-dated** action leaves the newest bar as-traded (D3); `d ≥ C_ref` is
  rejected and no view emits a non-positive price (D1); RAW is a no-op; a dividend with
  no preceding bar is skipped-with-warning; determinism (byte-identical re-derive).
- Isolation: AST walk over every `histdata` module asserts no import from the pinned
  `FORBIDDEN` set (§1); a subprocess imports the package, the official-client module,
  and `__main__` and asserts none of `FORBIDDEN` ∪ {`ibapi`, `ib_async`} leaked into
  `sys.modules`; a positive test asserts importing the official client leaves
  `ibapi`/`ib_async` absent (lazy-import proof); and a test asserts the package imports
  no `sqlalchemy`/`sqlite3` and constructs no `WriterLease`.
- Idempotency + fail-closed: a re-fetch of an overlapping window reproduces identical
  rows; a conflicting row aborts the write; a blocking quality issue aborts the write.
- Pacing: the controller blocks past the budget and releases as the clock advances.
- Holdout: a declared window is absent from the default read and present only under
  explicit unlock.
- Separate client id: the validator rejects `ib_data_client_id == ib_client_id`.

### 11. Design-review refinements (record)

Before implementation the two load-bearing areas were verified directly against the
codebase and the math re-derived from first principles; the refinements folded in:

1. **Isolation allow-list verified sound** — the transitive closure of the permitted
   imports (`config`→`domain`→`utils.identifiers`; `marketdata`→`bars`/`quality`;
   `utils.time`/`logging`) reaches no forbidden module and `config` does no import-time
   DB/broker construction, so the subprocess probe on `chronos.histdata` is meaningful
   (§1). This was the largest design risk; it is cleared with evidence.
2. **Adjustment composition** — `C_ref`/`d` read from the *unadjusted* series makes the
   dividend factor a scale-free ratio, so split+dividend factors compose commutatively
   and split-invariantly in one pass; the implementation must not read `C_ref` from an
   in-progress adjusted array; factors accumulate in full precision, only the final
   price rounds (§4).
3. **`TOTAL_RETURN` honesty** — it is the CRSP adjusted-close convention (exact splits,
   first-order dividends), documented as an approximation, not an exact reinvested TR
   index (§4, §8).
4. **Corrections** — a real vendor revision takes an explicit, logged
   `--allow-correction` re-baseline; fail-closed stays the default so history is never
   silently rewritten, but a correction is not permanently blocked (§3).
5. **Non-positive dividend factor** (`d ≥ C_ref`) is rejected-with-warning; the view
   never emits a non-positive adjusted price (§4).
6. **Holdout scope honesty** — C1's embargo is a default-masked accessor, not a
   structural guardian; direct-file reads bypass it, and enforced mediation is C2 (§7,
   §8). Stated in the deliverable rather than over-claimed.

Two independent reviewers (isolation lens; quantitative lens) then ran to completion and
grounded findings in measured import closures and re-derived math; these were folded in:

7. **Deny-list widened + made structural (isolation HIGH/MED-HIGH).** The whole
   `chronos.broker` plane imports clean and was reachable undetected; `chronos.risk`
   was only *incidentally* caught via execution coupling, and `chronos.control` (the
   ADR-0007 autonomous lock) was reachable. All three are now named explicitly. Raw
   `sqlalchemy`/`sqlite3` are forbidden so "never opens the trading DB / lease row" is
   structural despite `config` exposing `database_url` (§1).
8. **`ibapi`/`ib_async` handled correctly (isolation HIGH).** Excluded from the AST set
   (which descends into the official client's legitimate lazy `import ibapi` and would
   false-fail), enforced in the subprocess probe instead, plus a positive lazy-import
   proof; the probe imports the official-client module and `__main__`, not just the
   package. One pinned `FORBIDDEN` constant feeds both layers (§1, §10).
9. **Client id `ge=1`** (id 0 is TWS's master client id, wrong for a read-only plane);
   collisions are liveness, not safety (§1).
10. **Adjustment inputs hardened (quant MED-HIGH/MED).** Dividend `d` must be stored in
    **native as-of-ex-date basis** (never split-restated), the matching half of the
    unadjusted-`C_ref` rule — otherwise a later split double-counts (D2). Future-dated
    actions are skipped so the newest bar stays as-traded (D3). Derived views assert
    positivity themselves because the write-time quality gate only saw the unadjusted
    series (D1). Split ratio `r > 0` (reverse splits); separate price/volume factors.
11. **Actions stream gets bar-grade provenance (quant MED, D4).** SHA-256 in the
    manifest, append-only + logged supersede, and research provenance pins **both** the
    bars hash and the actions hash — else an edited action silently re-derives history
    under an unchanged bars hash (a provenance lie). Idempotency compares canonical
    parsed numerics so float-spelling drift does not false-fail (D5).

(Process note: an earlier parallel reviewer batch had two runs return no usable
findings, one of which surfaced an injected "skip the check" instruction in its result
field — disregarded, and re-run. The completing reviewers' confirmed findings are all
folded above; the injected content changed nothing.)

## Consequences

The research plane gains a provenance-clean, owner-runnable path to IBKR history that
is structurally incapable of placing an order or holding the writer lease. Adjusted
and total-return views become derived, auditable, and reversible rather than baked in
— fixing ADR-0006's A-32 approximation. The store ships **empty of bars and actions**:
C1 is the pipeline, and the first real backfill is an owner-run step against a live
gateway. Nothing in the trading/live plane changes; crypto stays disabled; the single
`transmit=True` boundary is untouched.
