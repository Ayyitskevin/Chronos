# Chronos — limitations

The honest, consolidated list of what Chronos does NOT do, cannot yet prove, or defers to an
owner action. Chronos is pre-release, local-first software being built toward controlled
autonomous trading (ADR-0016 / D-16); **it does not trade autonomously today** — see the
autonomy section below for exactly what has and has not been delivered. It is not an
investment adviser or a promise of profitable trading. Equities, futures, options, and crypto
can produce rapid, substantial losses, and an autonomous system can produce them without
waiting for you. This document is the single source of truth for limitations referenced by
the README and the runbooks.

## Broker integration

- **The official `ibapi` package is not installable in this build/CI environment.** The
  `OfficialIBKRBroker` order path (`placeOrder`/`cancelOrder`, order-object construction) is
  therefore validated against fake-ibapi objects and a recording spy, not a live gateway.
  **Owner gateway verification against a running paper/live TWS or IB Gateway is an owner
  action** and is the one remaining live integration seam. The complete pipeline drives any
  `Broker` implementation, so this seam is narrow and well-typed.
- **Live trading has never been exercised from this codebase.** No test, CI run, or development
  path places an order. Any live acceptance is an owner action through the finished app.
- The real-network IBKR smoke test is opt-in (`CHRONOS_RUN_IBKR_SMOKE=1`), read-only, and
  skipped by default.
- Modify-in-place is not implemented on the official live adapter: re-price by cancel +
  re-propose (the full gate walk re-applies).

## Crypto family (built, disabled by default)

- **IBKR paper accounts do not support crypto.** There is no paper dry-run for this family. Its
  validation is (1) deterministic demo fixtures, (2) the recording-spy pipeline walk, and (3) an
  owner-performed minimal-size live acceptance. This limitation is disclosed, not papered over.
- Crypto is **deny-by-default**: an empty `CRYPTO_ALLOWLIST` disables the entire family. It never
  trades unless the owner explicitly allowlists symbols on a live account. There is no dedicated
  "crypto is live-only" code gate; the live-only effect is enforced by multiple fail-closed layers
  — IBKR paper has no crypto, so on a paper gateway a crypto order fails closed at qualification /
  market-data / venue-conformance and would be rejected by the venue regardless.
- Spot only (no crypto options ⇒ no crypto wheel), long-only, limit orders only, no margin, no
  shorting, no staking/transfer features.
- Venue min-size / size-increment / min-tick come **only** from the qualified IBKR
  ContractDetails. When absent, the dependent checks are UNKNOWN and fail closed — never assumed.
  There is no min-notional check (IBKR ContractDetails carries no such field); the venue's own
  minimum-order rejection plus the per-order MAX notional cap are the guards.
- Owner gateway items to verify before enabling crypto live: TWS API ≥ 10.10 (the Decimal
  `totalQuantity` precondition) and the exact `minSize`/`sizeIncrement` ContractDetails field
  names; the Paxos/Zero Hash routing exchange; the permitted time-in-force (`CRYPTO_TIME_IN_FORCE`);
  crypto market-data permissions; crypto `whatIf` behavior; and jurisdiction/account eligibility.

## Order pipeline and reconciliation

- Reconciliation runs on the portfolio page render and inside explicit symbol workflows.
  Scheduled/periodic reconciliation on a timer is not implemented; startup, reconnect, and
  order/fill-event reconciliation are.
- The local reader conservatively marks persisted cycles, strategy state, drafts, fills, and
  basis symbols unresolved, so only locally-empty flat symbols can publish `RECONCILED`;
  positions and owned working orders stay `MANUAL_REVIEW` until complete allocation provenance
  exists. `MANUAL_REVIEW` is the safe outcome for any ambiguity.
- A `SUBMISSION_UNKNOWN` order (an ambiguous failure after a send may have started) blocks
  further live submissions until reconciliation resolves it from positive broker truth. The
  audited operator endpoint (`POST /orders/{id}/resolve`) refreshes evidence but cannot turn
  snapshot absence into a rejection — never an auto-retry.
- The confirmation summary hash and idempotency key canonicalize the quantity but **not** the
  limit price, so two economically-identical spellings of a limit price (e.g. trailing zeros)
  would produce distinct hashes. This is a recorded, low-impact limitation: changing the
  limit-price serialization would alter every existing hash, so it is deliberately left as-is.
- Covered-call scenarios remain blocked on complete stock-allocation provenance; strategy basis,
  arbitrary quantities, real-broker margin, and IBKR order what-if beyond the demo path are not
  fully wired. Stock allocation valuation requires a current underlying quote at the service layer.
  Dividend, borrow, and corporate-action inputs are optional because the broker port does not
  provide them yet.

## Persistence and migrations

- Fresh databases are built with SQLAlchemy `create_all` (always the current models); the Alembic
  chain exists to upgrade legacy v2/v3 databases, with `0001` a no-op baseline. Migration
  completeness is guarded in CI (via pytest) by a frozen table manifest and a
  from-baseline-upgrade check, so a **table** added to the models without a migration fails CI.
  A new **column** on an existing table added without a migration is not automatically caught for
  the legacy-upgrade path (fresh DBs get it via `create_all`); add the migration explicitly.
- Chronos never upgrades a v1 database in place, adopts account-specific rows from an unscoped
  database, or fabricates provenance for legacy rows. Preserve and back up any existing file and
  configure a fresh `DATABASE_URL` until an explicit operator-reviewed import exists.

## Historical-data plane (C1, `chronos.histdata`)

- **The real IBKR fetch is owner-gated and unexercised here.** `reqHistoricalData`
  runs only against a live gateway with the official TWS API (`ibapi`, not a
  dependency — invariant 8). CI proves the store, adjustment, pacing, quality gate,
  and process isolation against a fake client only; the official client's behavior
  (volume units, exact bar-date formatting, accepted pacing) is confirmed by the
  owner on first real backfill.
- **No corporate-action data is fabricated.** The store ships with empty bars and
  empty action files; adjustment correctness is proven with synthetic split/dividend
  fixtures. Real splits/dividends are captured or entered by the owner, in **native
  as-of-ex-date basis** (never restated to a later split's terms, or the read-time
  factor double-counts).
- **`TOTAL_RETURN` is the CRSP/vendor adjusted-close approximation**, exact for splits
  and first-order for dividends — not an exact reinvested total-return index.
- **Pacing is coded to documented limits, not measured.** Cross-process coordination
  with the trading backend (a shared pacing budget) is not wired; the data process
  self-paces conservatively under its own client id.
- **The holdout embargo is a default-masked accessor, not a structural guardian.** A
  caller that reads `bars/<SYMBOL>.csv` directly bypasses it. The once-only,
  owner-typed, logged unlock and registry-brokered reads are Phase C2's job.
- **The legacy `research/data/raw/` corpus is unchanged.** C1 stands up a separate
  go-forward store (`research/data/history/`) and does not migrate or reconcile the
  heterogeneous 5-ETF CSVs.

## Options forward capture (C0, `chronos.histdata options`)

- **No expired-options history exists at any spend.** IBKR provides no historical data
  for expired options, so capture is **forward-only** — the surface accrues at calendar
  speed and will span few volatility regimes for years. Frozen-criteria Wheel
  validation stays gated on either a paid vendor (owner decision N2) or an accepted
  multi-year horizon (restated in C5). The store ships empty; the first snapshot is an
  owner-run step.
- **$0-tier data is delayed / EOD-snapshot quality, and labeled as such.** Every row
  carries its `DataQuality` and each snapshot records a staleness histogram +
  worst-case; delayed/frozen data is never presented as live. Real-time OPRA is a paid
  subscription IBKR gates on account minimums.
- **The real fetch is owner-gated and unexercised here.** `reqSecDefOptParams` /
  `reqMktData` (with model greeks) run only against a live gateway; CI exercises a fake
  client. No greeks/IV are fabricated — absent fields are `null`.
- **Capture is bounded and the bounds are recorded.** Only expirations within the
  horizon and strikes within the band of spot are captured; anything outside is absent
  *by policy*, not missing data, and the applied bounds are stored in every snapshot.

## Experiment registry + holdout guardian (C2, `chronos.registry`)

- **The M5 "burned holdout" failure is detected and refused, not absolutely impossible.**
  The guardian verifies the ledger (hash chain **plus** an out-of-band head anchor)
  before trusting it and fails closed; the anchor catches accidental/incidental
  truncation, whole-file deletion, in-place edits, and rollback. **Out of scope
  (disclosed):** an actor who rewrites *both* the ledger and its head anchor consistently
  — the anchor is not a signed, off-host root of trust; the guarantee is detection of
  incidental loss + tamper-evidence, not prevention against a writer who controls both
  files on their own disk.
- **"Owner-typed" is enforced structurally + by phrase, not by a runtime interactivity
  check** (the codebase has none). The unlock requires a module-constant phrase; no
  *shipped* automated path can import or call it (AST tests over the whole
  service/services/control/execution/orders tree + `runtime.py`; a single-unmask-site
  test), and the copilot plane is barred prospectively. **Out of scope:** a determined
  runtime-reflection evasion (importlib string-dispatch, `unlocked=<var>`), and an owner
  scripting the phrase into their *own* automation.
- **Single-use is enforced under concurrency** by an OS file lock around the
  read-verify-append critical section; two processes cannot both consume one grant.
- **The trial count is derived from *registered* runs.** The arithmetic is honest, but
  completeness depends on every data-touching run being registered — the research runner
  is not auto-wired to the registry in this milestone (a follow-on; also pointing it at
  the C1 bars+actions dual hash instead of the legacy single-CSV sha). `register_run`
  fails closed on null provenance.
- **The budget policy is a first cut** (linear credits per accrued capture session);
  burns and *active* grants spend credits, expired-unused grants are refunded; it
  *rations* unlocks, it does not model statistical power (C3/C4). Empty store ⇒ zero
  budget ⇒ unlocks fail closed.

## Strategy / research honesty

- The regime-context panel (EMA/RSI/vol-percentile) is a Pine-derived heuristic, explicitly
  labeled "not a validated signal," and has no pathway into order transmission.
- The deterministic strategy platform (`chronos.execution`/`chronos.risk`) starts halted, refuses
  every live-capable mode in code, and is never imported by the Live Wheel order pipeline.
  ADR-0016 does not change this: that plane stays live-incapable and model-free.
- Backtests and shadow scans describe would-be intents only; paper fills do not prove live
  execution quality, and past behavior does not predict future results.

## Autonomous model authority (ADR-0016 / D-16)

- **Chronos does not trade autonomously today.** Milestone 1 delivered governance and the
  typed `AITradeDecision` / `AutonomyMandate` contracts and **no broker behavior**. The
  deterministic ModelDecisionGateway, mandate validation, decision admission, sizing, the
  model worker, tools, and every autonomous execution path are Milestones 2 onward. A test
  asserts that nothing outside `chronos.autonomy` imports the contracts.
- **A type that cannot express an order is necessary, not sufficient.** The decision contract
  carries no account, broker, routing, or transmit field, and the mandate is frozen, expiring,
  and deny-by-default. The deterministic gateway that actually judges a decision landed in
  Milestone 2 (`chronos.supervisor`) and has had its own adversarial review; what it does and
  does not enforce is below.

### What the M2 gateway enforces — and what it does not

Admission (`chronos.supervisor.admission`) enforces: an owner **activation** event and its
revocation/restart state, the mandate's effective window, account fingerprint, submitting
mode, decision replay **and** bounded re-submission after refusal, model/prompt/tool/schema
version-pin agreement, evidence-**bundle** id and digest binding, HOLD as non-executable,
asset class, instrument allowlist, strategy allowlist for every exposure-creating kind,
short-direction coherence, per-family promotion, order-form availability, and market-data
freshness/quality/spread, and **policy-version** agreement. Sizing
(`chronos.supervisor.sizing`) independently derives and clamps quantity from **every**
`CapitalLimits` ceiling — per-order notional, position notional, gross and net exposure, unit
ceilings, allocated capital net of what is already deployed, leverage, margin utilisation —
plus the cash and buying-power floors and per-symbol concentration headroom.

Two properties of sizing are worth stating explicitly, because the M2 adversarial review
found the code contradicting its own published claims on both:

- **A ceiling whose evidence the supervisor did not gather is a refusal, not an ignored
  limit.** Four ceilings were previously skipped in silence while the module claimed the
  result was "never larger than any mandate ceiling". Unenforceable limits now refuse, so the
  claim is true rather than aspirational.
- **Exposure ceilings bound new exposure, not its removal.** On a risk-reducing decision the
  headroom ceilings do not apply, because an account already over a ceiling has negative
  headroom and would be refused the very order that brings it back under. Such an order is
  bounded by the position actually held, and refused outright when that position is unknown.

Degraded state follows ADR-0016 §8 in both halves: no new exposure, but risk-reducing
decisions may still proceed — unless the degradation is one that leaves position truth
unknown (unreconciled positions, a lost lease, an unreachable broker), in which case nothing
proceeds, because "closing" a position we are wrong about opens the opposite one. Each
`DegradedReason` declares which kind it is, defaulting to the blocking kind.

It does **not** enforce, and these are open gaps rather than decisions:

- ~~**`LossLimits` and `ActivityLimits` in full**~~ — **enforced since M3.** See the M3
  section below.
- ~~**`scope.exchanges` and `scope.contract_families`**~~ — **enforced since M4.**
  `chronos.supervisor.compiler` checks both against the qualified contract, which is what
  they always needed and what did not exist before.
- **Sector, family, and correlated concentration**; and the option-liquidity floors
  (`min_option_volume`, `min_open_interest`), which need option-chain evidence the supervisor
  does not gather.
- **`SessionPolicy` in full** — permitted sessions and overnight holding need a session clock
  in the supervisor; the orders plane has its own, which still applies downstream.
- **Individual evidence citations** — the bundle is bound by id and digest, but citations
  inside it are not resolved against a store, because the EvidenceBundle store is M3/M4.
- ~~**Provenance authorship**~~ — **authenticated since M4.** A model authors a
  `ProposedDecision`, which has no provenance field and no id; `chronos.supervisor.queue`
  stamps both from harness-held configuration. The version-pin check now proves authorship
  rather than agreement.

`tests/safety/test_supervisor_gateway.py` pins this list against the mandate models
themselves: every field of every limits model must be classified ENFORCED or INERT, so a new
limit cannot arrive undisclosed, and a field classified INERT that the kernel starts reading
fails the same test.

### What M3 added, and what it deliberately did not

M2's gateway had no memory. `admit` was a pure function over a `SupervisorState` the caller
assembled from nowhere — the right shape for testing a veto without a broker, but it left
three guarantees unenforceable. M3 (`chronos.supervisor.durable`) closes all three:

- **`LossLimits` and `ActivityLimits` are enforced**, against durable per-session counters.
  A breach becomes a `DegradedReason` with `blocks_risk_reduction=False`, so it stops new
  exposure while leaving the position closable — being at a loss limit is exactly when
  closing must remain possible. Routing through the existing degraded lever rather than new
  refusal codes keeps that rule with one implementation instead of two.
- **Mandate activation and revocation are durable owner events**, so
  `RestartBehavior.REQUIRE_REACTIVATION` means something. An in-memory activation vanished
  on restart and could not distinguish "reactivated" from "never activated". Revocation is
  marked in place, never deleted.
- **R-31's re-submission counters are durable.** They lived in memory, so a model could
  route around a refusal by waiting for a restart.

Two persistence prerequisites landed first, because durable state is only as trustworthy as
the store beneath it:

- **The main database now uses WAL, `synchronous=FULL`, and a `busy_timeout`**, verified on
  every connection rather than merely issued. It previously ran on SQLite's defaults, so a
  committed transaction could be lost on power loss — and a risk counter that silently rolls
  back is worse than one that does not exist, because the system would trust it.
- **Append-only tables are hash-chained** (`chronos.persistence.hash_chain`). They were
  append-only *by convention*, which is a statement about the code rather than the data.

**Known gaps, all tracked:**

- **Owner alerts have no out-of-band delivery (R-32).** `chronos.supervisor.alerts` records
  alerts durably, deduplicates recurrences, escalates but never downgrades an unacknowledged
  alert, and keeps acknowledgements forever. It does **not** send email, SMS, push, or
  webhooks. The channel is **pull**, so an owner who is not looking is not told. This is
  deliberate — no outbound network calls, no credential store beside a trading system — and
  it **blocks unattended `LIVE_AUTONOMOUS` promotion**. A structural test fails if an egress
  dependency is added quietly.
- **Session boundaries are calendar dates, not market sessions (R-34).** Counters roll at
  midnight in the caller's zone. The supervisor deliberately owns no market calendar, so a
  mandate's "session" limits are day limits until a caller supplies the real boundary.
- **Hash chaining is tamper-evident, not tamper-proof (R-33).** It detects a targeted edit,
  deletion, reordering or corruption. It cannot stop an attacker who can write to the
  database from recomputing the whole chain; that needs an external anchor Chronos does not
  have.
- **The EvidenceBundle store is still M4.** Bundles are bound by id and digest; individual
  citations are still not resolved against a store.
- **Nothing counts *for* the counters yet.** `record_activity` and `record_equity` are the
  supervisor's API, but no production caller invokes them, because the order plane is not
  yet routed through the supervisor — compilation is M4. The limits are enforced in the
  sense that a recorded breach binds; they are not yet *fed* by live trading.
- **The M1 contracts shipped with real defects, found by adversarial review and fixed in M2a.**
  The worst was an authority-escalation vector: `model_copy(update=...)` bypassed every mandate
  validator, so a one-day SHADOW mandate could be copied into a ten-year `LIVE_AUTONOMOUS` one.
  Others: promotion was not per asset family, risk-reducing decisions could carry opening
  payloads, "deny-by-default" was false for floors, and two AST guards were blind to
  `from chronos import <subpackage>`. All are fixed and regression-tested; the full list is
  ADR-0016 §"Known limitations and residuals" item 0. The honest lesson recorded here: these
  contracts are young, and their first adversarial pass found a hole per lens.
- **Prompt injection is an open problem.** EvidenceBundles will be redacted, versioned, and
  hash-pinned and tools allowlisted, but evidence derived from external text (news, filings)
  is an untrusted input to a non-deterministic component. The deterministic kernel is the
  control that holds when injection succeeds; explicit injection tests are owed by M4 and are
  a frozen promotion criterion.
- **Kernel defects the autonomy programme inherits.** The M0 audit found four that unattended
  operation makes strictly more dangerous, tracked as RISK_REGISTER R-24…R-27. Status after
  M2:
  - **R-24 (writer lease never renewed; not a fencing token) — MITIGATED, with a live
    residual.** The backend now renews the lease on a heartbeat and demotes itself to
    read-only on any renewal failure, and the submission boundary re-checks ownership in the
    database immediately before the transmit line. It is **not closed**: IBKR accepts an order
    without knowing about our lease, so broker-side fencing is unavailable and a sufficiently
    unlucky pause between the check and the wire cannot be defended from here.
  - **R-25** (`max_opening_orders_per_day` inert — its evidence is never gathered),
    **R-26** (broker session evidence is never supplied, so `market_open` is permanently
    ambiguous; fail-closed today, meaning no live equity/option order can currently pass
    risk), and **R-27** (option deliverable verification set only by the demo broker) remain
    **OPEN**. Each must be closed before the asset family it governs is promoted.
- **The dormant second submission path is QUARANTINED (R-28), not retired.**
  `chronos/execution/brokers/ibkr_paper.py` contains a working `placeOrder` with a hardcoded
  `order.transmit = True`. Because that is an *attribute assignment* outside `chronos.orders`,
  the original single-transmit-site test structurally could not see it. M2 added a
  repository-wide transmit inventory matching both spellings and any computed value, an AST
  assertion that no production module constructs the adapter, and a construction guard
  requiring `quarantine_ack=True` — which nothing in `src/` passes. An accidental wiring now
  fails loudly at construction instead of quietly acquiring an ungated broker path.
- **The 30-day live-mandate ceiling is a judgment, not a derived number.**
- **No futures capability of any kind exists yet** (no contract model, no adapter support);
  futures options are refused outright by the mandate validator in this release.
