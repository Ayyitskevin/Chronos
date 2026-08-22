# ADR-0029 — The hourly bars lane

Status: **accepted 2026-08-21** (implements the hourly half of Phase 3's certified-export
deliverable, `docs/VISION_COMPLETION_PLAN.md` §8; recorded as D-32). Builds on ADR-0011
(historical-data plane) and D-30/D-31 (session calendar, certification).

## Context

Phase 3 commits to "uniform, point-in-time daily **and hourly** data across at least 6–10
liquid instruments." The plane could not express the second half, and the daily binding
ran deeper than the visibly hardcoded `"1 day"` request: the store's CSV had no timestamp
column, the idempotent merge and `Bar.sequence_id` were keyed by session date (so an
hourly session's bars collided — with `allow_correction`, silently, keeping only the last
bar and recording the loss as "corrections"), the quality gate would brand valid hourly
data as blocking `DUPLICATE_BAR`, and certification's coverage collapsed bars into a
session-date set, in which a session holding one of seven bars counts as covered.
A-31's "no trustworthy intraday data in this environment" was a data-availability fact,
never a design decision — this ADR is the plan for retiring it honestly.

## Decision

1. **A separate lane, not an overloaded schema.** Hourly bars live in
   `bars_1h/<SYMBOL>.csv` with schema `timestamp_utc,session_date,open,high,low,close,volume`,
   their own manifest entry (`bars_1h`, interval-stamped), and their own read/write pair.
   The daily lane is byte-identical to before — including `Bar.sequence_id`, which keeps
   its historical date-keyed form for DAY_1 **because it participates in execution intent
   identity**; intraday intervals append the close time.
2. **`session_date` is stored, never derived.** The exchange trading date is written at
   ingestion (US/Eastern date of the bar start); deriving it from the UTC timestamp would
   misdate every bar after 20:00 New York standard time. Holdout embargo and release
   partitioning key on `session_date`, so a masked or classified session always takes all
   of its bars with it.
3. **The IBKR request pins its ambiguities instead of inheriting them:** `barSizeSetting`
   `"1 hour"`, `formatDate=2` (epoch seconds — unambiguous UTC; the in-repo claim that
   `formatDate=1` intraday rows arrive as epoch has never met a gateway and is probably
   wrong), `useRTH=1` (the certification expectation is the regular session — extended
   hours would be a different dataset and a different decision). IBKR stamps bar starts;
   Chronos stamps closes, capped at the session's official close from the research
   session calendar, so the final partial bar reads 16:00 (13:00 on a half-day) rather
   than a fabricated 16:30.
4. **Chunking is the coordinator's job.** One client call is one gateway request; the
   backfill coordinator chunks (default 30 days per request — conservative under every
   published reading of IBKR's per-bar-size duration caps, which this repo has never
   gateway-verified), paces every chunk with distinct keys, runs oldest-first, records
   each empty chunk's end-date in `WriteResult.empty_chunks` and surfaces it in the CLI
   JSON (IBKR's intraday depth horizon is undocumented here, and without the record an
   operator cannot tell a gateway horizon from silent vendor loss), and the CLI's hourly
   path takes a stricter 4/min pacing window so a long backfill does not sustain IBKR's
   ~60/10-minute ceiling. **Bars that have not closed are dropped before the store**:
   a historical request reaching today returns the forming bar, and the close cap would
   stamp it with the session's official close — landing a partial print on exactly the
   timestamp certification expects for the delivered closing bar.
5. **Certification judges bars, not sessions.** `certify_export(interval=HOUR_1)` compares
   delivered close timestamps against `expected_close_timestamps_utc` per session; a
   missing bar and an off-slot bar are named findings with timestamps, and the frozen
   99.5% floor binds the bar ratio. Corporate-action reconciliation always runs in the
   daily close frame (per-session closes are derived first) — a split ratio implies a
   daily close-to-close return and nothing else. Minute intervals refuse as vocabulary.
6. **Hourly adjusted views refuse.** The dividend factor's reference price is the official
   daily closing print; the last hourly trade is not it. Adjusted hourly is deferred until
   it can anchor C_ref to the daily series — a later, explicit change.
7. **Interval guards are explicit at every write and judgement seam.** `write_bars`
   refuses non-DAY_1 and `write_hourly_bars` non-HOUR_1; `certify_export` refuses a
   series whose interval is not the one it was told to judge; `_render_partition`
   refuses an interval it has no faithful schema for; and the hourly store refuses a
   bar whose `session_date` disagrees with its timestamp's Eastern date. These are
   stated rather than emergent — the pre-existing refusal of hourly series by the daily
   store was an *accident* of the date-keyed `sequence_id`, and making that identifier
   interval-aware silently removed it. An adversarial review of this ADR's own
   implementation caught that regression; the guards and their regression tests
   (`tests/unit/test_hourly_review_fixes.py`) are its result.
8. **Evidence schemas bumped to v2** (`chronos-dataset-certification-v2`,
   `chronos-dataset-release-v2`) while zero production digests existed, so no recorded
   evidence changed identity. Interval is part of the release document and of dataset
   identity by naming convention (`chronos-etf-hourly-v1`); the certified-data catalog
   schema is untouched — its byte plane is deliberately interval-agnostic.

## What this does not change

- **Executable trading scope.** D-12/ADR-0008 limits executable candidates to daily-bar
  strategies on account-economics grounds (PDT rules, capital) that hourly *data* does not
  dissolve. Hourly bars feed the research plane; nothing here opens intraday trading.
- **R-26.** Market-open evidence remains the venue's own `CLOSED` token. The session
  calendar reached one new consumer (`histdata`'s parser, for the close-cap) — a read-only
  data-plane module the isolation guard continues to exclude from the authority list.
- **The holdout posture.** The hourly lane inherits the default-masked embargo unchanged.

## Owner-verify items (first real hourly backfill)

The gateway truths this repo cannot pin from here, to be confirmed and recorded on first
run: actual per-bar-size duration caps (then raise `--chunk-days` deliberately), the
actual intraday depth horizon per symbol, delayed-tier eligibility for hourly historical
requests, that RTH hourly bars arrive start-stamped at 09:30…15:30 with the final bar
spanning to the close, and volume units at intraday resolution.
