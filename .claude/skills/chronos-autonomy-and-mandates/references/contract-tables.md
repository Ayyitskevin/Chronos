# Contract field tables — ProposedDecision / AITradeDecision / AutonomyMandate

Companion to `../SKILL.md`. Every line verified against the repo on 2026-08-02.
Status vocabulary: **ENFORCED** = some deterministic module reads it and acts;
**ADVISORY** = docstring-labeled context, never a gate; **DEAD** = readable but no
runtime consumer (Phase-1 item 7, OPEN); **STRUCTURAL** = validity is enforced at
construction by the contract itself.

## 1. ProposedDecision — everything a model may author

Source: `src/chronos/autonomy/decision.py:231-414` (fields at 260-285). The model
authors THIS type only; `AITradeDecision` (decision.py:417-438) is a
`ProposedDecision` plus the two writer-owned fields.

| Field | Constraint (decision.py) | Status | Enforcement point |
|---|---|---|---|
| `kind` | `DecisionKind` enum (enums.py:129-141) | ENFORCED | payload-matches-kind validators decision.py:394-414; admission checks 10-13; compiler matrices compiler.py:118-166 |
| `asset_class` | `TradableAssetClass` (enums.py:51-65); FUTURE_OPTION refused in code decision.py:379-384 | ENFORCED | admission `_check_instrument` admission.py:616-643; compiler |
| `symbol` | ≤32 chars, uppercased, alphabet-checked decision.py:287-293; exactly one of symbol/futures_root per class :367-384 | ENFORCED | mandate symbol allowlist, admission.py:629-642 |
| `futures_root` | ≤8 chars, alphabet-checked :295-301 | ENFORCED | same check; futures untradable anyway (compiler matrix has no FUTURE row) |
| `direction` | `DecisionDirection`, default NEUTRAL | ENFORCED | short-coherence check admission.py:680-692; HOLD may not express one decision.py:413-414 |
| `requested_strategy` | `StrategyForm \| None` | ENFORCED | strategy allowlist + required-for-exposure admission.py:646-678; risk-reducing kinds may not carry one decision.py:406-408 |
| `requested_quantity` | positive, finite, Numeric(20,8) envelope :303-308, :123-135 | ENFORCED (as one *candidate*) | sizing.py:232-234 — a request, never executable; `min()` over all ceilings picks the binding bound sizing.py:396 |
| `requested_risk_budget_usd` | positive/finite :310-315; forbidden on risk-reducing kinds :411-412 | **DEAD** | read only by the dedup fingerprint queue.py:144-147 |
| `time_horizon` | `TimeHorizon \| None` | ADVISORY | enum docstring "advisory context for the kernel, never a gate" enums.py:150 |
| `entry_plan` | `EntryPlan` (optional `PriceTrigger` + `valid_until`) decision.py:216-221 | ENFORCED (veto-only) | a trigger can only PREVENT compilation, never set a price compiler.py:568-613 |
| `exit_plan` | `ExitPlan` (profit_target / protective_stop / time_exit) decision.py:223-228 | **DEAD** | fingerprint only, queue.py:159-179; no position-management lifecycle exists |
| `protective_order_required` | bool, default False | **DEAD** | fingerprint only, queue.py:152 |
| `max_acceptable_loss_usd` | positive/finite | **DEAD** | fingerprint only, queue.py:149-151 |
| `target_client_reference` | must match `^CHR-[A-Z0-9]+-[0-9A-F]{32}$` decision.py:65, 317-328 — never a broker order id | ENFORCED | required for targeted kinds, forbidden for OPEN decision.py:386-392 |
| `thesis`, `rationale` | ≤4000 chars, control chars/ANSI/NUL refused decision.py:97-112, 330-333 | STRUCTURAL (narrative) | journaled verbatim by the named recorder only; excluded from the economic fingerprint queue.py:123-131 |
| `confidence` | Decimal 0..1 | narrative | recorded, never a gate |
| `key_uncertainties`, `invalidation_conditions` | ≤32 entries × ≤500 chars, non-blank, control-char refused :335-348 | STRUCTURAL | exposure-creating kinds MUST state invalidation conditions decision.py:355-364 |
| `evidence` | ≤64 `EvidenceCitation` (64-hex digest + id) | ENFORCED (presence) | exposure-creating kinds must cite ≥1 decision.py:355-364; bundle id+digest matched at admission.py:580-613. Presence check, not support check (ADR-0016:205-220) |
| `reassess_at` | AwareDatetime \| None | narrative | recorded only |

**The four DEAD economic fields** — `exit_plan`, `protective_order_required`,
`max_acceptable_loss_usd`, `requested_risk_budget_usd` — verified 2026-08-02: a
grep across `src/chronos` finds no reader outside `autonomy/decision.py` and the
fingerprint in `supervisor/queue.py`. A model can request a protective stop and
the system will neither place one nor refuse the decision for asking. This
violates AGENTS.md:29-30 ("Every economic-looking field must be mechanically
enforced, explicitly advisory, or forbidden. Inert authority, risk, exit, or
protection fields are release blockers") and is exactly
VISION_COMPLETION_PLAN.md:160-162 (Phase 1 item 7). Status: OPEN. Resolving it
(enforce / label advisory / forbid each field) routes through
`chronos-change-control`, not a session judgment call. Note that because they
feed the fingerprint, changing any of them mints a NEW decision_id — a fresh
retry budget — so even "dead" fields are not free to reshape.

## 2. Writer-owned fields (a model may never author)

| Field | Stamped by | Refused at |
|---|---|---|
| `decision_id` | `queue.accept` — UUIDv5 over economic content (`economic_fingerprint`, queue.py:120-156); narrative excluded so rewording a thesis does not mint a retry budget | ingress refuses payloads carrying it, ingress.py:73, 179-185 |
| `provenance` (`DecisionProvenance`, decision.py:138-180: provider, model_id, model_version, prompt_version, tool_schema_version, decision_schema_version, policy_version, evidence_bundle_id, evidence_bundle_digest, produced_at) | `queue.accept` from `HarnessIdentity` (queue.py:84-117, 182-242) | same; re-stamping an already-stamped decision refused queue.py:211-219 |

Fingerprint material (queue.py:133-156): kind, asset_class, symbol, futures_root,
direction, requested_strategy, requested_quantity, requested_risk_budget_usd,
target_client_reference, max_acceptable_loss_usd, protective_order_required, and
entry/exit trigger tuples (`_plan_fingerprint`, queue.py:159-179).

## 3. Structural order-incapability (pinned)

The decision contract tree contains none of: `account`, `account_id`,
`account_fingerprint`, `broker`, `broker_order_id`, `order_id`, `perm_id`,
`permanent_id`, `client_id`, `transmit`, `order_type`, `order_ref`, `exchange`,
`primary_exchange`, `routing`, `route`, `con_id`, `conid`, `credentials`,
`api_key`. Pinned by
`tests/safety/test_autonomy_contracts.py::test_decision_has_no_order_capable_field_anywhere`
(forbidden-name set at :87-110, test at :453). A decision also never names its
mandate — the supervisor binds authority (decision.py:16-18). All autonomy models
inherit `AutonomyModel` (base.py:31-41): frozen, `extra="forbid"`, and
`model_copy(update=...)` re-runs every validator (closes the M1 escalation).

## 4. AutonomyMandate schema and enforcement pin

Source: `src/chronos/autonomy/mandate.py:346-390`. The ENFORCED/INERT
classification below is PINNED by
`tests/safety/test_supervisor_gateway.py:634-751` — an unclassified new field
fails the suite, and a misclassified one fails the guard-the-guard test
(`test_the_classification_matches_what_the_kernel_actually_reads`, :724-751).

| Block | Fields | Status (per the pin) |
|---|---|---|
| Identity | `mandate_id`, `mandate_version` (≥1), `account_fingerprint` (pseudonymous, normalized :392-395), `owner_authorization_ref`, `authored_at`, `note` | STRUCTURAL |
| Mode & window | `mode` (`AutonomyMode`), `effective_from`, `expires_at` (> effective_from :399-400; live ≤ `MAX_LIVE_MANDATE_DURATION` = 365d :63-69, 402-407), `restart_behavior` (default `RESUME_UNTIL_EXPIRY` :374-378) | ENFORCED (admission checks 3-6) |
| `promotions` | `tuple[FamilyPromotion, ...]` (asset_class, level) :175-185, 371 | consistency-checked ONLY — see SKILL.md §7 |
| `versions: VersionPins` | 7 required non-blank pins :158-172 | ENFORCED (admission check 8) |
| `scope: InstrumentScope` | asset_classes, symbols, futures_roots, exchanges, contract_families, strategies, order_forms :188-197; FUTURE_OPTION refused :224-230; strategy↔class cross-validated :231-239 | ENFORCED except `exchanges`/`contract_families` at admission (checked at compilation against the qualified contract, compiler.py:416-476) |
| `capital: CapitalLimits` | `model_discretion` (default False :266) + 9 `max_*` ceilings + `min_buying_power_usd`, `min_cash_floor_usd` :267-277 | ALL ENFORCED (sizing) |
| `loss: LossLimits` | 4 ceilings :283-286 | ENFORCED via durable counters → DegradedReason (blocks new exposure, leaves closing possible) |
| `concentration` | `max_symbol_exposure_pct` ENFORCED; `max_sector_exposure_pct`, `max_family_exposure_pct`, `max_correlated_exposure_pct` **INERT** (test pin :676-678) | mixed |
| `activity: ActivityLimits` | 4 per-session ceilings :301-304 | ENFORCED via durable counters |
| `market_data` | `max_quote_age_seconds`, `permitted_data_qualities`, `max_relative_spread` ENFORCED; `min_option_volume`, `min_open_interest` **INERT** (:692-693) | mixed |
| `sessions: SessionPolicy` | `permitted_sessions`, `allow_overnight_holding` **INERT** (:697-698) | INERT |

Submitting-mode validators (`SUBMITTING_AUTONOMY_MODES` = PAPER_AUTONOMOUS +
CANARY_LIVE_AUTONOMOUS + LIVE_AUTONOMOUS, enums.py:189-191):

- Explicit scope required: ≥1 asset class, order form, strategy, data quality;
  symbols for symbol classes; futures_roots for FUTURE (mandate.py:423-438).
- Every in-scope family must hold the mode's minimum promotion rung (:440-456).
- Floors required in EVERY submitting mode, discretion included: positive
  `max_quote_age_seconds`, `min_cash_floor_usd`, `min_buying_power_usd`
  (:458-473). The `model_discretion` waiver returns only AFTER the floors are
  validated (:475-481) and skips only the required-ceiling checks (:483-521).
- Live modes may permit only LIVE/DELAYED data (:523-535); STALE and UNKNOWN are
  never permitted in any mode (:78-85).

**Trap (deny-by-default inverts for floors):** zero is the MOST permissive value
for `min_*` floors and `max_quote_age_seconds`, which is why the validator forces
them. When adding any mandate field, first decide whether zero is the safe or
dangerous default, then classify it in `_LIMIT_ENFORCEMENT`
(test_supervisor_gateway.py:647-700) or the suite fails — that failure is the
control working.

## 5. Admission refusal codes

`AdmissionRefusal` (admission.py:113-139), one code per independent failure mode:
NO_ACTIVE_MANDATE, MANDATE_NOT_ACTIVATED, MANDATE_REVOKED, MANDATE_NOT_EFFECTIVE,
MANDATE_EXPIRED, MODE_CANNOT_SUBMIT, ACCOUNT_MISMATCH, VERSION_PIN_MISMATCH,
EVIDENCE_BUNDLE_MISMATCH, EVIDENCE_BUNDLE_UNKNOWN, DECISION_REPLAY,
RESUBMISSION_EXHAUSTED, NOT_EXECUTABLE, ASSET_CLASS_NOT_PERMITTED,
INSTRUMENT_NOT_PERMITTED, STRATEGY_NOT_PERMITTED, STRATEGY_REQUIRED,
DIRECTION_NOT_PERMITTED, PROMOTION_INSUFFICIENT, NO_ORDER_FORM_PERMITTED,
MARKET_DATA_UNAVAILABLE, MARKET_DATA_STALE, DEGRADED_STATE,
DEGRADED_RISK_REDUCTION_ONLY.
