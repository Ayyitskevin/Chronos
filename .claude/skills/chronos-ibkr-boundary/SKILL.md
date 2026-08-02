---
name: chronos-ibkr-boundary
description: >-
  Load before touching ANY code at the IBKR broker boundary of Chronos. Triggers:
  "IBKR", "ib_async", "ibapi", "TWS", "gateway", "adapter", "ContractDetails",
  "qualify contract", "qualification", "liquidHours", "timeZoneId", "underConId",
  "minTick", "pacing", "reqHistoricalData", "add a broker field", "read a field off
  the contract", "the gate always blocks", "the check always passes", "option
  deliverable", "smoke test", or any task that adds/changes an adapter, reads a new
  field from an IBKR object, wires broker evidence into a risk check, or touches
  request pacing. This skill names the repo's signature failure class (inert
  controls — four instances, R-24..R-27) and carries the mandatory prevention
  checklist for every new IBKR-derived fact. Owner-requested: this failure class
  must not recur a fourth time at this boundary.
---

# chronos-ibkr-boundary — the IBKR integration boundary

Date-stamped 2026-08-02. All paths relative to the repo root. No real IBKR gateway
(paper or live) has EVER been connected in this project's history — every adapter
behavior described below is fixture-verified only (see §7 and the MITIGATED ≠ CLOSED
rule in §3 gate 7).

## 1. THE FAILURE CLASS — read this before writing any code here

The house failure class is the **inert control**: *a safety control fully wired,
documented, and covered by passing tests that structurally can never fire.* It has
happened **four times** in this repository (R-24, R-25, R-26, R-27 in
RISK_REGISTER.md:31-35). Every instance was caught ONLY by the M0 adversarial audit
(filed together in commit `6feaea9`, the M1 governance reset, 2026-07-25) — never by
production, because there has never been any production. Your job at this boundary is
to not create the fifth instance.

**R-24 — writer lease never renewed.** `WriterLease.renew()` had zero production
callers, so the 30-second single-writer lease silently expired mid-session while the
backend still believed it held it, and the token was never checked by DB writes or the
broker send — an advisory lock, not a fencing token. Fixed M2 (`f8d4150`): a lifespan
heartbeat renews at TTL/3, any renewal failure permanently demotes the process to
read-only, and the boundary re-checks ownership in the database immediately before the
transmit line. Disclosed residual: IBKR does not know about the lease, so broker-side
fencing is impossible (RISK_REGISTER.md:31-32). Details of the lease invariant live in
chronos-architecture-contract.

**R-25 — daily opening cap: evidence never gathered, counter never called.** Two
independent defects, each sufficient: (1) `gather` never set
`RiskEvidence.opening_orders_today`, so the field's `0` default made `0 + 1 <= limit`
pass on every call, forever; (2) `count_opening_since` — which had **zero callers** —
also filtered `action == SELL`, and OPEN ∧ SELL is exactly {OPEN_SHORT_PUT,
OPEN_COVERED_CALL}, so every stock and crypto opening would have been invisible even
once wired. This is the **only fail-OPEN instance**: the cap passed everything, so
nothing was ever refused and nothing alarmed. ADR-0010 §4 claimed it was fixed;
neither half was. Fixed M10 (`654f842`): `opening_orders_today: int | None = None`
(risk.py:104-107), market-local midnight, counted at intent creation, UNKNOWN blocks
(RISK_REGISTER.md:33).

**R-26 — session gate permanently AMBIGUOUS.** The tri-state market-session gate was
complete, correct, and tested — but its only evidence supplier,
`BrokerRiskEvidenceProvider._broker_confirms_open`, hard-returned `None`, so every
equity/option instant inside regular trading hours resolved AMBIGUOUS and blocked.
The mechanism: **`liquidHours` and `timeZoneId` live on IBKR's `ContractDetails`, not
on the `Contract` nested inside it, and `instrument_from_contract` only ever read the
inner `Contract`** — the evidence "was arriving on every qualification and being
dropped one attribute short of the code that needed it." Fail-closed, so never a money
hazard — and invisible for exactly that reason. Fixed M9 (`701ebf4`): the
`chronos.services.liquid_hours` parser plus qualification-time enrichment; the tests
weight malformed input 13-vs-5 over happy path (RISK_REGISTER.md:34).

**R-27 — option-deliverable verification set only by the demo fake.** Exactly one
thing in the codebase set `deliverable_verified=True`: `DemoBroker`, by fiat. Neither
IBKR adapter populated it, so the check FAILed every option order against a real
gateway. Same wrong-object read one layer over: **`underConId`, `underSymbol`,
`underSecType` live on `ContractDetails`, not on the `Contract` inside it.** Most
damning: a line in `tests/unit/test_ibkr_broker.py` asserted
`deliverable_verified is False` for six milestones — **a passing test that was pinning
the defect** (it now asserts `is True`, tests/unit/test_ibkr_broker.py:599-604). Fixed
M11 (`c72a8e5`) with the five-condition deliverable screen (RISK_REGISTER.md:35).

**The signature mechanism (R-26/R-27):** IBKR's `reqContractDetails` returns
`ContractDetails` objects; enrichment read only the inner `.contract`; every consumer
downstream saw `None`/`False`; and the fail-closed posture **masked the inertness** —
everything blocked, nothing alarmed, no test failed. Fail-closed hid two bugs
(R-26/R-27); fail-open hid one (R-25). Neither direction screams.

Full chronicle — timeline, commit messages, review culture, the other settled battles
— lives in **chronos-failure-archaeology**. This skill owns the current-code
prevention pattern and the touchpoint map.

## 2. The nested-object map (the domain reference)

Jargon: `Contract` is IBKR's instrument identity object (symbol, conId, strike…).
`ContractDetails` is the wrapper `reqContractDetails` returns; it holds the venue
facts and carries the `Contract` at `.contract`. Chronos normalizes the inner
`Contract` with `instrument_from_contract(details.contract)`
(src/chronos/broker/official_ibkr.py:493-551 — reads ONLY Contract-level fields),
then **enriches from the outer details object**. Absent evidence stays absent
(`None`/unchanged) — never defaulted.

### Which fact lives where (verified against this repo's usage, 2026-08-02)

| Fact | Lives on | Chronos read site |
|---|---|---|
| `conId`, `symbol`, `secType`, `currency`, `exchange`, `primaryExchange`, `lastTradeDateOrContractMonth`, `right`, `strike`, `multiplier`, `localSymbol`, `tradingClass` | inner `Contract` (`details.contract`) | `instrument_from_contract`, official_ibkr.py:493-551 |
| `liquidHours`, `timeZoneId` (session evidence, incl. holidays) | **ContractDetails** | official_ibkr.py:224-225 (`_with_session_evidence`) |
| `underConId`, `underSymbol`, `underSecType` (deliverable evidence) | **ContractDetails** | official_ibkr.py:245-253; ibkr.py:849-861 |
| `minTick` (price-increment floor) | **ContractDetails** | official_ibkr.py:1105-1109; ibkr.py:855 |
| `minSize`, `sizeIncrement` (crypto venue metadata; field names need TWS API ≥ 10.10, gateway-unverified) | **ContractDetails** | official_ibkr.py:1058-1066 (docstring caveat :1034-1039) |
| `marketRuleIds` (rule-id → contract linkage) | ContractDetails | **read NOWHERE** — only the `marketRule` callback's per-rule increments are stored (official_ibkr.py:447-448 → callbacks.py:461-482). Fifth-instance watchlist. |
| `tradingHours` | ContractDetails | **deliberately not read** — `liquidHours` is the load-bearing field and `CLOSED` its load-bearing token (liquid_hours.py:1-46) |
| `longName` and every other ContractDetails field | ContractDetails | not read anywhere in src (grep-verified) |

### Every current touchpoint of the nested-object boundary (11 sites)

These are exactly the places a fifth instance could appear. If you edit one, run §3.

| # | Site | What it reads |
|---|---|---|
| 1 | official_ibkr.py:209-228 (`_with_session_evidence`) | `details.liquidHours` (:224), `details.timeZoneId` (:225); absent ⇒ contract unchanged ⇒ AMBIGUOUS ⇒ blocked |
| 2 | official_ibkr.py:231-269 (`_with_deliverable_evidence`) | `details.underConId` (:245), `underSymbol` (:252), `underSecType` (:253); failing screen returns the contract UNCHANGED |
| 3 | official_ibkr.py:1012-1029 (`qualify_underlying`) | inner contract via `getattr(first, "contract", first)` (:1026), then session evidence from the details (:1029) |
| 4 | official_ibkr.py:1031-1069 (`qualify_crypto`) | `minTick`/`minSize`/`sizeIncrement` off the details (:1058-1066); absent ⇒ `None` ⇒ UNKNOWN ⇒ fail closed |
| 5 | official_ibkr.py:1081-1114 (`qualify_option_contracts`) | inner contract (:1103), `detail.minTick` (:1105), session (:1112), deliverable (:1113) |
| 6 | official_ibkr.py:447-448 → callbacks.py:461-482 | market-rule increments keyed by rule id; the `marketRuleIds` linkage on ContractDetails is unread |
| 7 | ibkr.py:837-862 (`_option_details`, ib_async) | filters by inner identity `detail.contract.conId` (:845); reads `exact[0].minTick` (:855), `underConId` (:849), `underSymbol`/`underSecType` (:860-861) — all details-level |
| 8 | ibkr.py:864-916 (`_option_from_ib`) | builds from Contract fields, runs the deliverable screen with the details facts (:897-905), stamps verification only on pass (:914-916) |
| 9 | demo.py:477-495 and demo.py:655-671 (`DemoBroker`) | mirrors "qualified" metadata **by fiat** (crypto min_tick/min_size/size_increment; option `deliverable_shares=100`, `deliverable_verified=True`) — the R-27 lesson: a fixture that sets a verification flag hides an adapter that doesn't |
| 10 | Downstream consumers that trust the enriched instrument and never re-read IBKR objects | evidence.py:203-211 (`intent.contract.liquid_hours`/`time_zone_id`); risk.py:291-298 (`has_verified_standard_deliverable`); submission.py:597-613 (`intent.contract.min_tick`); wheel_state.py:87; strike_resolver.py:658, 830 |
| 11 | The parser plane | liquid_hours.py:148 (`parse_liquid_hours`) / :105 (`confirms_open`); every parse failure degrades to `None` ⇒ AMBIGUOUS ⇒ blocked |

## 3. THE PREVENTION CHECKLIST — run for EVERY new IBKR-derived fact

Any change that reads a new field off an IBKR object, or wires broker evidence into a
check, walks every numbered gate. No gate is optional. "It looks like a one-line
getattr" is how all four instances were born.

1. **Locate the field on the actual IBKR object.** Check the TWS API docs AND this
   repo's map (§2) — never assume the inner `Contract` carries it. Default suspicion:
   venue facts (hours, ticks, sizes, underlying identity, market rules, names) live on
   `ContractDetails`; identity facts live on `Contract`. R-26 and R-27 were both this
   gate skipped.

2. **Confirm the enrichment site actually passes it through.** New Contract-level
   fields go through `instrument_from_contract` (official_ibkr.py:493-551). New
   details-level fields need an enrichment step at qualification time in **both**
   adapters that qualify the family: `_with_session_evidence` /
   `_with_deliverable_evidence` / the inline crypto harvest for official_ibkr
   (touchpoints 1-5), `_option_details`/`_option_from_ib` for ib_async (touchpoints
   7-8). Downstream code reads the enriched domain instrument only (touchpoint 10) —
   never add a broker round-trip to the order path for a fact qualification already
   returned.

3. **Write the EXERCISED test.** Prove the control FIRES on the blocking path with
   realistic adapter payloads — not that the code path exists, not that a mock was
   called. Drive qualified-contract → evidence provider → check and assert the outcome
   that has never happened yet (OPEN, a refusal, PASS) AND the blocking outcomes.
   House patterns to copy: tests/safety/test_session_gate_exercised.py (9 tests) and
   tests/safety/test_opening_cap_exercised.py (14 tests). Remember: a passing test
   asserted R-27's defect (`is False`) for six milestones. A test that pins current
   behavior pins current bugs.

4. **Verify each conjunct by reverting it.** Revert/delete each half of your change in
   turn and confirm a **distinct** test fails for each. This is the house mutation
   pattern from the R-25/R-26/R-27 fixes ("each half of the fix verified by reverting
   it and confirming a distinct failure" — RISK_REGISTER.md:33, 35). A conjunct whose
   removal fails no test is an inert conjunct.

5. **Never let a fixture set a verification flag by fiat.** `DemoBroker` setting
   `deliverable_verified=True` is exactly what hid R-27 for six milestones. If your
   new fact carries a verified/confirmed/checked flag, the demo fake and test fixtures
   must earn it through the same screen production uses, or your tests must ALSO cover
   the adapter path that populates it from ContractDetails.

6. **Weight malformed-input tests toward the fail-closed direction.** R-26's fix ships
   13 malformed-input cases vs 5 happy-path (tests/safety/test_liquid_hours.py, 29
   collected). The only dangerous output at this boundary is the spurious pass
   (`True`/PASS/headroom); every parse failure, absent field, unknown timezone, or
   changed format must degrade to `None`/UNKNOWN ⇒ blocked. Fail-closed defaults are
   load-bearing: restoring `opening_orders_today: int = 0` (risk.py:104-107) silently
   re-disables the daily cap.

7. **MITIGATED ≠ CLOSED until a real gateway confirms.** Every adapter-path control in
   this repo is fixture-verified only — no real gateway has ever been connected. Your
   new field read joins that list: label its risk-register/limitations entry
   MITIGATED with the fixtures-only residual disclosed, never CLOSED, and never write
   "works against IBKR" anywhere. Claim rules live in chronos-change-control;
   evidence doctrine in chronos-validation-and-qa.

## 4. The adapters

All three implement the runtime-checkable `Broker` protocol (src/chronos/broker/
base.py:68+; error taxonomy :31-54 — `BrokerRefusedBeforeSend` at :47-54 is a PROOF
CLAIM that no bytes reached the gateway socket). All calls are serialized through
`BrokerConnectionManager` on a dedicated loop thread (src/chronos/broker/connection.py).

| Adapter | Class | Role and hard limits |
|---|---|---|
| src/chronos/broker/official_ibkr.py | `OfficialIBKRBroker` (:708) | **Canonical for live.** Official TWS API `ibapi` — NOT on PyPI, lazily imported with install guidance (`_INSTALL_GUIDANCE`, official_ibkr.py:202-206; steps in docs/ibkr_setup.md:8-28; CI never installs it). Read paths + the M7 order path + `historical_bars` (:1159-1218). Pre-send re-verification on every order call: connection, environment↔port (`verify_environment_port` :690-700; paper ports {7497,4002}, live {7496,4001} :193-194), configured account, managed-accounts membership, last-line kill-switch check (:1229-1254). `submit_order` refuses without `transmit=True` (:1375-1379) and without a `send_guard` (:1380-1383); `modify_order` refuses provably-before-send (:1463-1473). |
| src/chronos/broker/ibkr.py | `IBKRBroker` (:185) | ib_async-based READ-ONLY secondary. Every order method raises `BrokerSafetyError` (:657-676). Refuses crypto (:591-599) and refuses `historical_bars`, pointing at the official adapter (:609-630) — one pacing behaviour, one parser, on purpose. |
| src/chronos/broker/demo.py | `DemoBroker` (:61) | Deterministic in-process fake. Cannot submit or modify (:400-413). `historical_bars` emits deterministic synthetic bars stamped `source="demo"` so the terminal labels them, never presenting a synthetic series in a live register (:307-339). Sets option/crypto qualification metadata by fiat (§2 touchpoint 9). |

**Selection rules** (src/chronos/runtime.py:219-241): `BROKER_MODE=demo` ⇒
`DemoBroker`; else `BROKER_ADAPTER=ib_async` ⇒ `IBKRBroker`; else the production
default `OfficialIBKRBroker` (`broker_adapter` defaults `OFFICIAL_IBKR`,
settings.py:46). The live conjunction REQUIRES `BROKER_ADAPTER=official_ibkr` — "the
only adapter with a validated live order path" (settings.py:174-178) — and
`live_transmission_possible` re-derives that on every read (settings.py:290-301).
Full env-var table: chronos-config-and-flags.

**The quarantined fourth path (R-28):** `src/chronos/execution/brokers/ibkr_paper.py`
— the deterministic platform's paper adapter — holds the repository's dormant SECOND
transmit site, a hardcoded `order.transmit = True` attribute assignment (:160) that
the keyword-scanning AST test structurally could not see. It refuses construction
without `quarantine_ack=True` (:103, :109-117), which **nothing in src/ passes**, and
tests/safety/test_broker_mutation_inventory.py (9 tests) pins the repo-wide inventory
of BOTH transmit spellings. Never construct it; never add a new transmit-enabling site
anywhere — CI fails on either. The single-transmit-site invariant itself is homed in
chronos-architecture-contract (the one `transmit=True` in chronos.orders is
submission.py:745).

## 5. Qualification flow — how a symbol becomes a qualified contract

"Qualification" = asking the gateway to resolve a human symbol into a concrete
contract (conId) plus venue facts, via `reqContractDetails`. It is also **the only
moment enrichment happens**: session and deliverable evidence ride the qualified
domain instrument from then on; the order path never re-asks the broker for them.

- **Underlying (stock):** `qualify_underlying(symbol)` builds a STK/SMART/USD
  `Contract`, requests details, normalizes the inner contract, enriches session
  evidence from the details (official_ibkr.py:1012-1029). ib_async equivalent
  qualifies and returns `UnderlyingContract` too (both adapters share the domain
  model).
- **Option:** the API propose path hardcodes `multiplier=100` and
  `trading_class=symbol` into the spec (src/chronos/api/routes/orders.py:190-191) —
  an adjusted series cannot even be requested — then `qualify_option_contracts`
  qualifies each spec and enriches minTick + session + deliverable per detail
  (official_ibkr.py:1081-1114). Unqualifiable contracts are a 422 refusal
  (orders.py:193-198).
- **Crypto:** `qualify_crypto` (PAXOS venue) harvests min_tick/min_size/
  size_increment from the details; absent metadata stays `None` and downstream tick/
  size checks fail UNKNOWN (official_ibkr.py:1031-1069; consumer
  submission.py:597-613). The ib_async adapter refuses crypto outright.
- **OCC symbology as used here:** OCC (the US options clearing house) mints a **new
  root with a numeric suffix** (`AAPL1`, `SPY7`) whenever a series' deliverable is
  adjusted (split, spinoff, merger, special dividend); the unsuffixed root keeps the
  standard 100-share deliverable. So `trading_class == symbol` is evidence of the
  ABSENCE of adjustment — the load-bearing condition of the five-conjunct screen in
  src/chronos/services/option_deliverable.py:64-138. The 21-char OSI local symbol is
  parsed only as rejecting-direction corroboration (:134-156): an unparseable local
  symbol is NOT held against the contract (refusing on an unverified cosmetic field
  would re-create inertness), but a parsed root contradicting the OCC root refuses.
  The screen is a non-standard **detector**, not a deliverable **reader** — the TWS
  API does not expose OCC's deliverable schedule (docstring :18-28). Assignment math
  and wheel-side consequences: chronos-wheel-and-options.

Handling doctrine for constructed instruments: if you build an `OptionContract`/
`UnderlyingContract` by hand (fixtures, new adapters) without `liquid_hours`/
`time_zone_id`, every in-RTH order blocks as AMBIGUOUS — that is the system working
(models.py:122-126; blank evidence means unknown, never "no restrictions").

## 6. Pacing discipline

IBKR rate-limits `reqHistoricalData` (documented: ~60 historical requests per rolling
10 minutes, plus identical-request cooldowns). Chronos enforces a **more conservative**
budget with one shared implementation:

- **`PacingController`** (src/chronos/marketdata/pacing.py:46-84): rolling window of
  6 requests/minute plus a 15s per-key cooldown, pure function of an injected clock.
  Shared since M8c by the two callers below so one rule and one fix serve both.
- **Two self-pacing callers, two client ids, two postures:**
  - The **histdata backfill process** (research plane) is the only place a real sleep
    is allowed: `delay_before` → `time.sleep` → `record` → request
    (src/chronos/histdata/backfill.py:40-75).
  - The **backend `BarProvider`** (terminal chart, src/chronos/api/bars.py) **never
    sleeps**: it runs on the event loop of the process holding the order-pipeline
    broker connection, so a paced-out request **serves the cache marked stale** or
    refuses outright with the wait time — degradation, never latency (bars.py:21-33,
    :176-192).
- **Budget is recorded BEFORE the call, not after success** (bars.py:194-199). The
  real defect this fixed: a failed fetch that consumed no budget would let a bad
  symbol retry unthrottled on every panel poll — against the same connection that
  submits orders. Pinned by
  tests/safety/test_terminal_bars.py:289
  (`test_a_failed_fetch_still_spends_no_budget_it_did_not_use`).
- **Client-id allocation** (settings.py:49-55): the order/backend connection uses
  `ib_client_id` (default 17); the read-only histdata process uses
  `ib_data_client_id` (default 18, `ge=1` because client id 0 is TWS's master id);
  config validation refuses equal ids — TWS rejects two live connections sharing one
  (settings.py:260-264). The terminal chart issues its bars on the backend's own
  connection/id — there is no third id.
- **Disclosed residual:** each process paces ITSELF. Whether IBKR's real limits are
  shared across client ids is unknowable without a live gateway; nothing here claims a
  cross-process budget (pacing.py:18-31; bars.py:44-50). Do not "fix" this by merging
  the processes or by guessing a shared budget — it is an owner gateway-verification
  item.

## 7. The read-only smoke test — the ONLY sanctioned gateway touchpoint today

`scripts/smoke_test_ibkr.py [official_ibkr|ib_async]` (default official_ibkr) runs
the opt-in smoke test with every transmission flag forced off
(`ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false`, scripts/
smoke_test_ibkr.py:20-29). It sets `CHRONOS_RUN_IBKR_SMOKE=1` and invokes
`pytest -m ibkr tests/integration/test_ibkr_smoke.py` (marker declared at
pyproject.toml:66). Without that env var the test is skipped and imports no network
adapter (tests/integration/test_ibkr_smoke.py:15-23).

What it does, strictly read-only (test_ibkr_smoke.py:31-107): connect → connection
status → server time → account summary (asserting the connected account matches
`IB_ACCOUNT_ID`) → qualify the first allowlist symbol → option-chain parameters → one
bounded underlying quote → clean disconnect. No preview, submission, modification,
cancellation, or exercise call exists in it.

As of 2026-08-02 it has never been run against a real gateway (docs/
GO_LIVE_CHECKLIST.md:130-131 lists it as an [OWNER] TODO — "first proof this code has
ever touched a real gateway"). Running it requires an owner-provisioned TWS/IB
Gateway; that is the full Phase-2 campaign, specified in
**chronos-real-gateway-campaign** — do not improvise a connection outside it, and
nothing in this skill authorizes connecting as "verification".

## 8. When NOT to use this skill

- Wheel/deliverable **domain math**, assignment pressure, cash-secured sizing →
  **chronos-wheel-and-options** (this skill owns only the boundary read of the
  deliverable facts).
- **Running** the backend/terminal/histdata, arm/kill/halt procedures →
  **chronos-run-and-operate**.
- The **historical chronicle** of the defects, reviews, and pivots →
  **chronos-failure-archaeology**.
- Submission-pipeline gates, transmit-site/lease invariants →
  **chronos-architecture-contract**; env-var surface → **chronos-config-and-flags**;
  what counts as test evidence → **chronos-validation-and-qa**; the real-gateway
  evidence gate → **chronos-real-gateway-campaign**.

## Provenance and maintenance

Everything above verified against the working tree on **2026-08-02** (branch
claude/chronos-skills-library-bfbj29 = feat/wheel-dashboard-mvp tip, 47a8d72). All
re-verification commands are read-only; run from the repo root with the project venv
(`.venv/bin/...`, per README Setup).

| Volatile fact | Re-verify with |
|---|---|
| R-24..R-28 statuses still MITIGATED, wording unchanged | `sed -n '31,37p' RISK_REGISTER.md` |
| Enrichment helpers still read details-level fields | `grep -n "liquidHours\|timeZoneId\|underConId\|underSymbol\|underSecType" src/chronos/broker/official_ibkr.py` |
| `instrument_from_contract` still Contract-only | `sed -n '493,551p' src/chronos/broker/official_ibkr.py` |
| `marketRuleIds`/`tradingHours` still unread (watchlist) | `grep -rn "marketRuleIds\|tradingHours" src/` (expect no matches) |
| Cap evidence still `int \| None = None` | `grep -n "opening_orders_today" src/chronos/orders/risk.py` |
| ib_async details reads unchanged | `sed -n '837,916p' src/chronos/broker/ibkr.py` |
| Adapter selection unchanged | `sed -n '219,241p' src/chronos/runtime.py` and `grep -n "broker_adapter" src/chronos/config/settings.py` |
| Quarantine + transmit-site inventory hold | `.venv/bin/pytest tests/safety/test_broker_mutation_inventory.py tests/safety/test_single_transmit_site.py -q` |
| Exercised-test counts (9/14/30/29) | `.venv/bin/pytest --collect-only -q tests/safety/test_session_gate_exercised.py tests/safety/test_opening_cap_exercised.py tests/safety/test_option_deliverable.py tests/safety/test_liquid_hours.py` |
| Pacing budget-before-call rule + defaults | `sed -n '40,53p' src/chronos/marketdata/pacing.py` and `sed -n '194,199p' src/chronos/api/bars.py` |
| Client-id defaults and inequality check | `grep -n "ib_client_id\|ib_data_client_id" src/chronos/config/settings.py` |
| Smoke test still opt-in, read-only, marker `ibkr` | `sed -n '15,23p' tests/integration/test_ibkr_smoke.py` and `grep -n "ibkr:" pyproject.toml` |
| Still no gateway ever connected | `sed -n '22,23p' docs/limitations.md` and confirm chronos-real-gateway-campaign has no completed-run artifact |

If any command's output differs from what this skill states, the skill is stale:
update it in the same change that alters the boundary, and re-run §3 for any new
field read.
