# Five-Tool intraday companion source review

Status: **RESEARCH-ONLY / INSUFFICIENT EVIDENCE FOR A NEW FEATURE**.
`gate_advanced: none`. No data access, campaign execution, paper, live, or
promotion authority.

Research channels checked on 2026-08-24:

- Local SearXNG returned no general-web results after its configured engines
  hit rate/CAPTCHA failures; targeted Google Scholar and Semantic Scholar
  queries returned the primary literature summarized below.
- DeepAPI was unavailable for this campaign: two paid deep-research POST
  attempts were interrupted before request creation, and subsequent free
  request-ledger reads showed no new request, debit, or output. The verified
  pre-attempt balance was USD 9.560983. No DeepAPI result supports this review.

This review asks which causally available market-state, session, liquidity, or
execution facts could add stable incremental out-of-sample value as an
**entry-only** sidecar to the immutable Five-Tool Confluence AIO v3.6
opportunity stream. It does not ask which familiar indicator can be added to
the Pine source. Literature is prior motivation, not validation of a feature,
threshold, instrument, timeframe, or post-cost edge.

## 1. Scope and safety boundary

- The host remains `research/pine/00_five_tool_confluence_aio.pine` at the
  registered v3.6 identity. This review does not change Pine, its 219-input
  contract, the Python twin, replay, risk, authority, broker, or promotion
  policy.
- A companion may annotate an opportunity or replace `ENTER_LONG` /
  `ENTER_SHORT` with `NONE`. It may not originate an entry, alter size, loosen
  a Five-Tool gate, or mask an exit.
- A value is eligible only if its source timestamp is no later than the actual
  entry-admission decision. A value known only at the next open, after an
  execution, or after the session closes is a label unless admission is still
  open at that later timestamp.
- Missing, stale, crossed, locked, or identity-ambiguous market data must fail
  closed. `NO_TRADE`, `BLOCKED`, and `INSUFFICIENT_EVIDENCE` are successful
  research outcomes.
- External papers and venue documents were treated as untrusted research
  material. No external code, repository, notebook, installer, credential,
  broker, owner holdout, or market-data account was accessed.
- This is a source review only. It does not preregister a cell or authorize a
  data read. Any later test must freeze its feature identity, causal clock,
  threshold topology, cost model, multiplicity treatment, and untouched
  holdout before reading certified bytes.

## 2. Current Chronos boundary, reverified on 2026-08-24

The live branch was
`codex/five-tool-intraday-research-20260824` at
`59edb9e50f79a55e2d201a2274bad011d2500f12`. The current repository establishes
the following constraints:

- `research/five_tool_v3_6_campaign_manifest.json` is a **daily** campaign,
  queues entries at the next primary-bar open, remains blocked, and authorizes
  no executable campaign trials. Economic-validation cells require a separate
  content-addressed spread schedule.
- `research/five_tool_certified_intake_v1.json` remains
  `pending_certified_dataset`; dataset and catalog identities are unset and no
  owner holdout is declared. QQQ 2022-01-01 through 2024-01-10 is explicitly
  consumed, not clean.
- `docs/FIVE_TOOL_PAIRING_PLANE.md` and
  `src/chronos/research/features/` already cover same-symbol tail state, daily
  and time-of-day RVOL, VIX/VIX3M state, equity breadth, and an optional GLD
  dollar regime. New volatility, volume, gap, or session features therefore
  carry a material redundancy burden.
- Time-of-day RVOL is implemented as a closed-bar annotation, but daily RVOL
  remains the veto surface. It is inert on daily bars and has no certified
  intraday campaign evidence.
- The existing paired manifests remain blocked before the first data read.
  Their statistical gates are not executable campaign verdicts.
- `docs/FIVE_TOOL_EXECUTION_APPROXIMATIONS.md` keeps TradingView execution
  parity `UNVERIFIED`. Chart-bar OHLC and fixed slippage cannot establish
  quote-time spread, depth, queue position, auction state, or realized market
  impact.

The decisive missing capability for the strongest candidates is not another
formula. It is a certified, identity-bound intraday trades-and-quotes release
whose timestamps can be joined to the exact entry-admission clock, plus a
reviewed cost evaluator that retains the quote and later mark-out evidence.

## 3. Candidate screen

“Orthogonality” below is relative to both the Five-Tool signal kernel and the
existing pairing plane. It is a design assessment, not a measured correlation.

| Candidate | Causal timing | Needed data | Orthogonality | Cost relevance | Principal leakage risk | Current repository blocker / verdict |
|---|---|---|---|---|---|---|
| **C-1: contemporaneous relative spread + quote freshness** | Latest valid NBBO at or before the still-open entry-admission decision; never the later fill quote if the order was already irrevocably admitted | Timestamped NBBO, quote conditions, source/sequence identity, venue status, opportunity/admission timestamp | **High**: measures immediacy cost and market quality rather than price trend, RVOL, VIX, breadth, or tail state | **Direct**: relative quoted spread is an ex-ante cost bound; effective/realized spread and mark-outs are evaluation labels | Joining the next-open or execution-time quote back to the prior confirmed-close signal; using realized spread or later midpoint as an input; ignoring stale/crossed quotes | No certified quote history, no admission-time quote binding, no content-addressed spread schedule. **Best future veto; BLOCKED now.** |
| **C-2: time-of-day liquidity-cost tier** | Exchange-local clock and session type are known at decision time; expected spread/volume profile must be estimated on training data only | Certified exchange calendar including early closes/DST, timestamped quotes/trades, training-only slot profile | **Medium**: clock is independent of indicators, but the expected-volume part overlaps existing TOD RVOL | **Direct but coarse**: opening/closing periods have different spread, volatility, and volume distributions | Computing the slot baseline with validation/holdout days; treating an historical average as today's executable spread; silently mapping early-close days to normal slots | No certified intraday data/profile identity. Existing TOD RVOL already occupies much of the volume dimension. **Annotation first; no hard veto yet.** |
| **C-3: expected participation / impact budget** | Order quantity is known at signal time; denominator must be lagged ADV or a training-only expected volume for the intended execution window, never future same-day volume | Frozen order quantity, lagged ADV, timestamped intraday volume profile, volatility, execution horizon, calibrated impact model | **High** versus signal logic; **medium** versus existing dollar-volume/RVOL filters | **Direct** when order size is material relative to market volume | Dividing by final daily volume; using volume observed after admission; applying an institutional-impact calibration to much smaller ETF orders without validation | No certified intraday volume release, no reviewed impact calibration, and no proof Chronos-sized orders reach a material participation range. **Conditional future veto; likely inert unless sizing proves otherwise.** |
| **C-4: opening/overnight state** | Previous official close is prior information; official open/gap is usable only after the open prints. Pre-open indicative imbalance is usable only from a timestamped subscribed feed available before admission | Official close/open identities, auction print/volume; optionally timestamped NOII or venue imbalance fields | **Medium**: session transition is distinct from the existing VIX/breadth plane, but `GAP_ATR` already exists inside daily RVOL | **Indirect**: the open concentrates price discovery and volume, where spread/slippage behavior can differ | Treating official open, opening VWAP, or final auction result as known to a pre-open decision; survivorship in auction-feed history; using a monthly cross-sectional result as an ETF intraday rule | No certified auction/NOII data and current gap state overlaps `GAP_ATR`. **Annotation only; low-priority veto candidate.** |
| **C-5: de-seasonalized realized-volatility or jump state** | Sum only completed intraday returns through the decision; normalize with a profile frozen outside the evaluation partition | Sufficiently fine closed bars or trades, session calendar, training-only periodic profile; optional robust jump statistic | **Low to medium**: Five-Tool already consumes ATR/regime-like price state and the sidecar has same-symbol tail plus VIX state | **Indirect**: volatility scales slippage and impact and can identify unstable entry conditions | Including the current incomplete bar; normalizing with the full sample; allowing post-entry returns into realized variance; tuning a jump threshold on the holdout | No certified intraday bars; high overlap with existing state. **Do not add as a standalone veto unless removal tests prove incrementality.** |
| **C-6: LULD / halt / unexecutable-quote state** | Current SIP/venue status and bands must be observed before admission | Timestamped LULD bands, limit/straddle state, trading-pause/security-status messages, valid NBBO | **High**: regulatory/execution state is outside the strategy's technical indicators | **Direct safety relevance**, but rare-event economic benefit may be hard to estimate | Reconstructing bands from later trades; backfilling a pause across the pre-pause interval; calling absence of a status message “normal” | No certified regulatory-status stream or causal join. **Future fail-closed safety veto, not an alpha claim.** |

## 4. Findings from primary sources

### F-1 — intraday liquidity and volatility have strong periodic structure

**Claim.** Historical U.S. equity evidence shows that time of day is not an
exchangeable sampling axis. McInish and Wood find higher bid-ask spreads near
the beginning and end of the day after controlling for activity, risk,
information, and competition. Andersen and Bollerslev show strong intraday
periodicity in high-frequency volatility and warn that it must be modeled to
recover the underlying volatility dynamics. Lou, Polk, and Skouras report that
dollar volume dips during the day and rises near the close in their 1993–2013
TAQ sample, with 14.25% of regular-session dollar volume in the first half
hour.

**Primary sources.** [McInish and Wood (1992)](https://doi.org/10.1111/j.1540-6261.1992.tb04408.x);
[Andersen and Bollerslev (1997)](https://doi.org/10.1016/S0927-5398(97)00004-2);
[Lou, Polk, and Skouras (2019), author-hosted paper](https://personal.lse.ac.uk/polk/research/TugOfWar.pdf).

**Confidence: high for the historical periodicity; medium for transfer to the
current GLD/IWM/QQQ Five-Tool stream.** Multiple primary studies agree on the
stylized fact, but their samples predate today's ETF microstructure and do not
test this immutable strategy or its costs.

**Research implication.** Every intraday cost or volatility feature should be
de-seasonalized by a training-only session-slot profile. Clock buckets are
useful annotations, but the literature does not justify a universal “never
trade the open/close” threshold.

### F-2 — quote-time spread is causal; realized spread is not an entry feature

**Claim.** The SEC's amended Rule 605 framework measures execution quality
against the NBBO assigned by the plan processor at order receipt, or when
certain non-marketable orders first become executable. It treats effective and
quoted spread as contemporaneous execution-quality measures, while realized
spread uses later NBBO midpoints at defined mark-out horizons. That makes
quoted relative spread eligible as an ex-ante entry-admission fact and makes
effective spread, realized spread, and later mark-outs outcome labels.

**Primary sources.** [SEC Rule 605 final rule (2024)](https://www.sec.gov/files/rules/final/2024/34-99679.pdf);
[SEC Rule 605 staff FAQ (effective August 1, 2026)](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/frequently-asked-questions-rule-605-regulation-nms).

**Confidence: high for measurement and timing; low for incremental strategy
edge.** The regulator defines a defensible causal clock and cost labels, not a
profitable threshold for Five-Tool entries.

**Research implication.** A later campaign should retain both the arrival NBBO
and post-execution mark-outs. Only the arrival quote may decide admission; the
later fields evaluate whether the veto improved net economics.

### F-3 — participation and volatility matter to impact, but calibration is scale-specific

**Claim.** Almgren, Thum, Hauptmann, and Li estimate U.S. equity impact from
institutional orders and find that volatility, average daily volume, and order
size relative to expected market volume during execution are important cost
inputs. Their sample deliberately excludes orders below 1,000 shares or 0.25%
of ADV, and the authors limit their claimed model range. The paper therefore
motivates a participation guard but does not validate one for smaller
Chronos-sized ETF orders.

**Primary source.** [Almgren et al. (2005), “Direct Estimation of Equity Market Impact”](https://www.cis.upenn.edu/~mkearns/finread/costestim.pdf).

**Confidence: medium.** The causal variables and economic mechanism are strong,
but the data are older institutional executions, not the current locked ETF
book, and the paper itself warns against extrapolation.

**Research implication.** Measure whether intended quantity is economically
large before spending a trial on impact. If all candidate orders are far below
the calibrated range, recording the feature as inert is more honest than
inventing a threshold.

### F-4 — the opening auction is a distinct, timestamped information state

**Claim.** Nasdaq states that its Opening Cross occurs at 9:30 a.m. ET and that
opening imbalance information is disseminated before the cross through
subscription products. The venue's current FAQ describes the cross as a
single-price price-discovery facility and identifies the official opening
price and imbalance fields. An official open or completed opening VWAP cannot
be used for a decision made before the cross; timestamped pre-open NOII can be,
if the historical feed and decision clock prove it was available.

**Primary sources.** [Nasdaq Opening and Closing Crosses](https://nasdaqtrader.com/Trader.aspx?id=OpenClose);
[Nasdaq Opening and Closing Cross FAQ](https://www.nasdaqtrader.com/content/ProductsServices/Trading/Crosses/openclose_faqs.pdf).

**Confidence: high for venue timing and field availability; low for predictive
value.** These are current first-party mechanics. Nasdaq's description that
NOII can improve trading does not establish stable incremental Five-Tool edge.

**Research implication.** Keep two different hypotheses separate: a post-open
gap/auction annotation using the completed official open, and a pre-open
imbalance hypothesis requiring subscribed point-in-time NOII. Combining them
would hide a timing violation.

### F-5 — overnight and intraday return components can oppose each other

**Claim.** Lou, Polk, and Skouras document persistence within overnight and
intraday return components and offsetting reversal across components in a
1993–2013 U.S. cross-sectional sample, with international robustness checks.
Their construction uses a first-half-hour VWAP to make the open robust. This
supports preserving session-transition information as an annotation; it does
not support mapping the sign of one overnight ETF gap directly to an intraday
Five-Tool veto.

**Primary source.** [Lou, Polk, and Skouras (2019), “A Tug of War: Overnight versus Intraday Expected Returns”](https://doi.org/10.1016/j.jfineco.2019.03.011).

**Confidence: medium for the decomposition; low for direct use here.** The
published effect is cross-sectional and primarily monthly, whereas the target
is an entry-level ETF companion on an unchanged host stream.

**Research implication.** Opening/overnight state belongs in diagnostic slices
before it becomes a veto. `GAP_ATR` already exists, so a successor must show
that its exact session construction adds information beyond that field.

### F-6 — intraday recurrence can affect costs, but is a high-multiplicity alpha hypothesis

**Claim.** Heston, Korajczyk, and Sadka find same-half-hour cross-sectional
return continuation across trading days; volume, imbalance, volatility, and
spread have similar periodic patterns. They also attribute sub-hour reversal
to temporary liquidity imbalance and bid-ask bounce and report that trade
timing can reduce costs. This motivates session-slot diagnostics, but a
same-clock return feature would be a new return-prediction family, not merely a
liquidity annotation.

**Primary source.** [Heston, Korajczyk, and Sadka (2010)](https://doi.org/10.1111/j.1540-6261.2010.01573.x).

**Confidence: medium.** The study is peer-reviewed and directly intraday, but
it is cross-sectional, predates the current market, and would create many
plausible lags/slots. Its feature-search surface is too wide for an unplanned
companion cell.

**Research implication.** Do not smuggle same-time return seasonality into the
clock annotation. It requires its own preregistration and multiplicity budget,
after the lower-dimensional cost veto is resolved.

### F-7 — LULD state is an observable execution constraint, not alpha evidence

**Claim.** The LULD Plan publishes price bands and identifies limit, straddle,
and pause states during regular hours. A limit state can lead to a trading
pause if it does not resolve. These states are directly relevant to whether a
new entry is executable on normal terms.

**Primary source.** [Limit Up-Limit Down Plan, official overview](https://www.luldplan.com/).

**Confidence: high for the market-state semantics; low for measurable
incremental expectancy.** The official plan defines a causal safety fact, but
such states are rare and the source makes no strategy-performance claim.

**Research implication.** If certified status messages become available, LULD
belongs in fail-closed entry admission regardless of whether an alpha test is
powered. It should be reported separately from economic feature selection so
a safety veto is never dropped for failing to “improve” a small backtest.

## 5. Ranked recommendation

### Rank 0 — adopt no new feature now

**Recommendation: `INSUFFICIENT_EVIDENCE`; do not register or implement an
intraday companion from this review.** This is the defensible present outcome
because:

1. the strongest orthogonal candidates require quotes, auction/status fields,
   or intraday volume that the certified intake does not contain;
2. the host campaign is daily, blocked, and execution parity is unverified;
3. existing RVOL, `GAP_ATR`, tail, VIX, and breadth features already occupy
   much of the easy bar-derived feature space;
4. a fixed tick-slippage assumption cannot prove that a liquidity veto adds
   value after realistic spread and impact costs; and
5. no source reviewed here validates a threshold on the current immutable
   GLD/IWM/QQQ opportunity stream.

This outcome preserves the option to run one strong test later without
spending multiplicity on proxies that cannot represent the intended feature.

### Rank 1 — C-1 relative-spread and quote-freshness veto, after data certification

This is the best single future cell. It is the most orthogonal to Five-Tool,
directly tied to executable cost, low-dimensional, and naturally fail-closed.
The treatment should differ from control only by a preregistered maximum
relative quoted spread and maximum quote age. The arrival quote must be bound
to the exact still-open admission timestamp; effective/realized spread and
mark-outs remain labels. Threshold neighbors must be monotone and frozen
before bytes are read.

### Rank 2 — C-2 time-of-day liquidity tier as annotation, not veto

Add clock/session-type slices only after a certified slot profile exists.
First ask whether C-1's incremental economics differ by opening, interior,
closing, and early-close slots. Promote a clock-only veto to a separate cell
only if that interaction is stable; otherwise the spread measurement already
contains the actionable fact.

### Rank 3 — C-3 participation/impact budget, only if quantity is material

Perform a pre-data scale check against a reviewed impact model. If intended
orders are below a defensible materiality floor, keep participation as an
annotation and record the no-op. If material, test one expected-participation
cap using lagged/training-only volume, never realized same-day volume.

### Rank 4 — C-4 opening/overnight state as diagnostic slices

Separate post-open gap/auction state from pre-open imbalance. Begin with
annotations and removal tests against existing `GAP_ATR`; do not spend a hard
veto cell unless it adds stable information beyond that field and Rank 1.

### Rank 5 — C-5 realized-volatility state

Defer. It has the highest redundancy with Five-Tool and the current sidecar.
A de-seasonalized completed-bar measure may be useful for cost stratification,
but the burden is to prove incrementality after removing ATR, tail, VIX, and
TOD-RVOL exposure.

### Separate safety track — C-6 LULD/status veto

Treat this as execution admissibility, not a performance feature. When a
certified status stream exists, missing or adverse state should fail closed.
Its retention should not depend on a noisy alpha estimate.

## 6. Evidence required to leave the no-feature outcome

A later preregistration should remain blocked until all of the following can
be named by immutable identity:

- a certified intraday trades-and-quotes release for GLD, IWM, and QQQ with
  NBBO, quote conditions, trades/volume, session/auction/status fields as
  applicable, source sequence/timestamps, corporate-action policy, calendar,
  gaps, and a clean/seen/burned holdout map;
- the exact Five-Tool opportunity and entry-admission timestamps and a causal
  as-of join that cannot see the next quote, fill, session close, or mark-out;
- a treatment/control replay sharing the unchanged host opportunity stream,
  sizing, fill policy, and exits, with the companion allowed only to mask
  entries;
- a content-addressed cost model separating commission, arrival quoted spread,
  effective spread, price impact/mark-outs, and any participation calibration;
- a one-family hypothesis, frozen threshold and monotone neighbors, power and
  sample floor, multiplicity accounting, OOS-native metrics, stressed costs,
  and the repository's existing removal/plateau gates; and
- an explicit result vocabulary in which `NO_TRADE`, no material
  participation, no incremental lower-bound improvement, and holdout failure
  all resolve to rejection or `INSUFFICIENT_EVIDENCE`, never threshold edits.

Until those facts exist, the correct companion is an annotation plan and a
blocked source record—not a new veto pretending that bar proxies are
execution data.

## 7. Bounded technical outcome

The source review selects **no companion**. It does justify one research-only
technical blocker slice:

- `research/five_tool_intraday_quote_evidence_v1_manifest.json` preregisters a
  blocked, zero-trial arrival-quote evidence plan. Dataset, catalog, holdout,
  quote-age threshold, and spread threshold identities remain unset.
- `chronos.research.features.arrival_quotes` performs a deterministic causal
  as-of join to the latest same-symbol quote whose source and recorder receipt
  timestamps are both no later than an explicit admission instant. It emits
  quote status, age, and relative quoted spread only for a normal, positive,
  non-locked, non-crossed, non-stale quote.
- Late receipts and future quotes cannot backfill historical evidence. Missing,
  stale, malformed, source-drifted, and ordering-ambiguous evidence fails
  closed.

This capability does not open data, freeze an economic threshold, apply a
veto, register a trial, import runtime authority, or make a performance claim.
Its next valid trigger is an owner-certified, identity-bound quote/trade release
plus a separately reviewed campaign preregistration that resolves the exact
admission clock, costs, multiplicity, and untouched holdout. Until then the
campaign remains `BLOCKED` and the answer remains `INSUFFICIENT_EVIDENCE`.
