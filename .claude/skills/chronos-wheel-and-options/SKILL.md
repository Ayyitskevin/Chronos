---
name: chronos-wheel-and-options
description: >-
  Use for implementing, reviewing, diagnosing, or documenting Chronos Wheel and
  listed-option behavior: Wheel state, cash-secured puts, covered calls,
  assignment, exercise, deliverables, option selection, capital coverage, basis,
  or MANUAL_REVIEW. Differentiator: derives the current option-domain contract
  across reconciled state, broker evidence, risk, persistence, and autonomous
  selection; use chronos-ibkr-boundary for vendor object ownership and
  chronos-research-methodology for statistical claims.
---

# Chronos Wheel and options domain

Treat this skill as a derivation procedure, not a snapshot of the Wheel. The
option surface combines broker identity, reconciled positions, strategy state,
capital coverage, lifecycle evidence, and promotion policy. A true statement in
one layer is not evidence that another layer consumes or enforces it.

Nothing in this skill authorizes a gateway connection, credential use, account
selection, market-data purchase, order preview or submission, exercise,
assignment handling, promotion, or change to a capital or safety limit.

## Start from the authority spine

Read the authorities that govern the exact task before implementation prose:

- repository and safety contract: `AGENTS.md`, `docs/AGENT_PROTOCOL.md`,
  `docs/safety.md`, and `docs/limitations.md`;
- accepted decisions and current residuals: `DECISIONS.md`, `RISK_REGISTER.md`,
  the relevant document under `docs/adr/`, especially
  `docs/adr/ADR-0009-live-submission-branch.md`,
  `docs/adr/ADR-0012-options-forward-capture.md`,
  `docs/adr/ADR-0016-controlled-autonomous-model-authority.md`,
  `docs/adr/ADR-0021-inert-economic-fields-on-the-decision-contract.md`, and
  `docs/adr/ADR-0030-deterministic-option-selection-and-evidence-receipts.md`;
- domain vocabulary: `src/chronos/domain/enums.py`,
  `src/chronos/domain/models.py`, and `src/chronos/orders/intent.py`;
- reconciled Wheel state: `src/chronos/strategy/wheel_state.py` and its caller
  in `src/chronos/services/reconciliation.py`;
- broker-derived option evidence: `src/chronos/services/option_deliverable.py`,
  `src/chronos/broker/base.py`, `src/chronos/broker/official_ibkr.py`, and
  `src/chronos/broker/ibkr.py`;
- selection and handoff: `src/chronos/strategy/strike_resolver.py`,
  `src/chronos/supervisor/option_selection.py`,
  `src/chronos/api/option_selection.py`,
  `src/chronos/api/autonomy_wiring.py`, and `src/chronos/supervisor/loop.py`;
- capital and admission: `src/chronos/strategy/capital.py`,
  `src/chronos/strategy/reservations.py`, `src/chronos/orders/risk.py`,
  `src/chronos/orders/submission.py`, and `src/chronos/config/settings.py`;
- allocation and accounting: `src/chronos/strategy/basis.py`,
  `src/chronos/persistence/schema.py`, and
  `src/chronos/persistence/repositories.py`;
- lifecycle and operations: `src/chronos/orders/state_machine.py`,
  `src/chronos/orders/tracker.py`,
  `src/chronos/orders/reconciliation_recovery.py`,
  `src/chronos/api/reconciliation_loop.py`, and `src/chronos/runtime.py`;
- research evidence: `src/chronos/histdata/options_capture.py`,
  `src/chronos/histdata/options_store.py`, and the current research documents
  and registry artifacts.

Use primary sources for market and vendor semantics. The repository decides
what Chronos currently implements; these sources decide what must not be
invented:

- OCC options disclosure document:
  <https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document>
- OCC information memos for contract adjustments:
  <https://infomemo.theocc.com/infomemo/search>
- current IBKR TWS API documentation:
  <https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/>
- IBKR contract-definition lesson:
  <https://ibkrcampus.com/campus/trading-lessons/defining-contracts-in-the-tws-api/>

Load `chronos-ibkr-boundary` before making any claim about which vendor object
owns a field, callback completion, market rules, pacing, or adapter parity.
Its Contract-vs-ContractDetails source-derivation procedure is authoritative
for locating inner contract identity versus outer qualification evidence.

## Derive the live domain map

Record `git rev-parse HEAD`, then discover symbols and callers rather than
copying an enum or module inventory into prose:

```bash
rg -n '^class (WheelStage|OptionRight|OrderIntent|OptionContract|BrokerOrder|BrokerExecution)' \
  src/chronos/domain src/chronos/orders
rg -n '^def (derive_wheel_state|assess_standard_deliverable|assess_assignment_pressure|project_strategy_basis)|^class (OrderRiskEngine|StrikeResolver|AutonomousOptionSelectionService)' \
  src/chronos
rg -n 'derive_wheel_state\(|assess_standard_deliverable\(|assess_assignment_pressure\(|project_strategy_basis\(' \
  src/chronos tests
rg -n 'OPEN_SHORT_PUT|OPEN_COVERED_CALL|CLOSE_SHORT_OPTION|MANUAL_REVIEW' \
  src/chronos tests
```

Keep these three state systems separate:

| State | Current owner to derive | Question it answers |
|---|---|---|
| Wheel strategy state | `strategy/wheel_state.py` from reconciled broker evidence | What exposure shape exists, and may the Wheel propose an opening action? |
| Order lifecycle | `orders/state_machine.py`, `orders/tracker.py`, and recovery | What happened to one submitted intent? |
| Autonomous option selection | supervisor/API selection receipt and activation artifact | Which exact contract and price, if any, may represent one model-authored economic request? |

Do not infer one from another. A selected contract is not a position, a filled
order is not complete cycle attribution, and a Wheel stage is not permission to
transmit.

`src/chronos/strategy/strike_resolver.py` owns a separate decision-support
resolver. Do not infer that it owns ADR-0030's canonical autonomous receipt;
derive the callers and evidence contract of each path independently.

Answer the four invariables for the task:

1. **Where does state live?** Name the broker observation, normalized domain
   object, derived decision, durable row or receipt, and authoritative owner.
2. **Where does feedback live?** Name the typed refusal, manual-review reason,
   lifecycle event, alert, log, operator view, and test that observes it.
3. **What breaks if this disappears?** Trace callers from the producer through
   reconciliation, risk or selection, submission, persistence, and display.
4. **When does timing work?** Identify snapshot boundaries, timestamps, evidence
   age, callback terminal signals, reconciliation generations, restart behavior,
   and periodic refresh.

If any answer comes only from an ADR, a skill, or a fixture, report it as a
claim to verify, not current behavior.

## Derive Wheel state from reconciled evidence

Inspect `WheelStateInput`, `WheelStateDecision`, `derive_wheel_state`, every
`WheelStage` member, and the production caller together. Build a temporary table
for the change:

| Evidence class | Normalization | Ambiguity or contradiction | Derived result | Downstream consumer |
|---|---|---|---|---|
| account and instrument identity | exact account, contract and currency keys | wrong scope, duplicate or conflicting identity | current fail-closed state | reconciliation/UI/risk |
| positions | signed quantities and verified option metadata | unsupported exposure or uncertain coverage | current stage and exposure totals | opening eligibility |
| working orders and fills | lifecycle plus matched permanent/client/order identity | stale, unmatched, overfilled or simultaneous actions | pending/closing/manual state | opening lock |
| corporate action or assignment evidence | explicitly supplied fields | missing producer or incomplete provenance | current conservative result | operator action |

Derive the stage list and eligible actions from source on every task. Do not
preserve a copied count or list in durable guidance. Search the production call
site for each input field: a model field with no production supplier is dormant,
not operational. Search each output consumer too; a computed reason displayed
nowhere is not feedback an operator can rely on.

`MANUAL_REVIEW` is a fail-closed result. Diagnose the missing or contradictory
evidence that produced it; never bypass it, rewrite it as a benign state, or
patch a UI-owned stage around the derivation.

## Prove option identity and deliverable evidence

For each contract fact, use the matrix in `chronos-ibkr-boundary` to identify
the vendor owner, both production adapters, normalized model field, downstream
consumer, and malformed or absent behavior. In particular, distinguish:

- contract identity from outer `ContractDetails` evidence;
- a qualified contract from a complete option chain;
- a standard-deliverable detector from an authoritative adjustment schedule;
- a multiplier from proven share, cash, or other-asset deliverables;
- a minimum tick from the routing-specific market-rule schedule;
- a quote observation from a usable, current, uncrossed market.

Trace `assess_standard_deliverable` through both adapters, the domain model,
Wheel state, risk admission, strike resolution, autonomous selection, and tests.
Missing, malformed, conflicting, adjusted, or non-authoritative evidence must
not become a guessed standard contract. OCC adjustment evidence belongs to the
exact series and effective event; symbol similarity is not proof.

For a change to this boundary, require an adapter-shaped payload to reach the
actual risk or selection consumer. Helper-only and demo-only tests do not prove
that the control can fire.

## Trace every admission layer

An economic desire to trade an option crosses several independent contracts.
Derive the current path in this order:

1. vocabulary and mandate express an allowed economic decision;
2. Wheel state permits that economic action from reconciled exposure;
3. autonomous selection, when required, gathers complete evidence and emits a
   canonical selected or no-trade receipt;
4. compiler or app wiring reproduces the exact instrument and economics;
5. risk evaluates account, session, lifecycle, deliverable, coverage,
   concentration, and applicable ceilings;
6. submission rechecks current authority and broker evidence at its own clock;
7. tracker and reconciliation record post-send truth without automatic retry.

Find the live functions and typed refusal at each step. A field that appears in
a request or receipt but changes no downstream decision is advisory or inert;
route it through `chronos-change-control` and ADR-0021 rather than claiming it is
enforced. Never weaken an earlier layer because a later layer also checks the
same fact: the clocks and evidence owners may differ.

Inventory broker mutation spellings before and after any option-path change:

```bash
rg -n 'placeOrder|cancelOrder|globalCancel|exerciseOptions|order\.transmit|submit_order|modify_order|cancel_order' \
  src/chronos tests
```

Use `tests/safety/test_broker_mutation_inventory.py` and
`tests/safety/test_single_transmit_site.py` as structural guards, but still
inspect the call graph. A grep inventory is not proof of runtime reachability.

## Derive capital, coverage, and accounting separately

For a short put, trace the verified deliverable into gross assignment
obligation, cash reservations, other pending obligations, and concentration.
For a covered call, trace exact underlying identity, settled shares, existing
short calls, pending orders, other reservations, and deliverable quantity. Read
the current Settings declarations and validators instead of copying defaults.
Premium expected from the new order is not available cash unless accepted
policy and executable code explicitly make it so.

Capital sufficiency, account funding, and strategy evidence are different
claims. Inspect the current owner-approved envelope and live account evidence;
never reuse an old account snapshot or a research CLI premise as current
authority.

Trace fills and commissions from broker normalization to durable rows before
making basis or P&L claims. The schema's existence is not proof that production
populates it. For every basis entry, derive exact account, Wheel cycle,
instrument, execution, quantity, price, multiplier, currency, and commission
provenance. Verify duplicate, late, corrected, missing, and conflicting evidence
behavior. Estimated and actual commission must remain visibly distinct.

## Handle assignment, exercise, expiry, and corporate actions honestly

Inspect `assignment_pressure.py` and all production callers before describing
the heuristic as active. A tested helper with no consumer is dormant. If it is
wired, preserve its advisory status unless an accepted decision says otherwise,
and prove where the operator sees the result.

Assignment, exercise, expiry, pin risk, dividend timing, borrow, and contract
adjustments require positive evidence. Derive which inputs the broker port can
currently supply and which remain absent. Do not infer assignment merely from a
position delta when allocation provenance is incomplete, and do not automate an
exercise or assignment response under this skill.

Any change that adds or changes automatic lifecycle action, capital policy,
option scope, exercise, assignment handling, transmission, or promotion is an
owner-gated safety/authority change. Draft the decision and obtain the required
review before implementation.

## Separate operational evidence from research evidence

An option chain used for a current decision is not an options-history corpus.
Inspect ADR-0012 and the histdata client, capture coordinator, store, manifests,
and scheduling evidence to determine what has actually been captured. Derive
whether history is empty, partial, uncertified, or usable from artifacts and
commands; do not preserve that answer in this skill.

No option-path unit test, demo broker, current quote, selection receipt, or
paper fill proves profitability. Route backtests, trial registration, holdouts,
multiple-testing controls, and promotion claims to
`chronos-research-methodology`. A promotion artifact authorizes only its exact
strategy, evidence, policy, scope, and rung; it does not transfer from equities
or from another option family.

MITIGATED remains distinct from CLOSED. Fixture coverage can show internal
behavior, but only the owner-gated real-gateway campaign can establish current
vendor behavior, account scope, permissions, callbacks, pacing, and subscription
cleanup.

## Red-green prevention loop

For every changed conjunct:

1. Add a realistic failing case at the downstream decision point. Name the
   unsafe interpretation it prevents.
2. Make the smallest change that carries evidence from its real producer to its
   owner and consumer without inventing a default.
3. Exercise good, missing, malformed, duplicate, stale, future, partial,
   conflicting, wrong-account, and restart forms relevant to the path.
4. Revert each conjunct independently and require a distinct test failure. A
   conjunct whose removal leaves all tests green is inert.
5. Verify both production adapters when broker evidence is involved, or record
   an explicit typed refusal where parity cannot be honest.
6. Run structural mutation/transmit guards and the end-to-end decision path.
7. Report demo-only, fixture-only, skipped, owner-gated, and unobserved evidence
   explicitly.

Fail closed without erasing semantics: unknown is not closed, zero, standard,
covered, reconciled, rejected, or safe.

## Diagnose a refusal or manual-review result

Reproduce from a sanitized evidence snapshot or fixture; never request account
credentials or print raw account identifiers. Follow the path from the first
producer of the disputed fact to the final refusal. Return findings in this
shape:

```text
Commit and mode: <exact commit; demo/shadow/paper/live posture>
Decision owner: <Wheel state | lifecycle | selection | risk | submission>
Broker and domain identity: <field owners and normalized objects>
State and timing: <snapshot/reconciliation generation/evidence ages>
Capital and allocation: <gross obligations and exact provenance>
First failing layer: <typed refusal or MANUAL_REVIEW reason>
Downstream effect: <what remained locked or was recorded>
Evidence commands: <rerunnable focused tests and source traces>
Gateway status: <fixture-only | owner-gated observed evidence>
Owner gate: <required action, or not applicable>
Unresolved: <missing producer, ambiguity, or residual risk>
```

## Verification

Discover focused tests from the symbols changed, then run at least:

```bash
.venv/bin/pytest tests/unit/test_wheel_options_skill_contract.py -q
.venv/bin/pytest tests/unit/test_wheel_state.py \
  tests/safety/test_option_deliverable.py \
  tests/unit/test_strike_resolver.py \
  tests/unit/test_option_selection.py \
  tests/unit/test_order_risk_engine.py -q
.venv/bin/pytest tests/safety/test_broker_mutation_inventory.py \
  tests/safety/test_single_transmit_site.py -q
make gates
```

Review `git diff -- .claude/skills/chronos-wheel-and-options tests` and rerun
every command after the last edit. Do not run a real-gateway smoke test, connect
to a broker, inspect `.env`, or use operator credentials as ordinary
verification.

## When not to use this skill

- Vendor object ownership, callback mechanics, or pacing alone: use
  `chronos-ibkr-boundary`.
- Statistical validation, trial families, holdouts, or promotion evidence: use
  `chronos-research-methodology`.
- Configuration declaration or flag wiring: use `chronos-config-and-flags`.
- Permission to alter an accepted safety or authority contract: use
  `chronos-change-control`.
- Operating a gateway or conducting the owner campaign: use
  `chronos-real-gateway-campaign` and stop at its owner gates.

## Maintenance

Keep this file procedural. Do not add dates, commits, line numbers, test counts,
enum counts, current defaults, account balances, active branch state, or a
copied inventory of stages and gates. Add a durable rule only when it tells a
future agent how to derive or verify the answer from the current repository and
primary sources.
