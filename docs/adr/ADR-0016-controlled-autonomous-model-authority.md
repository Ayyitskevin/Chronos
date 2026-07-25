# ADR-0016: Controlled Autonomous Model Authority

Status: accepted (owner directive, 2026-07-25)
Date: 2026-07-25
Index entry: DECISIONS.md **D-16**, which supersedes **D-11**.
Supersedes: **ADR-0004 §5 only** (the generative-AI prohibition). ADR-0004 §§1-4 —
the structural separation of authority indexed as D-04 — are **preserved and
load-bearing**, and this ADR depends on them.

## Context

Every governing document in this repository has, until now, asserted that no
generative-model output may feed any runtime decision (ADR-0004 §5, D-11), and
that Chronos is decision-support with a human confirming each order. The owner
has now explicitly changed the mission: Chronos is to become a fully autonomous,
model-driven trading system for Interactive Brokers across equities/ETFs,
exchange-traded futures, and listed equity and index options.

That directive supersedes the blanket prohibition. It does **not** supersede the
reason the prohibition existed. The prohibition was a blunt instrument aimed at a
real hazard: a non-deterministic component acquiring the ability to move money.
This ADR replaces the blunt instrument with a precise one — the model gains
*trade-time* authority inside boundaries the owner sets at *policy time*, and
every deterministic gate that made the previous posture defensible remains in
force and unweakened.

The previous roadmap already anticipated this. `docs/AI_QUANT_GAME_PLAN.md`
reserved milestone E3a for a "standing-authorization redesign" — an
owner-pre-authorized, revocable envelope replacing per-order confirmation at
unattended rungs — and required it to ship through a reviewed release. The
AutonomyMandate below is that envelope, specified.

An honest statement of what changes: this is the single largest expansion of
risk in the project's history. Before it, no code path existed by which a
non-deterministic component could originate an order. After it, one does. The
whole of §3, §4, and §8 exists to make that path narrow, bounded, observable,
revocable, and vetoable.

## Decision

### 1. The authority split

**Policy time (owner only).** The owner defines portfolio objectives, chooses
model and provider versions, defines permitted instruments and strategies,
allocates capital and risk, promotes configurations between autonomy levels,
activates or revokes autonomous authority, and operates the durable kill switch.

**Trade time (model, inside those boundaries).** After the owner explicitly
activates an approved AutonomyMandate, the model may decide and the system may
execute without per-order human approval. Permitted decisions are HOLD, OPEN,
INCREASE, REDUCE, CLOSE, HEDGE, ROLL, CANCEL, and REPLACE.

Manual trading mode retains per-order typed confirmation. Autonomous modes
replace *that gate only* with the mandate. No other gate is replaced.

### 2. One decision type, one gateway

An approved model may originate runtime trading decisions **only** through a
typed `AITradeDecision` and the single deterministic `ModelDecisionGateway`.
Free-form chat, theses, summaries, and Markdown are never parsed into orders.

`chronos.autonomy.decision.AITradeDecision` (this milestone) is structurally
incapable of expressing a broker order — the same technique ADR-0004 §1 uses for
`StrategyProposal`. It has no account id, account fingerprint, broker order id,
permanent id, client id, exchange routing, `transmit` flag, or order-type field,
and it is frozen with `extra="forbid"` so none can be smuggled in. A decision
also does not name the mandate it is judged against: the supervisor performs
that binding, so a model cannot select its own authority.

The model's requested quantity **is not executable**. `requested_quantity` and
`requested_risk_budget_usd` are requests. Deterministic code independently:
resolves and qualifies the exact broker contract; calculates or clamps final
quantity; calculates collateral, margin, buying-power use, and exposure; selects
a permitted order form; validates tick size, lot size, multiplier, currency, and
exchange; checks market-data freshness and liquidity; reconciles account,
positions, orders, and fills; enforces risk, promotion, and mandate limits; mints
execution approval; and submits, modifies, or cancels broker orders.

**The deterministic kernel has unconditional veto authority.** It may reject or
reduce any request. The model cannot override, reinterpret, or repeatedly route
around a rejection; repeated re-submission of a refused decision is itself a
bounded, rate-limited, audited event.

Execution uses `chronos.orders` as the canonical live execution plane. **No
second, AI-specific submission path is created.** The single `transmit=True`
site (`chronos/orders/submission.py`) and its AST test survive unchanged.

### 3. Model isolation

The model worker runs **outside the broker-writing process**.

The model may receive: immutable, versioned, redacted EvidenceBundles; a bounded
allowlist of read-only tools; and high-level decision tools that write to a
durable decision queue — `submit_trade_decision`, `submit_cancel_decision`,
`submit_replace_decision`, `submit_close_or_reduce_decision`,
`submit_roll_decision`, `update_thesis`, `schedule_reassessment`. These submit
typed requests to the deterministic supervisor and never call IBKR.

The model must never receive: an IBKR client or broker object; account
credentials; low-level `placeOrder`/`cancelOrder` functions; direct
submission-module imports; arbitrary SQL, shell, filesystem, or network access;
policy-writing tools; arming or mandate-writing tools; or deployment or
code-modification tools.

`chronos.autonomy` therefore imports nothing from `chronos.orders`,
`chronos.broker`, `chronos.execution`, `chronos.risk`, `chronos.api`, or
`chronos.persistence`, enforced by an AST test plus a subprocess `sys.modules`
probe — the pattern already guarding the UI (`tests/unit/test_ui_no_broker_imports.py`),
the historical-data plane (ADR-0011 §7), and the registry (ADR-0013 §7). The
model plane also remains barred from the holdout unlock (D-15/ADR-0013 §7): that
bar was written prospectively against `chronos.copilot` and is retargeted, not
relaxed, now that a real model plane exists.

### 4. The AutonomyMandate

`chronos.autonomy.mandate.AutonomyMandate` (this milestone) is the owner's
bounded grant. It defines: account fingerprint; operating mode; effective time
and expiration; restart-survival behavior; permitted model, prompt, tool, policy,
and schema versions; permitted asset classes; symbols, futures roots, exchanges,
and contract families; permitted strategies and order forms; allocated capital;
maximum order and position notional; maximum contracts or shares; gross and net
exposure; leverage and margin limits; buying-power and cash floors;
daily/session loss limits; peak-to-trough drawdown; per-symbol, sector, family,
and correlated-exposure limits; order, cancellation, replacement, and turnover
limits; permitted sessions and overnight holding; market-data freshness and
liquidity requirements; and promotion level.

Three properties are structural, not procedural:

- **Immutable.** Frozen, `extra="forbid"`. Creating, expanding, renewing,
  enabling, and revoking are authenticated owner *events* recorded against
  `mandate_id`; none mutates the record. The model has read-only access and no
  tool that writes this type. It cannot arm itself or change its limits.
- **Expiring.** `expires_at` is required and must follow `effective_from`. Live
  and canary-live mandates may not exceed `MAX_LIVE_MANDATE_DURATION` (30 days).
  There is no perpetual live authority; renewal is a fresh owner action.
- **Deny-by-default.** Every limit defaults to zero and every scope tuple to
  empty, so a default-constructed mandate authorizes nothing — the all-zeros
  `config/risk.example.yaml` doctrine. A submitting mandate must additionally
  state asset classes, strategies, order forms, permitted data qualities, and
  the symbols or futures roots they cover; silence is never a grant.

Operating modes are RESEARCH, BACKTEST, REPLAY, SHADOW, PAPER_AUTONOMOUS,
CANARY_LIVE_AUTONOMOUS, and LIVE_AUTONOMOUS. **Startup defaults to a non-live
mode** (`DEFAULT_AUTONOMY_MODE = SHADOW`), and an environment variable alone may
never activate live autonomous trading — activation requires an authenticated
owner action creating and enabling a mandate, on top of the existing ADR-0009
configuration conjunction. A mandate's mode may never exceed the promotion rung
its asset family has earned (`MINIMUM_PROMOTION_FOR_MODE`).

This vocabulary is deliberately **separate** from `chronos.control.modes`.
ADR-0007's unconditional denial of `TradingMode.CANARY_LIVE`/`LIVE` in the
deterministic strategy platform is untouched, and that plane stays live-incapable.

### 5. Provenance, and what is not stored

Every model run records provider and model, model and prompt version,
tool-schema version, code release, EvidenceBundle id and hash, input and output
hashes, citations, the parsed decision, deterministic gate results, and final
order disposition (Milestone 3). Owner truth, broker truth, and model-derived
records are stored separately; broker truth comes only from reconciliation and
the deterministic ledger, never from the model.

Chronos does **not** request or persist hidden model chain-of-thought. The
`thesis`, `rationale`, `key_uncertainties`, and `invalidation_conditions` fields
carry concise, decision-relevant reasoning and citations. They are recorded,
displayed, and audited; nothing in the runtime pipeline parses them into an
order parameter.

An exposure-creating decision (OPEN, INCREASE, HEDGE, ROLL, REPLACE) must cite
at least one evidence id and state its invalidation conditions, or it fails
validation.

Stated precisely, because the difference matters: this is a **presence** check,
not a support check. The contract enforces that a citation exists and is
well-formed (a 64-hex digest and an id); it does not and cannot verify that the
cited evidence exists in the bundle, that its digest matches, or that it
actually supports the thesis. Binding citations to the EvidenceBundle they claim
to come from is the gateway's job (M2), and until it ships "must cite evidence"
means only that an uncited decision is refused.

### 6. Asset-class capability matrix

The system-wide CSP-only and long-only limitations are replaced by an explicit,
versioned capability matrix expressed in the mandate's scope. Initial scope:

- **Equities and ETFs** — long positions; short positions only after separate
  promotion, with deterministic shortability and borrow checks before shorting.
  **Gap disclosed (M1 review):** `FamilyPromotion` keys promotion on asset
  *class*, so an EQUITY promotion covers `LONG_EQUITY` and `SHORT_EQUITY`
  alike — "separate promotion" for shorting is not yet expressible in the
  contract. Until M2 adds strategy-level promotion, a mandate must simply omit
  `SHORT_EQUITY` from its scope to keep shorting unauthorized;
  whole-share limit or marketable-limit orders initially; corporate-action,
  split, dividend, halt, spread, liquidity, and session checks.
- **Options** — long calls and puts, cash-secured puts, covered calls, and
  defined-risk vertical spreads; further defined-risk structures only after
  independent validation. **No uncovered short options, and no temporary naked
  exposure while constructing spreads.** Multi-leg strategies use atomic combo
  orders where supported. Deterministic max-loss and collateral calculations;
  expiration, exercise, assignment, pin, dividend, and early-assignment controls;
  aggregate delta, gamma, vega, theta, and underlying-equivalent exposure; exact
  conId, expiry, strike, right, multiplier, trading class, and deliverable.
- **Futures** — long and short; explicit root, exchange, and contract-month
  allowlists; qualified tradable contracts only; continuous contracts are
  data-only and never submitted; exact conId, local symbol, multiplier,
  currency, tick, tick value, exchange, trading hours, timezone, last-trade date,
  and first-notice date; deterministic front-contract selection and roll policy;
  no accidental delivery exposure; contract-count, notional, margin, leverage,
  and stressed-loss caps; separate intraday and overnight permissions;
  root-level aggregation across expirations; begin with liquid micro contracts.

Equity options, index options, and futures options are **separate** capabilities.
Futures options are out of scope in this release: `TradableAssetClass.FUTURE_OPTION`
is recognized vocabulary that the mandate validator refuses, following ADR-0007's
precedent that refusing in code beats refusing in configuration.

Two absences are enforced by the vocabulary itself. `StrategyForm` has no
uncovered/naked short-option member, so no mandate can authorize one and no
decision can request one. `OrderForm` has no `MARKET` member: market entries
provide no price protection and stay disabled. Any protective stop-market or
emergency-liquidation policy requires its own instrument-specific ADR, tests, and
mandate permission before that enum grows. The reference implementation's
market-order behavior is deliberately not copied.

### 7. Promotion ladder

Each asset family is promoted independently along
BACKTEST → REPLAY → SHADOW → PAPER_AUTONOMOUS → CANARY_LIVE_AUTONOMOUS →
CAPPED_LIVE_AUTONOMOUS. A stock promotion authorizes neither futures nor options.

Criteria are frozen before evaluation and include: complete contract-resolution
correctness; zero duplicate transmissions; zero unbounded-risk orders; zero
unresolved reconciliation incidents; full fill and commission reconciliation;
malformed-output and prompt-injection tests; gateway disconnect, stale-data,
timeout, reject, partial-fill, and restart tests; idempotency, tick-rounding,
quantity, multiplier, and margin property tests; a paper operational soak;
execution-quality and slippage evidence; a signed, expiring autonomy mandate; a
minimum-size live canary; and documented rollback and kill procedures.

Promotion is never granted on backtest or paper profitability alone. A material
change to the model, prompt, tool schema, decision schema, contract resolver,
risk policy, or order compiler invalidates the affected promotion record and
returns that configuration to SHADOW or PAPER.

### 8. What this ADR does NOT supersede

Autonomy supersedes the no-AI-decision rule and per-order confirmation **only
inside an active autonomous mandate**. It does not supersede, weaken, or grant
any exception to:

one canonical order-transmission boundary; single-writer lease and fencing;
durable idempotency; reconciliation to broker truth; account and contract
qualification; stale-data rejection; the durable kill switch and halt; session
and rolling drawdown breakers; capital, concentration, margin, and leverage
limits; duplicate and replay protection; order and cancellation rate limits;
restart recovery; orphan-order handling; immutable audit trails; DEMO/non-live
defaults; and the prohibition against broker mutations from tests or CI.

ADR-0004 §§1-4 (D-04) are preserved in full: strategies still emit proposals
that cannot express an order, the portfolio layer still sizes, the risk engine
is still deny-by-default with frozen policy and instance-identity approvals, and
only the execution layer talks to a broker adapter. ADR-0007's mode lock,
ADR-0009's live conjunction and ten-gate stack, and ADR-0013's holdout guardian
all stand.

**Degraded-state rule.** If the model, broker, market data, clock, database,
lease, contract resolver, risk engine, or reconciliation state is unavailable,
ambiguous, stale, or inconsistent, the system creates no new exposure, permits
only deterministic risk-reducing behavior allowed by policy, records the denial,
and alerts the owner. **An AI failure never becomes permission to trade.**

## Consequences

- A generative model can, for the first time, originate an order in this system.
  The mitigations are structural: one decision type, one gateway, one transmit
  site, an expiring owner mandate, and an unconditional deterministic veto.
- The model plane is testable without a broker and without a model: the decision
  and mandate types are pure data, and the gateway (M2) is driven by fakes.
- Cost: more boilerplate between an idea and an order, and a mandate the owner
  must deliberately renew. Both are accepted.
- Documents asserting advisory-only status are migrated, not deleted. The
  history stays legible: ADR-0004 and D-11 are marked superseded in place, with
  the scope of the supersession stated.

## Milestone sequencing

M1 (this milestone) lands governance and the two contracts, and adds **no**
broker behavior — nothing in `chronos.autonomy` is wired into any runtime path.
M2 implements mandate validation, decision admission, deterministic compilation
and sizing, policy checks, the complete broker-mutation inventory, and the
consolidation or quarantine of duplicate submission paths, against fake brokers
only. M3 the persistent brain; M4 the agent and tool layer (DEMO and SHADOW
only); M5 the terminal and scheduler with execution still disabled; M6-M10 the
graduated per-family promotions. Every milestone stops for owner approval.

## Known limitations and residuals

0. **M1 adversarial review (2026-07-25) — findings and remediation.** A five-lens
   review of the M1 diff found defects that were fixed at the top of M2, before
   any gateway work. The material ones, recorded so the history is legible:
   - `model_copy(update=...)` bypassed **every** mandate validator: a one-day
     SHADOW mandate could be copied into a ten-year `LIVE_AUTONOMOUS` mandate
     with an empty scope and a SHADOW promotion rung. Closed by
     `chronos.autonomy.base.AutonomyModel`, which re-validates on copy.
   - A single scalar `promotion_level` could not express §7's per-family
     promotion, so one family's evidence licensed another's live trading.
     Replaced by `promotions: tuple[FamilyPromotion, ...]`, required per
     permitted asset class.
   - Risk-reducing decision kinds could carry a full new-exposure payload
     (strategy, entry plan, size, direction). Now refused by kind.
   - "Deny-by-default" was false for **floors**: `min_cash_floor_usd`,
     `min_buying_power_usd`, and `max_quote_age_seconds` default to zero, which
     is the *most* permissive value. Submitting mandates must now set them.
   - A live mandate could license FROZEN/DELAYED_FROZEN/DEMO market data that
     the deterministic live gate already refuses. Now restricted to LIVE/DELAYED.
   - The AST import matcher was blind to `from chronos import autonomy`,
     silently defeating both the isolation test and the milestone guard; the
     same hole existed in the ADR-0013 holdout test. Both fixed.
   - The naked-short guarantee was a substring scan that a member named
     `SHORT_CALL` would have passed; `StrategyForm` is now pinned to its exact
     member set.
   - Several documents published controls that do not exist yet as though they
     were live. README now marks every safety bullet `[enforced]`, `[contract]`,
     or `[M2+]`.
1. **The gateway does not exist yet.** These contracts are inert in M1. A type
   that cannot express an order is necessary, not sufficient; the veto lives in
   M2's gateway and is unproven until it ships with its adversarial review.
2. **Existing kernel defects are inherited, not fixed here.** The M0 audit found
   that the writer lease is never renewed in production (a second process can
   take the lease while the first still believes it holds it, and the lease token
   is not used as a fencing token on writes or sends), that
   `max_opening_orders_per_day` is inert because its evidence is never gathered,
   that broker session evidence is never supplied so `market_open` is permanently
   ambiguous, and that option deliverable verification is set only by the demo
   broker. Unattended operation makes each strictly more dangerous. They are M2
   prerequisites and are recorded in RISK_REGISTER.md (R-24 … R-27); this ADR
   does not close them.
3. **A dormant second submission path still exists.**
   `chronos/execution/brokers/ibkr_paper.py` contains a functioning `placeOrder`
   with a hardcoded `transmit = True` that the single-transmit-site test does not
   scan. It is constructed nowhere in production, but M2 must retire, quarantine,
   or prove its isolation before autonomy operates.
4. **Prompt injection is an open problem, not a solved one.** EvidenceBundles are
   redacted and versioned and tools are allowlisted, but evidence derived from
   external text (news, filings) remains an untrusted input to a
   non-deterministic component. The deterministic kernel is the control that
   holds when injection succeeds; M4 owes explicit injection tests, and the
   promotion criteria require them.
5. **The 30-day live-mandate ceiling is a judgment, not a derived number.** It
   bounds unattended authority to a horizon the owner can be expected to revisit.
6. **Capital reality.** The account held ~USD 110 at last verification. Options
   and futures autonomy are capability work; nothing here changes the arithmetic
   that makes small-account trading cost-dominated, and no milestone should
   operationalize theater at sizes where costs dominate.
