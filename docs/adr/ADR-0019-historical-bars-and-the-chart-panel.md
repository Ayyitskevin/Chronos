# ADR-0019: Historical Bars and the Chart Panel

Status: accepted (2026-07-26)
Date: 2026-07-26
Index entry: DECISIONS.md **D-19**.
Extends: ADR-0018 (the operator terminal) — this is the chart its residual 1 deferred.
Depends on: ADR-0011 (the historical-data plane and its pacing doctrine),
ADR-0009 (the live conjunction), ADR-0002 (TWS API).

## Context

ADR-0018 shipped the terminal with no chart and said why: "there is no
historical-bar route, and the `Broker` protocol has no bars method — a chart
would have nothing honest to draw." Closing that is what makes the terminal feel
like an operator's window rather than a status page, and it was the owner's
chosen next step.

The obvious implementation is the wrong one, and the reason is worth recording
because it is not visible from the outside.

### Why not the research corpus

Chronos already has a historical-data plane (`chronos.histdata`, ADR-0011) with a
bar store, a manifest, provenance tracking, and a pacing controller. Serving the
chart from it would have cost nothing and added no broker load.

It was rejected on the data. The corpus is **research material, and stale**:
SPY ends 2019-11-14, IWM covers 2019–2021 only, and RISK_REGISTER R-08 records
that the symbols are heterogeneous — some dividend-adjusted, some nominal, some
markdown-transcribed to two decimals for a validation window. That is
appropriate for backtesting and useless for supervising a live autonomous
trader, which is what this terminal is for. A chart of SPY ending seven years ago
is not a chart; labelling it honestly would only make it an honest irrelevance.

Reading it would also have dragged the holdout question into a display surface:
ADR-0013 reserves windows that research may not consume without an owner-typed
unlock, and "does looking at a chart burn a holdout" is a question worth never
having to answer.

**So bars come from the broker.** That is the decision, and everything below is a
consequence of it.

## Decision

### 1. `Broker.historical_bars` joins the protocol

A read-only, idempotent method returning **closed bars only**. A forming bar is a
number that changes while it is being read, and a chart, a moving average, and an
operator's judgement are all better served by a series that is complete as far as
it goes than by one whose last element is provisional and unlabelled.

- **`official_ibkr`** implements it: one `reqHistoricalData`, `useRTH=1`,
  `keepUpToDate=False`. A streaming subscription would hold a request slot open
  against a budget this connection shares with the order pipeline. Bars route
  through the existing `RequestRegistry` — historical responses are an
  append-only sequence terminated by `historicalDataEnd`, which is exactly the
  shape the registry already models, so no new bridge state was needed.
- **`demo`** generates a deterministic synthetic series seeded from the symbol,
  so the same symbol draws the same chart in every run and every test. It is
  stamped `source="demo"` and the panel banners it.
- **`ib_async`** refuses, pointing at the official adapter — the same pattern it
  already uses for crypto. Two bar implementations would mean two pacing
  behaviours and two parsers against a gateway neither can be verified against
  from here, and a chart that silently differed by adapter is worse than one that
  says which adapter it needs.

An unparseable bar is **dropped with a warning, never guessed at**. A chart
missing a day is visibly wrong; a chart with a fabricated day is not.

### 2. Pacing degrades — it never blocks

This is the load-bearing decision. IBKR paces historical requests far harder than
anything else it serves, and the backend holds **one** connection shared with the
order pipeline and the autonomy tick.

`chronos.api.bars.BarProvider` sits between the route and the broker:

- **Completed bars are immutable, so the cache is the normal path.** Keyed by
  `(symbol, interval)`, remembering how much history it holds, so a 30-day
  request is sliced out of a cached 180-day series rather than issuing a second
  request for a subset of what is already in memory.
- **A paced-out request serves the cache and labels it stale**, or refuses when
  there is nothing cached. It never sleeps. The histdata backfill process answers
  a pacing delay by sleeping; doing that inside a request handler on this event
  loop would convert a pacing limit into latency for order submission.
- **Budget is recorded before the call, not after a success.** The gateway sees
  the request either way, and a failure that did not consume budget would let a
  bad symbol retry on every poll with nothing throttling it. This was a real
  defect in the first implementation, caught by its own test.
- **No lock is held across a broker call.** Two concurrent requests for the same
  cold symbol both fetch, which wastes one request and is strictly better than a
  request handler holding a lock while another waits on the network.

The chart panel polls at **two minutes**, not the terminal's ordinary five
seconds. That is a pacing decision as much as a display one.

### 3. `PacingController` moved to `chronos.marketdata`

It began in `chronos.histdata`, whose package `__init__` pulls in the whole
research plane including the holdout machinery — too much to import into the
process holding the broker connection for a forty-line utility. Duplicating it
would have been worse: two implementations of a rate limit is two places for it
to be wrong.

`chronos.marketdata` is the neutral vocabulary both planes already share
(`bars.py` lives there), so neither has to import the other to pace itself.

### 4. Panels became symbol-aware

`GP` is the first command that narrows by symbol, which changed the terminal's
dedupe rule from "one panel per panel id" to **one panel per (panel id, symbol)**:
`SPY GP` and `AAPL GP` are different questions and get their own panels, while a
second `SPY GP` focuses the one already open. Saved workspaces gained a version
and read the old shape, so an upgrade does not silently empty an operator's desk.

### 5. Honesty rules, applied to the surface that most invites trust

A chart is what an operator believes on the least evidence, so ADR-0018's data
honesty applies here with the sharpest teeth:

- A **synthetic** series is banner-labelled. Demo candles drawn in the same
  register as live ones would be the most convincing false thing this page could
  show.
- **Stale** bars say so, with the time they were actually fetched.
- A **refusal** draws nothing and states the reason. An empty plot area with no
  explanation reads as "this instrument is flat".
- Prices are floats here — a deliberate exception to the terminal's `Decimal`
  rule, safe because nothing downstream of a chart is an order parameter, and
  re-quantizing would imply precision the source never had.

## Consequences

- The terminal can draw a real chart, and the backend has a bars capability the
  rest of the system can use later (per-holding context, execution-quality views).
- The backend now makes historical requests, which it never did before. Bounded
  by the cache, the pacing controller, and the two-minute panel cadence, and
  recorded as RISK_REGISTER **R-42**.
- The chart is deliberately plain: candles, a price axis, and the numbers.
  No indicators, no drawing tools, no crosshair. Those are additive and none of
  them is what makes the panel trustworthy.

## Known limitations and residuals

1. **The pacing budget is per-process and IBKR's may not be.** The histdata
   backfill process paces itself separately under a different client id. Whether
   the real limits are shared across client ids cannot be determined from here
   without a live gateway. This is ADR-0011 §5's original residual, now with a
   second self-pacing caller; `chronos.marketdata.pacing` says so at the source.
2. **The IBKR path is unverified against a real gateway.** It is wired and tested
   against fakes, exactly as the order surface was at M7, and gateway
   verification remains an owner action. The demo path is fully exercised.
3. **Only daily bars are validated.** The interval vocabulary carries hourly and
   minute sizes and the adapter maps them, but nothing has run them, and IBKR's
   duration limits differ per bar size in ways this milestone did not explore.
4. **No holdout interaction, by construction.** Broker bars are live market data,
   not the research corpus, so ADR-0013's embargo does not apply and no unlock is
   involved. If a future chart ever reads `chronos.histdata`, that question
   becomes live and must be answered before it does.
5. **A cold concurrent duplicate wastes one request.** Accepted, and explained in
   §2: the alternative holds a lock across a network call in the process that
   submits orders.
6. **The account is ~USD 110.** A chart of an instrument this account cannot
   meaningfully trade is still useful context for supervising the model, but it
   is context, not capability.
