---
name: chronos-ibkr-boundary
description: >-
  Use for implementing, reviewing, or debugging Chronos code that touches IBKR,
  TWS, ibapi, ib_async, broker qualification, ContractDetails, market rules,
  quotes, historical-data pacing, or broker-derived risk evidence.
  Differentiator: this skill derives object ownership and cross-adapter evidence
  flow from current sources; use chronos-wheel-and-options for option-domain
  policy and chronos-real-gateway-campaign for any actual gateway connection.
---

# Chronos IBKR boundary

This procedure prevents broker evidence from becoming a wired-but-inert safety
control. Treat every claim about IBKR objects, adapters, request timing, and
downstream use as a derivation from the current tree and primary vendor
documentation. Do not turn today's line numbers, field counts, defaults, test
counts, or branch state into durable instructions.

Nothing in this skill authorizes connecting to TWS or IB Gateway, using
credentials, changing an owner promotion, or sending a broker mutation.

## Source hierarchy

Read the authorities that govern the proposed change before editing:

- repository policy: `AGENTS.md`, `docs/AGENT_PROTOCOL.md`,
  `docs/safety.md`, and `docs/limitations.md`;
- accepted decisions and residuals: `DECISIONS.md`, `RISK_REGISTER.md`,
  `docs/adr/ADR-0009-live-submission-branch.md`,
  `docs/adr/ADR-0011-historical-data-plane.md`,
  `docs/adr/ADR-0019-historical-bars-and-the-chart-panel.md`, and
  `docs/adr/ADR-0030-deterministic-option-selection-and-evidence-receipts.md`;
- dependency and installation truth: `pyproject.toml`,
  `requirements-dev.lock`, and `docs/ibkr_setup.md`;
- port and adapter truth: `src/chronos/broker/base.py`,
  `src/chronos/broker/official_ibkr.py`, `src/chronos/broker/ibkr.py`,
  `src/chronos/broker/demo.py`, `src/chronos/broker/connection.py`, and
  `src/chronos/broker/market_data.py`;
- selection and configuration truth: `src/chronos/runtime.py` and
  `src/chronos/config/settings.py`;
- downstream evidence and timing truth:
  `src/chronos/services/liquid_hours.py`,
  `src/chronos/services/option_deliverable.py`,
  `src/chronos/marketdata/pacing.py`, `src/chronos/api/bars.py`, and
  `src/chronos/histdata/backfill.py`, and
  `src/chronos/histdata/official_client.py`;
- gateway procedure and structural tests: `scripts/smoke_test_ibkr.py`,
  `tests/integration/test_ibkr_smoke.py`,
  `tests/safety/test_broker_mutation_inventory.py`, and
  `tests/safety/test_single_transmit_site.py`.

Use the current IBKR Campus material first, then the official API reference for
details that Campus does not expose:

- contract qualification and ContractDetails:
  <https://ibkrcampus.com/campus/trading-lessons/defining-contracts-in-the-tws-api/>
- ContractDetails member reference:
  <https://interactivebrokers.github.io/tws-api/classIBApi_1_1ContractDetails.html>
- minimum increments and market rules:
  <https://interactivebrokers.github.io/tws-api/minimum_increment.html>
- historical-request limitations:
  <https://interactivebrokers.github.io/tws-api/historical_limitations.html>
- order submission and transmit behavior:
  <https://interactivebrokers.github.io/tws-api/order_submission.html>

When documentation and installed-library behavior differ, record both. Derive
the `ib_async` version from the project constraint and lock; check whether
`ibapi` is importable without assuming CI has it. Never install or upgrade a
dependency merely to answer a boundary question.

## Derive the live boundary

Start with symbols and callers, not prose:

```bash
rg -n '^class Broker\(Protocol\)|async def (qualify_|option_|request_|historical_bars|preview_order|submit_order|modify_order|cancel_order)' \
  src/chronos/broker
rg -n 'ContractDetails|details\.contract|liquidHours|timeZoneId|underConId|underSymbol|underSecType|minTick|minSize|sizeIncrement|validExchanges|marketRuleIds|reqMarketRule' \
  src/chronos tests
rg -n 'OfficialIBKRBroker|IBKRBroker|DemoBroker|broker_adapter|broker_mode' \
  src/chronos/runtime.py src/chronos/config/settings.py
rg -n 'placeOrder|cancelOrder|transmit|globalCancel|exerciseOptions' src/chronos tests
```

Then answer the four invariables for the exact capability:

1. Where does state live? Identify the vendor object, normalized domain field,
   cache or receipt, and any durable store.
2. Where does feedback live? Identify typed errors, refusal codes, logs,
   evidence records, and the test that observes them.
3. What breaks if this is removed? Trace every caller from the `Broker(Protocol)`
   method through the manager or service to the risk, order, UI, or research
   consumer.
4. When does timing work? Identify request registration, terminal callback,
   timeout/cancellation, cache age, pacing budget, event-loop ownership, and
   disconnect cleanup.

Do not proceed while any answer is inferred only from a skill or ADR.

## Field ownership: Contract versus ContractDetails

`reqContractDetails` returns a `ContractDetails` wrapper whose
`details.contract` contains contract identity. Venue and qualification evidence
may live on the outer wrapper. Verify the exact field against the primary reference
and the installed adapter model before choosing a read site.

For every added or changed fact, build a working matrix from the current source:

| Question | Required derivation |
|---|---|
| Vendor owner | Is the fact on the inner contract, outer details object, a callback payload, or another endpoint? |
| Official adapter | Where is the raw value validated, bounded, normalized, and attached? |
| ib_async adapter | What object shape does the pinned version expose, and does behavior match or explicitly refuse? |
| Demo adapter | Is evidence earned by the production screen, or asserted by fiat? |
| Domain owner | Which typed model carries known, unknown, and conflicting states? |
| Consumer | Which manager, risk gate, compiler, chart, or research process reads it? |
| Failure | What happens for missing, malformed, duplicate, ambiguous, partial, stale, future, or contradictory input? |

Market increments require special care. `minTick` is only a
smallest-increment fact; it is not the complete price schedule. Derive the
routing pair from `validExchanges` and `marketRuleIds`, require exact
alignment with the qualified exchange, and obtain the schedule through
`reqMarketRule`. Unknown, mismatched, or incomplete mappings must not become a
guessed tick.

After the matrix is complete, inspect the full cross-adapter path. A method on
the protocol is not proof that every adapter implements it honestly. An
explicit refusal can be correct parity; silent omission, invented evidence, or
behavior that changes with adapter selection is not.

## Prevention loop for broker-derived controls

Use this red-green loop for each fact or conjunct:

1. **Red:** add a realistic adapter payload that demonstrates the missing
   behavior at the downstream decision point. Assert the typed failure, not
   merely that a helper or mock was called.
2. **Green:** make the smallest adapter, model, and consumer change that carries
   the fact without inventing a default.
3. Exercise known-good evidence and the missing, malformed, duplicate,
   ambiguous, partial, unknown, stale, future, and conflicting forms relevant
   to that API.
4. **Revert each conjunct** independently and require a distinct test to fail.
   A conjunct whose removal leaves every test green is inert.
5. Drive at least one path from a realistic adapter payload through
   normalization and the actual consumer. Helper-only tests do not prove the
   control fires.
6. Never let `DemoBroker` or a fixture set a verified, confirmed, complete,
   or authoritative flag by fiat. Reuse the production screen or pair the fake
   case with adapter-path proof.
7. Preserve partial observations as bounded failure evidence when the current
   contract requires it; never relabel a failed fresh read as a successful
   cached observation.

At this boundary, fail closed means missing evidence cannot grant authority.
It does not mean collapsing distinct conditions into a convenient false value.
Keep unknown separate from closed, zero, complete, or safe.

## Qualification and evidence flow

Trace the current flow rather than assuming qualification ends at `conId`:

1. a caller supplies bounded economic identity;
2. the adapter requests the vendor's qualification or metadata response;
3. identity is normalized from the correct nested object;
4. outer details or terminal callbacks enrich the domain contract;
5. exact-set, completion, size, source, and timestamp checks reject incomplete
   responses;
6. the manager applies cache, retry, subscription, and cancellation policy;
7. the downstream control consumes only the typed evidence it owns.

Session strings, deliverable evidence, option-chain completion, market rules,
quotes, and historical bars have different owners and terminal signals. Do not
create one generic “qualified” boolean that erases those distinctions.

For option deliverables, preserve the boundary between a non-standard detector
and an authoritative deliverable schedule. The current TWS facts and local
screen cannot be promoted into authoritative share/cash/asset evidence merely
because they are internally consistent.

## Timing and pacing

Derive pacing parameters from `src/chronos/marketdata/pacing.py` and callers;
do not copy the values into documentation.

- In the histdata process, a pacing delay may sleep because that process is
  isolated from order handling. Inspect its coordinator and record the budget
  before issuing the vendor request.
- The backend bar provider never sleeps for historical-data pacing. It serves a
  suitable cache entry marked with its actual freshness or refuses with the
  wait; it must not stall the event loop shared with the order path. Other
  market-data operations may await bounded retries, so derive timing per
  operation instead of generalizing this rule to the whole backend.
- A failed send can still consume vendor budget. Record the budget before the
  call, not after success.
- Identify which client id each process owns and verify configuration rejects
  collisions.
- Shared controller code does not create a shared cross-process budget. Treat
  real limit scope and observed gateway behavior as owner-gated evidence.
- Never hold a local lock across a broker network call unless the current design
  and tests explicitly prove why that ordering is safe.

## Mutation and gateway boundary

Before and after a boundary change, inventory all broker mutation spellings and
the guarded send site. `preview_order`, `submit_order`, `modify_order`,
and `cancel_order` are not equivalent operations, and read-only verification
must not reach any of them. Cancellation may be risk-reducing while still being
a broker mutation; preserve the current decision and gate rather than
classifying it by intuition.

The only sanctioned connection procedure is the opt-in, bounded, read-only
campaign described by `chronos-real-gateway-campaign`. The wrapper and test
listed in the source hierarchy are inspectable evidence, not permission to run
them. A real gateway, credentials, account selection, scheduling, smoke run, or
campaign is an owner action.

Default CI must remain offline. Importing or collecting an opt-in test must not
open a connection. A skipped smoke test proves only that the gate stayed closed;
a passing fake proves no vendor behavior.

## Verification

Start focused, then run the repository gate:

```bash
.venv/bin/pytest tests/unit/test_ibkr_boundary_skill_contract.py -q
.venv/bin/pytest tests/safety/test_broker_mutation_inventory.py \
  tests/safety/test_single_transmit_site.py -q
make gates
```

Add the narrow adapter, manager, service, and exercised safety tests discovered
by the live call graph. Review `git diff -- .claude/skills tests src/chronos`
and rerun every command after the last change. Do not run the opt-in smoke as
part of ordinary verification.

MITIGATED ≠ CLOSED until owner-gated gateway evidence confirms the exact vendor
behavior. Report fake-only, import-only, skipped, and unexercised paths
explicitly.

## Required output

End a boundary investigation or change with this compact record:

```text
Field owner: <vendor object/callback and primary source>
Adapter paths: <official, ib_async, demo; implemented or explicit refusal>
Downstream consumer: <manager/service/gate/store and the typed field>
Fail-closed outcome: <missing/malformed/partial/conflicting behavior>
Evidence commands: <commands run and observed results>
Gateway status: <not run | owner-run read-only evidence reference>
Owner gate: <not required | required, with the exact action withheld>
```

## Known pitfalls

- Reading only `details.contract` when the fact belongs to ContractDetails.
- Assuming a non-empty response is complete without its terminal callback.
- Treating `minTick` as the full exchange price-increment schedule.
- Zipping `validExchanges` and `marketRuleIds` without validating equal
  length and exact route identity.
- Letting a fake establish authority by fiat.
- Treating UNKNOWN as zero, closed, complete, or safe.
- Reusing cached evidence after a forced fresh read failed.
- Sleeping or holding a network lock on the backend event loop.
- Recording pacing only after a successful request.
- Calling an adapter directly when the manager owns bounds, cache, retry, or
  cancellation.
- Treating an enabled setting as runtime authority.
- Claiming gateway compatibility from fixtures, imports, or a skipped smoke.
- Maintaining a static line-number or site-count map that silently decays.

## Route adjacent work

- repository change/merge protocol: `chronos-change-control`;
- configuration surfaces and coupled flags: `chronos-config-and-flags`;
- exercised-proof doctrine: `chronos-validation-and-qa`;
- submission, lease, and transmit invariants: `chronos-architecture-contract`;
- option-domain policy and sizing: `chronos-wheel-and-options`;
- actual gateway evidence collection: `chronos-real-gateway-campaign`;
- operating an already-approved runtime: `chronos-run-and-operate`;
- historical defect narrative: `chronos-failure-archaeology`.
