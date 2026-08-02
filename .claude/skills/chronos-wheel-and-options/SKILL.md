---
name: chronos-wheel-and-options
description: >-
  Load this skill whenever a task touches the Wheel/options trading domain of Chronos:
  anything mentioning "wheel", "covered call", "cash-secured put", "assignment",
  "deliverable", "option order", "OCC", "corporate action", "wheel dashboard", or
  "MANUAL_REVIEW"; when someone asks "why is my option order refused" or why a symbol is
  stuck in MANUAL_REVIEW; when extending or debugging the wheel state derivation, the
  option-deliverable screen, cash-secured/covered-call risk checks, the basis ledger, or
  assignment handling; and before making ANY claim about whether the wheel strategy works
  or has evidence behind it. Covers the 11-stage state model, the R-27 deliverable
  detector, the orphaned assignment-pressure heuristic, every gate an option order passes,
  and the zero-backtest-evidence reality.
---

# Chronos — Wheel and options domain

Date-stamped 2026-08-02. Repo root `/home/user/Chronos`; all paths below are relative to
it. This is the most operationally mature, highest-stakes surface in the repo for real
capital. Four binding rules from AGENTS.md apply to everything here:

1. **Fail-closed and deny-by-default stay the default posture.** Never weaken a safety
   mechanism, widen autonomous authority, or treat an untested control as proven — even
   hypothetically.
2. **MITIGATED ≠ CLOSED.** Every adapter-path control is fixture-verified only; no real
   IBKR gateway (paper or live) has ever been connected in this project's history.
3. **Never claim "done/working/validated" without naming the exact evidence** (test file,
   real-gateway run, promotion artifact).
4. The **~USD 110 vs ~USD 3,000 capital question is a LIVE, unresolved owner decision** —
   flag it, never quietly assume either number.

Naming trap before anything else: `chronos.strategy` (singular) is the wheel;
`chronos.strategies` (plural) is the research platform. `chronos.orders.risk` is the wheel
order risk engine; `chronos.risk` belongs to the deterministic platform and is never
imported by the wheel path. Full two-plane map: **chronos-architecture-contract**.

## 1. Wheel primer (for the zero-context reader)

The Wheel is an options income strategy: sell a cash-secured put (you hold enough cash to
buy 100 shares at the strike if assigned), and if the put is assigned you buy the stock
and then sell covered calls against it (calls backed 1:1 by shares you own) until the
stock is called away, at which point you are flat and start again. At every step you
collect option premium, which is the strategy's income. The risks: assignment forces you
to buy a falling stock at the strike; the call caps your upside if the stock rallies; a
corporate action can change what an option contract actually delivers, silently breaking
the "100 shares per contract" math the whole cash-securing discipline assumes.

## 2. The state model: derived, never stored

**11 stages** — `WheelStage` StrEnum, `src/chronos/domain/enums.py:64-75`: `FLAT`,
`SHORT_PUT_PENDING`, `SHORT_PUT_OPEN`, `PUT_CLOSE_PENDING`, `LONG_STOCK`,
`SHORT_CALL_PENDING`, `SHORT_CALL_OPEN`, `CALL_CLOSE_PENDING`, `CLOSING`,
`ASSIGNMENT_RECONCILING`, `MANUAL_REVIEW`. (README.md:240-247 lists only 8 of these —
the enum is truth.)

**There is no stored, transitioning state machine.** `derive_wheel_state(WheelStateInput)
-> WheelStateDecision` (`src/chronos/strategy/wheel_state.py:97`) is a *pure, stateless,
fail-closed derivation from reconciled broker evidence* ("Pure, fail-closed Wheel state
derivation from reconciled broker evidence", wheel_state.py:1), recomputed on every
reconciliation pass. Do not look for transition persistence; `strategy_state.wheel_stage`
in the DB is a snapshot of this function's output, nothing more. **UI never owns strategy
state** (wheel_state.py:98 "without consulting UI session state"; README.md:244).

Resolution order once no manual-review reason fired (wheel_state.py:420-453): working
buy-to-close orders → `PUT_CLOSE_PENDING`/`CALL_CLOSE_PENDING`/`CLOSING` (mixed rights);
else pending opening puts → `SHORT_PUT_PENDING`; pending calls → `SHORT_CALL_PENDING`;
open short puts → `SHORT_PUT_OPEN`; open short calls → `SHORT_CALL_OPEN`; stock ≥ one
call lot (`eligible_call_multiplier`, default 100, wheel_state.py:42) → `LONG_STOCK`;
else `FLAT`.

**Only two stages are ever proposal-eligible** (wheel_state.py:455-458):
`FLAT → OPEN_SHORT_PUT` and `LONG_STOCK → OPEN_COVERED_CALL`. Every other stage returns
`eligible_action=None`, and `opening_actions_locked=True` unless stage ∈ {FLAT,
LONG_STOCK} (wheel_state.py:497).

**Everything ambiguous ⇒ MANUAL_REVIEW — the safe outcome, not an error.** Any one
manual-review reason forces the stage (wheel_state.py:389-401). Do not "fix" a
MANUAL_REVIEW by routing around it; fix the evidence. Trigger classes (all in
wheel_state.py):

| Trigger | Lines |
|---|---|
| Evidence from the wrong account; duplicate position rows per (account, conId) | 109-135 |
| `reconciliation_status` ≠ RECONCILED; corporate-action warning; unexplained mismatch | 136-141 |
| Active broker orders without an affirmative reconciliation match (or stale matches) | 142-163 |
| Long options ("outside the MVP Wheel model"); fractional contract quantities; short stock | 190-201, 236-237, 265-272 |
| Unverified deliverable on any short option position or option order (see §3); an unverified call also forces `unencumbered_shares` to 0 | 218-228, 273-279, 320-327 |
| Stock + short puts coexisting (ambiguous cycle ownership); puts + calls coexisting; calls exceeding matching stock per exact (underlying conId, currency); >1 opening order per right; fills not matching the short position; closing quantity > short; simultaneous open+close; multi-instrument stock lots; odd lots | 329-387 |

Crypto contracts are excluded entirely — never a wheel object, never a manual-review
trigger (wheel_state.py:185-188, 255-256; ADR-0010).

**`ASSIGNMENT_RECONCILING` is production-unreachable today — verified fact.** It is
entered only when `evidence.partial_assignment=True` (wheel_state.py:403-418), and the
only production caller of `derive_wheel_state` —
`src/chronos/services/reconciliation.py:441-451` — never sets `partial_assignment` (nor
`corporate_action_warning`); it passes only `unexplained_mismatch`. A real assignment
therefore surfaces as MANUAL_REVIEW via provenance reasons ("A nonzero broker position
lacks complete cycle and fill-allocation provenance", reconciliation.py:490-493). That is
by design, not a bug — see §8. Tests: `tests/unit/test_wheel_state.py` (44 test
functions).

## 3. Option-deliverable verification (the R-27 fix)

**Why it exists.** Assignment math is `strike × multiplier × contracts` — true only for a
STANDARD deliverable. When OCC adjusts a series (non-whole-share split, spinoff, merger,
special dividend) the contract delivers something else: 150 shares, shares plus cash,
another issuer's stock. Sizing an adjusted series as 100 shares reserves the wrong number
"in the direction that leaves the account short at assignment"
(`src/chronos/services/option_deliverable.py:9-16`; RISK_REGISTER.md:35).

**The screen:** `assess_standard_deliverable` (option_deliverable.py:64-138). Five
necessary, **conjunctive** conditions (any failure refuses; refusals list every failed
reason). A non-blank option symbol is a prerequisite (lines 90-91).

| # | Condition | Lines | What it screens out |
|---|---|---|---|
| 1 | Broker named the underlying conId (`underlying_con_id > 0`) | 96-97 | A claim on something unidentified |
| 2 | Underlying security type is exactly `STK` | 102-105 | Cash-settled/index/futures underlyings — share math is *meaningless* there, not merely imprecise |
| 3 | Underlying symbol equals the option root | 107-110 | Deliverable of a different issuer's stock |
| 4 | **Load-bearing:** OCC root (`trading_class`) equals the symbol | 115-121 | A suffixed root (AAPL1, SPY7) is how OCC marks an adjusted series — the only deliverable signal the API carries |
| 5 | Multiplier == 100 (`STANDARD_DELIVERABLE_SHARES`, line 43) | 123-124 | Non-standard multiplier (e.g. the defunct minis at 10) |

Plus rejecting-only corroboration: a 21-char OSI local symbol whose parsed root
*contradicts* the OCC root refuses (134-136; `_osi_root` 141-156). **The deliberate
asymmetry:** an *unparseable* local symbol is NOT held against the contract — IBKR's
local-symbol format is gateway-unverified (R-04), and refusing every option over an
unverified cosmetic field "would make this control inert in exactly the way R-25 and
R-26 were" (comment, lines 126-133). Parsing-and-contradicting refuses; failing-to-parse
does not.

**Where the evidence comes from.** Both adapters read `underConId` / `underSymbol` /
`underSecType` off **`ContractDetails`, not the `Contract` nested inside it** — the
wrong-object read is exactly how R-27 shipped inert for six milestones
(`src/chronos/broker/official_ibkr.py:231-269`, docstring 234-237;
`src/chronos/broker/ibkr.py:849, 860-861` feeding the screen at 897-916). For the general
Contract-vs-ContractDetails pattern and the full touchpoint map, load
**chronos-ibkr-boundary** — do not add a fourth instance of this bug class.

**Fail-closed on refusal.** A failing contract is returned **unchanged**
(`deliverable_verified` stays False; official_ibkr.py:255-262, ibkr.py:906-913 — logged
as `option_deliverable_not_standard`), and the risk engine then FAILs the order:
`_check_deliverable_verified`, `src/chronos/orders/risk.py:291-298`, FAILs unless
`contract.has_verified_standard_deliverable`
(`src/chronos/domain/models.py:157-166`: verified AND underlying_con_id AND
deliverable_shares == multiplier). Model validators make an unverified deliverable
untrustable by construction (models.py:145-155). The capital primitives independently
refuse non-standard deliverables (`src/chronos/strategy/capital.py:236-246`).

**HONESTY (non-negotiable framing).** This is a **non-standard DETECTOR, not a
deliverable READER** (option_deliverable.py:17-33): the TWS API exposes no OCC
deliverable schedule; the screen infers the *absence of an adjustment* from OCC's
root-naming convention. It is fixture-verified only, gateway-unverified — R-27 is
**MITIGATED, not CLOSED** (RISK_REGISTER.md:35; docs/limitations.md:424-429). After any
real corporate action, expect refusal; the correct source of truth is OCC adjustment
memos and a human, not this code. Never "fix" a refused adjusted option by loosening the
screen — the account-solvency math assumes strike × 100. Tests:
`tests/safety/test_option_deliverable.py` (16 test functions; register counts 30 with
parametrization).

## 4. Assignment-pressure heuristic — implemented, tested, ORPHANED

`assess_assignment_pressure` (`src/chronos/strategy/assignment_pressure.py:71-164`).
Policy defaults (lines 22-27, mirrored in `src/chronos/config/settings.py:152-157`):
near_zero_extrinsic ≤ 0.05, meaningful_extrinsic ≥ 0.10, elevated_abs_delta ≥ 0.50,
high_dte ≤ 3, elevated_dte ≤ 5, ex_dividend_window_days = 5. Precedence:

| Rule (first match on status tier) | Result | Lines |
|---|---|---|
| corporate_action_warning | UNKNOWN | 79-85 |
| stock_price/strike/dte missing | UNKNOWN | 86-92 |
| remaining_extrinsic ≤ 0.05 (independent rule) | HIGH | 108-113 |
| ITM (put: price<strike; call: price>strike, 94-98) AND dte ≤ 3 | HIGH | 114-116 |
| ITM, or abs(delta) ≥ 0.50, or dte ≤ 5 | ELEVATED | 117-128 |
| OTM AND extrinsic ≥ 0.10 AND dte > 5 | LOW | 129-136 |
| otherwise | UNKNOWN ("insufficient_evidence_for_category") | 137-139 |
| Escalation: CALL, ITM, dividend_data_reliable, 0 ≤ days_to_ex_div ≤ min(window, dte), expected_dividend > extrinsic | HIGH | 141-155 |
| borrow_warning bumps LOW/UNKNOWN | ELEVATED | 156-159 |

Every result carries `fired_rules`, a rationale, and the fixed notice "Assignment
pressure is a heuristic warning, not an assignment probability" (lines 16-18).

**REAL STATUS — do NOT present this as active.** It is fully implemented and tested
(`tests/unit/test_assignment_pressure.py`, 13 tests) but **ORPHANED: zero production
callers** (verified 2026-08-02: grep over `src/` finds `assess_assignment_pressure` /
`AssignmentPressureInput` only in their defining module; only the enum is referenced
elsewhere). It gates nothing and is displayed nowhere. `dividend_data_reliable` defaults
False with **no supplier** — "Dividend, borrow, and corporate-action inputs are optional
because the broker port does not provide them yet" (docs/limitations.md:69-71) — so the
dividend-escalation branch cannot fire in production even once wired. It would be
advisory-if-wired; wiring it is open work routed through **chronos-change-control**
(owner-visible task, not a drive-by edit).

## 5. Cash-secured and coverage discipline — gross, never premium-netted

The solvency rule (`src/chronos/strategy/reservations.py:1-10`): before proposing, reserve
at **gross** — never netting the premium you would collect — everything already
committed.

- **Short put obligation** = `strike × verified_deliverable_shares × contracts`
  (capital.py:123; `short_put_notional` capital.py:74-84). `evaluate_short_put_capital`
  (capital.py:98-142) FAILs when `uncommitted_cash < obligation` (131-132) and enforces
  post-trade allocation limits: symbol ≤ 25% NLV, total wheel ≤ 60% NLV by default
  (capital.py:195-220; settings.py:144-145).
- **Reservation layer** `reserve_cash` (reservations.py:36-62): existing short-put
  obligation + pending put orders + the proposed order + the cash buffer must fit inside
  available cash; available cash = broker `total_cash` minus resting crypto BUY notional
  (risk.py:350).
- **Buffer** = max(`min_cash_buffer_usd` [default **5000**], NLV × `min_cash_buffer_pct`
  [default 0.10]) — risk.py:337-340; settings.py:89-90.
- Composed into the tri-state `cash_secured_put` check (risk.py:315-367); malformed
  inputs become UNKNOWN, never exceptions (risk.py:326-353); UNKNOWN fails closed.
- **Covered calls**: `reserve_shares` (reservations.py:79-115) computes unencumbered
  shares (settled longs minus shares covering short calls, pending calls, other orders);
  `covered_call_coverage` (risk.py:369-412) FAILs unless the new calls fit inside
  `max_new_call_contracts`. Naked calls cannot be expressed anywhere (see §6).

**The live capital reality (2026-08-02).** The last documented account snapshot is
≈ USD 110 (docs/VISION_COMPLETION_PLAN.md:68-70), while the default buffer alone is
USD 5,000 — so with defaults **every cash-secured put is unaffordable today**, before any
strike math. This is a **LIVE owner decision** (fund the account vs accept
stock/crypto-only scale), not a bug: flag it in any plan; never "work around" it by
shrinking the buffer or assuming the historical ~USD 3k premise, whose ghost still
haunts older docs and research-plane CLI defaults.

## 6. The options gating map — every layer an option order passes

An option order that reaches the broker has passed ALL of these, in roughly this order.
When someone asks "why was my option order refused," walk this table top to bottom; the
refusal reason names the layer.

| Layer | What it enforces | Where |
|---|---|---|
| Symbol/family eligibility | Deny-by-default allowlists: OPTION and STOCK require `SYMBOL_ALLOWLIST` membership (default AAPL, MSFT, SPY); a symbol on both equity and crypto lists refuses as ambiguous | `src/chronos/strategy/eligibility.py:44-74`; settings.py:127; risk check risk.py:186-192; full config table: **chronos-config-and-flags** |
| Intent validators | `order_type` is `Literal["LMT"]` (market orders impossible); options TIF DAY-only (API 422s non-DAY for non-crypto); option intents limited to OPEN_SHORT_PUT / OPEN_COVERED_CALL / CLOSE_SHORT_OPTION; whole contracts only | `src/chronos/orders/intent.py:107, 108-111, 162-170, 193-198; src/chronos/api/routes/orders.py:149-156` |
| API propose path | Options require expiration+strike+right; spec hardcodes `multiplier=100` and `trading_class=symbol` (an adjusted series cannot even be *requested*); broker qualification required, else 422 | routes/orders.py:180-198 (190-191 hardcoding) |
| Risk engine (all tri-state; UNKNOWN blocks; overall PASS only if every check passes) | eligibility, reconciled state, market_open, limit_only, max contracts/order (default **1**), plus per-intent option checks | risk.py:140-178; settings.py:143 |
| Session gate (R-26) | `market_open` (risk.py:212-219): family-aware session; OPEN only with broker `liquidHours` confirmation read off the intent's own qualified contract; parse failure ⇒ AMBIGUOUS ⇒ blocked. `CLOSED` is the holiday token | `src/chronos/orders/evidence.py:183-211`; `src/chronos/services/trading_hours.py`, `liquid_hours.py`; details: **chronos-ibkr-boundary** |
| Daily opening cap (R-25) | `max_opening_orders_per_day` (default 3), counted at intent creation since **market-local** midnight; uncountable day ⇒ None ⇒ UNKNOWN ⇒ blocked. Applied only to OPEN intents — **closing intents are never capped** (throttling risk-reducing orders would be backwards) | risk.py:104-107, 156-157, 257-274; evidence.py:147-181; settings.py:87 |
| Deliverable screen (R-27) | `standard_deliverable_verified` FAILs any option without a verified standard deliverable (§3) | risk.py:291-298 |
| Option-specific caps | `max_open_short_option_contracts` default 5; `max_gross_assignment_usd` default 25000; cash_secured_put / covered_call_coverage / concentration (§5); closes require a matching held short | risk.py:315-469; settings.py:86, 88 |
| Submission boundary | Writer lease, paper/live branch, ten-gate live stack, single transmit site — generic pipeline, not options-specific | **chronos-architecture-contract** |

**The autonomy seam (options are refused wholesale).** An autonomous option decision
refuses at the instrument seam: chain/strike/expiry selection is not built, so the wiring
refuses "rather than pricing against a guess"
(`src/chronos/api/autonomy_wiring.py:262-265`; docs/limitations.md:444-448). Separately,
the autonomy compiler's capability matrix is a whitelist over (asset class, kind,
strategy): EQUITY_OPTION maps ONLY to cash-secured put, covered call, and
close/reduce → CLOSE_SHORT_OPTION — **the vocabulary cannot express an uncovered short
option**, and the deliberate absences (naked shorts, futures, multi-leg) are documented
as safety properties (`src/chronos/supervisor/compiler.py:108-166`). The option-chain
evidence-boundary work exists only on side branch
`codex/chronos-option-chain-selection-v1` @ ae9d256 — do NOT assume it is merged
(VISION_COMPLETION_PLAN.md:65-67). Widening any of this requires a new ADR and an owner
decision (**chronos-autonomy-and-mandates**, **chronos-change-control**).

## 7. Ledger and basis — append-only, broker cost never overwritten

- **Basis ledger** (`src/chronos/strategy/basis.py`): append-only `BasisLedgerEntry`
  rows, entry types in enums.py:149-157 (opening/closing premium, commission
  estimate/actual, assignment/called-away stock fills, manual adjustment). Validators
  enforce premium = ±(price × multiplier × qty) with side semantics; estimates stay
  provisional; stock-fill entries carry amount=0 — they are *evidence*, never basis
  rewrites; manual adjustments require a note and no execution provenance.
- **Strategy-Adjusted Basis is a projection, never a mutation.**
  `project_strategy_basis` (basis.py:226-390) computes `strategy_adjusted_basis =
  broker_average_cost + total_adjustment / eligible_shares` (basis.py:371), labeled
  "Strategy-Adjusted Basis — not tax basis" (basis.py:21), and returns
  MANUAL_REVIEW/PENDING with NO computed basis on cross-account/cycle/currency entries,
  duplicates, partial assignment, splits, symbol changes, manual trades, unexplained
  mismatch, or zero eligible shares (basis.py:237-336, blockers 244-255). The DB keeps
  `broker_average_cost` and `strategy_adjusted_basis` as SEPARATE columns
  (`src/chronos/persistence/schema.py:101-102`) — broker cost is never overwritten.
- **DB scope binding (D-03, DECISIONS.md:10).** The wheel ledger `data/chronos.db` is
  bound to one (broker_mode, environment, account_fingerprint) tuple; `SCHEMA_VERSION =
  7` (`src/chronos/persistence/database.py:20`); binding refuses foreign or unscoped DBs
  ("configure a separate DATABASE_URL"). All rows are keyed by the pseudonymous
  sha256 `account_fingerprint`, never the raw account id.

## 8. Not handled — verified and disclosed, not hidden

The following do NOT exist in the wheel path. Treat their absence as documented scope,
and their symptoms as the system working:

- **Dividends / ex-div:** the broker port supplies nothing (limitations.md:69-71); the
  only ex-div logic is inside the orphaned heuristic (§4).
- **Expiration processing:** no event handling; an expiration shows up as an unexplained
  broker-position change ⇒ MANUAL_REVIEW.
- **Exercise:** no path at all (`grep -rn exerciseOptions src/` → zero hits, verified
  2026-08-02).
- **Assignments** surface as MANUAL_REVIEW (§2 — `ASSIGNMENT_RECONCILING` is
  production-unreachable), and reconciliation currently publishes `RECONCILED` only for
  locally-empty flat symbols (limitations.md:52-58), so a post-assignment account is
  MANUAL_REVIEW **by design**. If you build assignment handling, you must wire
  `partial_assignment` and the pressure heuristic — through change control.
- **The future work is specified:** VISION_COMPLETION_PLAN.md §10 Phase 5, item 2
  (:287-290) defines the equity-options lifecycle work — authoritative deliverables,
  corporate-action adjustments, assignment/exercise, ex-dividend, expiration,
  cash-in-lieu, exact lifecycle accounting. **Multi-leg structures, if ever admitted,
  use one atomic combo order — never sequential naked legs** (:289-290; the compiler
  refuses multi-leg for the same reason, compiler.py:116-117).

## 9. EVIDENCE REALITY — what you may never claim about the wheel

**The wheel has ZERO backtested evidence, and cannot get any from IBKR.** "IBKR provides
no historical data for expired options — there is no backfill path at any spend level"
(docs/adr/ADR-0012-options-forward-capture.md:11-13; docs/limitations.md:111-116;
D-14, DECISIONS.md:21). The wheel was chosen because its premise is structural, not
backtested — no document in this repo claims an empirical wheel edge, and neither may
you.

- **Forward capture machinery exists** (`python -m chronos.histdata options`, ADR-0012,
  isolated read-only data process) but the store is **EMPTY — verified 2026-08-02**:
  `research/data/history/` contains only `HOLDOUTS.json` and `README.md`. No real
  capture has ever run (no gateway has ever been connected; the real fetch is owner-gated
  and unexercised in CI, limitations.md:122-125).
- **Phase 3 requires a complete Wheel/options lifecycle simulation before any option
  strategy can qualify** (VISION_COMPLETION_PLAN.md:231). That simulation **does not
  exist** (verified: no options simulator in the research plane).
- Without licensed vendor history (owner decision N2), option validation is
  calendar-bound and "can take multiple years" (VISION_COMPLETION_PLAN.md:332-333).

**A session may NEVER claim** the wheel "works", "has edge", "is validated", or "is
profitable" — in code comments, docs, UI copy, commit messages, or conversation. Before
any such claim could be made, ALL of the following must exist: a populated option surface
(years of owner-run forward capture OR licensed vendor history); trials registered
through the hash-chained registry with the holdout guardian (ADR-0013/D-15); the complete
lifecycle simulation; and a pass through the Phase 3 frozen gates — sample floor
max(power-required N, 100 OOS closed trades), net-expectancy and benchmark-alpha 95%
lower bounds > 0 after all costs, deflated Sharpe ≥ 0.95, FDR q ≤ 0.05, PBO ≤ 10%,
evidence across ≥3 instruments and 2 regimes, robustness and plateau checks, one
untouched holdout (VISION_COMPLETION_PLAN.md:240-254). The QQQ holdout is burned and not
clean (:61-62). The gate mechanics live in **chronos-research-methodology**.

## 10. When NOT to use this skill

| Need | Use instead |
|---|---|
| IBKR object/field semantics, Contract vs ContractDetails, qualification, pacing, the inert-control prevention checklist | **chronos-ibkr-boundary** |
| Generic order-pipeline flow (propose→preview→confirm→submit), transmit site, writer lease, kill switch vs halt | **chronos-architecture-contract** |
| Running the wheel dashboard / backend / terminal; arm/kill/revoke procedures | **chronos-run-and-operate** |
| Research gates, walk-forward, DSR, trial counting, holdout discipline | **chronos-research-methodology** |
| History of R-24..R-27 and other past defects | **chronos-failure-archaeology** |
| Autonomy mandates, AITradeDecision, promotion ladder | **chronos-autonomy-and-mandates** |

## Provenance and maintenance

All facts verified against the repo on **2026-08-02** (branch
`claude/chronos-skills-library-bfbj29`; default branch `feat/wheel-dashboard-mvp`).
Re-verify volatile facts before relying on them — read-only commands only:

| Volatile fact | Re-verify with |
|---|---|
| 11 wheel stages | `sed -n '64,75p' src/chronos/domain/enums.py` |
| Only FLAT/LONG_STOCK proposal-eligible; lock rule | `sed -n '455,458p;497p' src/chronos/strategy/wheel_state.py` |
| ASSIGNMENT_RECONCILING unreachable (no caller sets partial_assignment) | `grep -rn "derive_wheel_state(" src/ ; sed -n '441,451p' src/chronos/services/reconciliation.py` |
| Deliverable screen conditions | `sed -n '64,138p' src/chronos/services/option_deliverable.py` |
| Risk engine FAILs unverified deliverable | `sed -n '291,298p' src/chronos/orders/risk.py` |
| Assignment pressure still orphaned | `grep -rn "assess_assignment_pressure\|AssignmentPressureInput" src/` (defining module only ⇒ still orphaned) |
| Buffer/cap defaults (5000, 3, 1, 5, 25000, allowlist) | `sed -n '86,90p;127p;143p' src/chronos/config/settings.py` |
| Autonomous options still refused at the seam | `sed -n '259,266p' src/chronos/api/autonomy_wiring.py` |
| Option capture store still empty | `ls research/data/history/` |
| SCHEMA_VERSION | `grep -n "SCHEMA_VERSION = " src/chronos/persistence/database.py` |
| R-25/26/27 status rows | `sed -n '33,35p' RISK_REGISTER.md` |
| Account snapshot ≈ USD 110 still current | `sed -n '68,70p' docs/VISION_COMPLETION_PLAN.md` (and ask the owner — this is a live decision) |

If any command's output no longer matches this skill, the skill is stale: update it via
the task contract in **chronos-change-control**, and never let a stale claim stand.
