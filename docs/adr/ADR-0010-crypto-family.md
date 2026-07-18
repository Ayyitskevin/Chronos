# ADR-0010: The CRYPTO product family (Milestone 7C)

Status: proposed (M7C in progress; design-panel review pending)
Date: 2026-07-18

## Context

Game plan A2 (LIVE_WHEEL_GAME_PLAN §6b): IBKR spot crypto (Paxos venue) through the
SAME human-confirmed pipeline and the SAME single submission boundary as options and
stocks — no family-specific submission paths. Spot only; no crypto options, wheel,
shorting, margin, staking, or transfers. Fractional Decimal quantities with venue
min-size/notional validation **from qualified contract details, never assumed**.
~24/7 family calendar. Limit orders only, crypto-exchange routing (not SMART).
`CRYPTO_ALLOWLIST` default-empty keeps the family disabled. IBKR **paper accounts do
not support crypto**, so validation is deterministic demo fixtures + the recording-spy
live-path walk + an owner-performed minimal-size live acceptance (disclosed).

Discovery (three-survey workflow, 2026-07-18) verified: the session calendar
(`trading_hours.session_for` CRYPTO branch), eligibility (`strategy/eligibility`:
empty-allowlist disables; both-list symbols refused), and persistence
(`order_intents.quantity` is `Numeric(20,8)`) already exist. The work concentrates in
the quantity type system, a crypto contract model, the risk dispatch, venue metadata,
and the adapter.

## Decision

### 1. Fractional quantity — family-conditional, hash-stable

`WheelOrderIntent.quantity` and `OrderRequest.quantity` widen from `PositiveInt` to
positive `Decimal` with a **family-conditional integrality validator**: OPTION and
STOCK quantities must be integral (the stock `whole_shares` check currently re-asserts
wholeness only via the old type — the validator now owns it); CRYPTO must be > 0 and,
when the qualified contract carries a venue `size_increment`, an exact multiple of it.

Hash/idempotency stability is a hard constraint: `idempotency_key` and
`order_summary_hash` currently embed `str(quantity)`. They switch to a canonical
`format(quantity.normalize(), "f")` (as `limit_price` already does), and **golden
regression tests pin the pre-change key/hash values for existing option/stock
intents** — `Decimal("1")` must produce byte-identical keys to the old `int` 1, and no
Decimal exponent variant may fork a key (a fork would break duplicate suppression or
produce a `CONFIRMATION_MISMATCH` between confirm and submit).

The option capital primitives (`strategy/capital.py`) require real `int` contract
counts: the risk engine's option paths pass `int(intent.quantity)` AFTER the
integrality validator guarantees it — `capital.py` itself is not modified.

### 2. `CryptoContract` — a distinct model carrying venue truth

A new domain model (reusing `UnderlyingContract` is rejected: its `security_type` is
pinned STK, it would route SMART, and the stock checks would apply):

- `con_id`, `symbol` (bare, e.g. `BTC` — allowlist entries stay alphanumeric; the
  venue pair mapping happens at qualification), `security_type: Literal[CRYPTO]`
  (new `SecurityType.CRYPTO = "CRYPTO"`), `exchange` default `"PAXOS"`, `currency`
  `"USD"`.
- Venue metadata, **all Optional and populated ONLY from qualified contract
  details**: `min_tick` (price increment), `min_size`, `size_increment`,
  `min_notional`. Absent metadata makes the dependent risk checks report **UNKNOWN
  (fail closed)** — never a default.

`Instrument` union widens to include it. New intents `OPEN_LONG_CRYPTO` /
`CLOSE_LONG_CRYPTO` (registered in the open/sell intent sets), plus
`build_crypto_intent`. The `"CRYPTO order intents are not supported until Milestone
7C"` refusal is replaced by full family validation **in the same commit** that adds
the crypto risk branch — never earlier (the intent validator is currently the only
backstop behind the API's allowlist union).

### 3. Risk: a third dispatch branch (`chronos/orders/crypto.py`, mirroring `stocks.py`)

`OrderRiskEngine.evaluate` dispatches OPTION → STOCK → **CRYPTO** (today CRYPTO would
fall into the whole-share stock checks). `validate_crypto_order` checks:

- venue conformance: quantity ≥ `min_size`, quantity an exact multiple of
  `size_increment`, notional ≥ `min_notional` — each UNKNOWN when the venue field is
  absent;
- per-order notional cap: `quantity × limit_price ≤ max_crypto_notional_per_order_usd`
  (this is the only per-order size bound for the family — `_check_max_contracts`
  auto-passes non-options);
- allocation cap: (current crypto allocation + pending crypto orders + this order's
  notional) ≤ `max_crypto_allocation_pct × net_liquidation`;
- BUY cash sufficiency net of the cash buffer and gross put obligations (mirrors
  stocks);
- SELL bounded by settled crypto holdings for the symbol — **no shorting** (mirrors
  stocks).

`RiskEvidence` gains `settled_crypto_quantity` and `current_crypto_allocation` (+
pending-crypto-notional), and the evidence provider **stops classifying every
non-OptionContract as wheel stock**: `CryptoContract` positions/orders are excluded
from `settled_long_shares`, `current_symbol_allocation`, and wheel-total allocation
(they currently would corrupt stock sell checks and concentration), and feed the new
crypto aggregates instead. `strategy/wheel_state.py` filters crypto positions/orders
explicitly so a crypto holding can never AttributeError or force MANUAL_REVIEW of the
wheel snapshot.

### 4. Calendar honesty: broker evidence must be able to CLOSE crypto

`session_for`'s CRYPTO branch currently returns OPEN before `broker_confirms_open` is
consulted — a venue halt/maintenance signal could not close the family. Restructured:
the calendar default at any hour/weekend is OPEN (that is the honest ~24/7 fact), but
`broker_confirms_open=False` now yields CLOSED/may-not-submit (broker evidence wins,
matching the equity branch). The weekly Paxos maintenance window is NOT modeled as
local clock arithmetic — the code cannot know venue facts; the broker signal is the
authority, and the runbook documents the window as an operator expectation.

### 5. Boundary: only the data gate learns anything

Per the plan, the submission boundary is NOT redesigned. The single change:
`_data_evidence`'s price-increment source becomes family-aware — CRYPTO uses the
qualified `CryptoContract.min_tick` and **fails the gate when it is absent** (the
stock 0.01 fallback would wrongly refuse or wrongly pass venue prices). Options keep
`min_tick`; stocks keep 0.01. Everything else (ten-gate walk, CAS, single transmit
line, refusal exits) is family-agnostic already.

Day-bucket semantics: the idempotency day bucket and drawdown baseline keep the
America/New_York calendar-date boundary for ALL families, crypto included — one
consistent boundary, stated here as a decision (a per-family UTC fork would open
duplicate-order windows across mismatched boundaries).

### 6. Adapter (`OfficialIBKRBroker`) — normalization FIRST, then orders

Ordering constraint (discovery risk): `instrument_from_contract` raises on secType
CRYPTO and is used by `positions()`/`executions()`/`open_orders()` — the **first real
crypto fill would wedge every broker read for the whole account**. The normalization
branch therefore lands in the same milestone as (and logically before) any path that
could create a crypto order:

- `instrument_from_contract` gains a CRYPTO branch returning `CryptoContract`;
- new `Broker.qualify_crypto(symbol)` protocol method: secType `CRYPTO`, exchange
  `PAXOS`, currency USD; harvests `min_tick` from ContractDetails the way option
  qualification does, and min-size/size-increment/min-notional from the details
  fields — **exact ibapi field names (minSize/sizeIncrement/…) carry an
  owner-verification note**; absent fields stay `None` (⇒ UNKNOWN risk checks);
- `_build_order_objects`: the quantity assignment becomes Decimal-preserving (no
  `int()` truncation — `int(Decimal("0.005")) == 0` would transmit a zero-quantity
  order); the exchange continues to come from the qualified contract (a
  `CryptoContract` carries PAXOS), `LMT`/`DAY`/RTH-only unchanged — IBKR's accepted
  TIF set for Paxos limit orders is an owner-verification item recorded in the
  runbook;
- the quote path routes by the contract's exchange (the SMART hardcode in
  `_snapshot_quote` is bypassed for crypto: quote requests are built from the
  qualified contract), and `market_data.underlying_quote` annotations widen to accept
  `CryptoContract` (it only touches `con_id`/`symbol`);
- `runtime._read_fresh_quote` gains the CRYPTO branch (today it returns None ⇒ the
  live data gate refuses — safe but nonfunctional).

### 7. API

`OrderProposeRequest.quantity` accepts exact-decimal input (int or string; JSON
floats are rejected — repo convention is exact decimal text). `_build_intent` gains
the CRYPTO branch (qualify_crypto → build_crypto_intent) mirroring STOCK; a crypto
propose currently falls into the option branch and 422s. No new UI — like stocks, the
pipeline surface is the API (existing family badges already display crypto state).

### 8. Demo fixtures and validation

`DemoBroker` gains deterministic crypto fixtures: BTC/ETH `CryptoContract`s with
venue metadata, fractional quotes, holdings for the SELL path, and new `DemoCase`
entries (empty-account profile filtering extended deliberately). `submit_order` keeps
raising `BrokerSafetyError` — demo can never transmit.

Proof (M7C-e): the recording-spy walk — ONE happy-path fractional crypto order
(`transmit=True`, PAXOS contract, size-increment-valid Decimal quantity asserted
byte-exactly on the outbound request, notional under caps) and adversarial cases each
leaving `submit_calls == []`: empty allowlist, off-allowlist symbol, notional cap,
allocation cap, sub-min-size, increment violation, SELL beyond holdings, absent venue
metadata (UNKNOWN ⇒ refused), broker-reports-closed session, stale/FROZEN quote,
missing crypto price increment at the data gate, plus the full M7 gate stack still
applying. Golden hash/idempotency stability tests for existing families. Structural
transmit-site tests unchanged.

**What this milestone cannot prove** (disclosed in README/limitations + runbook):
IBKR paper has no crypto and demo cannot transmit, so the only end-to-end proof is
the owner's minimal-size live acceptance. Owner items at M7C close: jurisdiction
crypto eligibility for the account (plan §7.6), `CRYPTO_ALLOWLIST` values (suggested
BTC, ETH), and gateway verification of the ibapi ContractDetails field names and
Paxos TIF rules.

## Consequences

- The family ships disabled: empty `CRYPTO_ALLOWLIST` refuses every crypto intent at
  eligibility, and nothing about option/stock behavior changes (pinned by golden
  hash tests and the untouched-suite gate).
- One pipeline, three families — same boundary, same gates, same audit trail.
- A live crypto order remains behind everything M7 built: the full conjunction, ten
  gates, arming, typed confirmation, kill switch, drawdown breaker.
