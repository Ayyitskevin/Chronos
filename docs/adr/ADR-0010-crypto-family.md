# ADR-0010: The CRYPTO product family (Milestone 7C)

Status: accepted (design-panel remediated, 2026-07-18)
Date: 2026-07-18

## Context

Game plan A2 (LIVE_WHEEL_GAME_PLAN §6b): IBKR spot crypto through the SAME
human-confirmed pipeline and the SAME single submission boundary as options and
stocks — no family-specific submission paths. Spot only; no crypto options, wheel,
shorting, margin, staking, or transfers. Fractional Decimal quantities with venue
metadata **from qualified contract details, never assumed**. ~24/7 family calendar.
Limit orders only, crypto-exchange routing (not SMART). `CRYPTO_ALLOWLIST`
default-empty keeps the family disabled. IBKR **paper accounts do not support
crypto**: validation is deterministic demo fixtures + the recording-spy live-path
walk + an owner-performed minimal-size live acceptance (disclosed).

This ADR was adversarially reviewed by a three-judge design panel (hash/idempotency
stability; risk/evidence correctness; adapter/venue realism) BEFORE implementation.
All 22 confirmed findings are folded in; §10 records the material corrections.

## Decision

### 1. Fractional quantity — family-conditional, hash-stable, scale-bounded

`WheelOrderIntent.quantity` and `OrderRequest.quantity` widen from `PositiveInt` to
positive `Decimal` with a **family-conditional validator**: OPTION and STOCK
quantities must be integral; CRYPTO must be > 0, of **exponent ≥ −8** (the
`Numeric(20,8)` persistence scale is a family invariant — SQLite round-trips
anything finer lossily, which would desynchronize the audit record from the hashed
and transmitted quantity and break `_economic_signature` replay detection), and an
exact multiple of the venue `size_increment` when known.

Hash canonicalization: keys and summary hashes serialize quantity as
`format(quantity.normalize(), "f")`. **Corrected premise (panel h-1/ris-8/ada-9):
`limit_price` does NOT normalize today** — `normalize()` is the new, required
canonicalization for quantity precisely because the limit_price pattern would fork
keys on exponent variants. The pre-existing limit_price trailing-zero fork
(`"64000.00"` vs `"64000.0"` retype defeating same-day duplicate suppression) is
recorded as a known limitation — changing it would alter existing hashes and is out
of M7C scope. The golden pins (`tests/unit/test_quantity_hash_stability.py`,
captured from the pre-change code and committed BEFORE the widening — already
merged) enforce byte-stability for existing families and the Decimal-spelling
anti-fork guarantee.

Integrality is defended in depth, not by one validator (panel h-3): the stock
branch's `whole_shares` becomes a genuine FAIL check
(`quantity == quantity.to_integral_value()`), the option paths assert integrality
before any `int()` conversion, and the model validator remains the first line.
API input is strict (panel h-8): quantity arrives as exact decimal text or int —
a `BeforeValidator` rejects JSON floats with a 422 (tested).

Every outward serialization of quantity uses one shared canonical formatter — the
API `OrderView`, event evidence, and logs can never emit E-notation
(`str(Decimal("0.00000001")) == "1E-8"` on the operator's confirm screen was a
confirmed defect class; tested).

### 2. `CryptoContract` — venue truth, honestly scoped

New domain model (`SecurityType.CRYPTO = "CRYPTO"`); reusing `UnderlyingContract`
stays rejected. Fields: `con_id`, `symbol` (bare, e.g. `BTC`), `security_type`,
`exchange` (populated from the QUALIFIED details — the gateway's own routing echo;
`"PAXOS"` is only the qualification hint, because the venue is entity-dependent
(Paxos vs Zero Hash) — panel ada-5), `currency`, and venue metadata **all Optional,
populated only from qualified ContractDetails**: `min_tick`, `min_size`,
`size_increment`.

**Min-notional is NOT a ContractDetails field (panel ada-1)** — the earlier draft
would have refused every real order forever while demo fixtures masked it. There is
no venue min-notional check. The venue's own minimum-order rejection is the venue's
guard (fail-safe direction: a too-small order is rejected, never oversized), and
Chronos's per-order bound is the MAX notional cap (§4). `min_size`/`size_increment`
require TWS API ≥ 10.10 (older builds lack the fields) — recorded as an M7C
precondition and an owner gateway-verification item; absent fields ⇒ the dependent
checks report UNKNOWN (fail closed).

New intents `OPEN_LONG_CRYPTO`/`CLOSE_LONG_CRYPTO` + `build_crypto_intent`.

### 3. TIF is designed now, not deferred (panel ada-2)

`time_in_force` is load-bearing: frozen into the intent type, hashed into the
confirmation summary, required by the limit-only risk check, and hardcoded in the
adapter. It becomes **family-conditional**: OPTION/STOCK stay `Literal["DAY"]`
semantics; CRYPTO takes its TIF from a validated setting (`crypto_time_in_force`,
allowed set {DAY, IOC}, default DAY), `_check_limit_only` becomes family-aware,
`OrderRequest` gains `time_in_force`, and the adapter maps TIF from the request
instead of hardcoding. **Gateway verification of the accepted Paxos TIF set is a
precondition of enabling the family** (it gates the final refusal-removal commit,
§7), not a post-ship runbook note.

### 4. Risk: the crypto branch (`chronos/orders/crypto.py`), corrected scopes

Dispatch OPTION → STOCK → CRYPTO. Checks:

- venue conformance: quantity ≥ `min_size`; quantity an exact multiple of
  `size_increment` (each UNKNOWN when the field is absent);
- per-order notional cap: `quantity × limit_price ≤ max_crypto_notional_per_order_usd`;
- allocation cap — **BUY/OPEN-scoped only** (panel ris-5: an owner above the cap
  after appreciation must not be blocked from REDUCING exposure):
  `(current_crypto_allocation + pending_crypto_buy_notional + this_order_notional)
  ≤ max_crypto_allocation_pct × net_liquidation`. The allocation **mark source is
  the fresh qualified-contract quote** the family already fetches; no mark ⇒
  UNKNOWN (panel ris-6 — cost-based marks under-count appreciated crypto);
- BUY cash sufficiency net of the cash buffer, gross put obligations, **and pending
  BUY-side encumbrance** (panel ris-1): `pending_crypto_buy_notional` (and the
  mirrored `pending_stock_buy_cost`) join `RiskEvidence` and are subtracted in the
  crypto BUY check, the stock BUY check, and the short-put cash reservation — a
  resting crypto BUY can no longer double-commit the cash securing a put;
- SELL bounded by `held_crypto_quantity` (renamed from "settled" — `positions()` is
  the trade-date authority and carries no settled dimension; documented) **minus
  resting crypto SELL remaining quantities** (panel ris-3 — two SELLs cannot both
  be approved against the same coins; the stock SELL check gets the same fix);
- the daily opening cap becomes real for BUY-opening families (panel ris-4/h-6):
  `count_opening_since` extends to count OPEN intents of any side, and
  `BrokerRiskEvidenceProvider` actually wires `opening_orders_today` (it never has —
  a latent stock gap fixed here).

  > **Correction (2026-07-27, M10).** Neither half of that sentence was true when
  > it was written, and the paragraph stayed wrong for five milestones. The side
  > filter was not extended and the provider did not wire the field; the cap
  > therefore never refused anything, for any family, including the crypto family
  > this ADR governs. Both defects are fixed in M10 and R-25 is where the corrected
  > account lives. The claim is left standing above rather than edited away, because
  > an ADR that quietly rewrites its own history is worth less than one that shows
  > where it was wrong.

Evidence decontamination (unchanged) now explicitly includes
`services/reconciliation.py` (panel ris-7): the coordinator receives the union of
both allowlists with a family-aware representation, so a held crypto position is
not a permanent MANUAL_REVIEW "outside the configured allowlist" symbol.
`wheel_state.py` filters crypto explicitly.

### 5. Calendar honesty — restructure lands; the production limit is disclosed

`session_for`'s CRYPTO branch is restructured so `broker_confirms_open=False`
yields CLOSED (broker evidence CAN close the family — matching the equity branch).
Honesty (panel ris-2/h-5): **no production component supplies that signal for
crypto in M7C** — the wired evidence provider returns None, which keeps the family
OPEN. "A venue halt cannot close the crypto session at risk-evaluation time in
production" therefore moves to the cannot-prove list, the runbook, and
limitations; the capability exists for the seam when a real venue-status source is
wired (candidate: tradingHours/liquidHours from qualified details — future scope).
The connection-health live gate still blocks when the gateway itself is down.

### 6. Boundary and day-bucket (unchanged from draft)

The boundary's only change stays the family-aware price-increment source in
`_data_evidence` (CRYPTO uses qualified `min_tick`, absent ⇒ gate fails; stocks
keep 0.01; options keep `min_tick`). Day-bucket stays the America/New_York
calendar date for all families (stated decision).

### 7. Mandated commit order (panel ada-3 — merges happen mid-milestone)

"Same milestone" is not a discipline when PRs merge fast. The order is mandated,
each commit safe standalone:

1. **FIRST:** `CryptoContract` + `SecurityType.CRYPTO` + the adapter
   `instrument_from_contract` CRYPTO branch (+ positions/executions/open-orders
   normalization and `wheel_state`/evidence/reconciliation tolerance). Safe with
   no order path — and it fixes today's LATENT exposure: an owner's manual TWS
   crypto purchase would already wedge every broker read.
2. Quantity widening + integrality/scale validators + canonical serialization
   (goldens must stay green; the crypto intent-validator refusal STAYS).
3. Risk branch + evidence fields + calendar restructure + TIF plumbing + settings.
4. Adapter `qualify_crypto` + Decimal-preserving order building + demo fixtures +
   API branch + fakes.
5. **LAST:** delete the `"CRYPTO order intents are not supported"` refusal — in the
   same commit as the spy suite that proves every refusal path, and only after the
   TIF/field-name preconditions are recorded as owner-verification gates.

### 8. Adapter, demo, and quote chain

As drafted (qualify_crypto, Decimal-preserving `totalQuantity`, exchange from the
qualified contract, normalization-first), plus: the full annotation chain widens
(`Broker` protocol `request_underlying_quote`, `market_data`, DemoBroker, test
fakes — panel ada-8); `Broker.qualify_crypto` is implemented by **DemoBroker and
the test fakes too** (panel ada-7 — the propose path AttributeErrors otherwise);
demo crypto fixtures thread through both profiles with the EMPTY_ACCOUNT filtering
decision explicit, and the demo configuration sets `CRYPTO_ALLOWLIST=BTC,ETH`
while the production default stays empty (otherwise the demo proof cannot pass
eligibility).

### 9. What proves it (panel-corrected)

- **Adapter-level fake-ibapi tests are mandatory (panel ada-4):** the spy records
  the domain `OrderRequest` and structurally cannot observe `_build_order_objects`
  — where the truncation fix, Decimal-onto-ibapi assignment, exchange routing, and
  TIF mapping live. A fake-ibapi family (distinct fake classes with strict
  attributes) drives preview/submit for a `CryptoContract` request asserting the
  exact Decimal survives exponent-free, the exchange is the qualified one, and the
  TIF is family-correct; option/stock requests stay integral.
- **Evidence-provider proof (panel ada-6):** unit tests of
  `BrokerRiskEvidenceProvider.gather` over a mixed portfolio (crypto + long stock +
  short options + open orders of each) asserting every aggregate includes its own
  family and excludes the others; the spy harness's canned evidence becomes
  family-aware (`session_for(intent.product_family)`, crypto fields populated).
- Spy suite: one happy-path fractional crypto order (exact Decimal quantity
  asserted byte-for-byte, qualified exchange, `transmit=True`) and adversarial
  cases each leaving `submit_calls == []`: empty allowlist, off-allowlist,
  notional cap, allocation cap (incl. the CLOSE-not-blocked case), sub-min-size,
  increment violation, scale violation (exponent < −8), SELL beyond held-minus-
  resting, absent venue metadata, broker-reports-closed session (injected),
  stale/FROZEN quote, missing crypto price increment, float-quantity 422, plus the
  full M7 gate stack. Golden stability + E-notation-free serialization tests.

**Cannot prove without the owner** (README/limitations + runbook): live
transmission (no crypto paper); venue-halt session closing (§5); and the gateway
items — ContractDetails `minSize`/`sizeIncrement` field names and TWS ≥ 10.10,
the accepted Paxos TIF set, whatIf behavior for crypto orders (panel ris-9 — the
pipeline hard-requires an accepted preview), crypto snapshot market-data behavior
and the account's crypto market-data permission, the entity's routing exchange,
jurisdiction eligibility (§7.6), and `CRYPTO_ALLOWLIST` values (suggested BTC/ETH).

### 10. What the panel changed (record)

HIGH — min-notional check removed (field does not exist in ibapi; would have been
dead-on-arrival behind passing demo fixtures); TIF designed now as family-
conditional with gateway verification gating family enablement; mandated commit
order with normalization FIRST and refusal-removal LAST; adapter-level fake-ibapi
tests made mandatory (the spy cannot see the adapter); pending BUY cash
encumbrance added to all three cash consumers; calendar restructure kept but its
production reach honestly disclosed; quantity scale bound (Numeric(20,8)) added.
MEDIUM — integrality checks made real (not decorative); one canonical outward
formatter (no E-notation); SELL encumbrance + held-vs-settled rename; daily
opening cap made real for BUY families; allocation cap BUY-scoped with a fresh-
quote mark source; reconciliation coordinator added to the decontamination list;
demo/fake protocol requirements enumerated; quote-chain annotations enumerated.
LOW — limit_price canonicalization premise corrected (its fork recorded as a known
limitation); golden-capture ordering documented (already done pre-change); strict
API quantity input; whatIf/market-data owner items added.

## Consequences

Unchanged from the draft: the family ships disabled (empty allowlist), option/stock
behavior is pinned by goldens, one pipeline serves three families, and a live
crypto order sits behind everything M7 built. The panel's net effect: the family
cannot be dead-on-arrival behind passing fixtures, cannot double-commit cash,
cannot mis-serialize on the confirm screen, and cannot reach the venue with an
unverified TIF.
