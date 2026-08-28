# Test Plan

The test taxonomy as it exists in this repository, what each layer proves, exact commands, and —
just as important — what cannot be tested from this environment. Dated run evidence lives in
docs/TEST_RESULTS.md; counts are snapshots, so rerun `make gates` before citing the current tree.

## Layers

### 1. Application unit and integration suites — `tests/unit/`, `tests/integration/`

The original pre-platform snapshot was 951 passed and one skipped. The directories have since
grown beyond the wheel dashboard: they cover broker adapters, reconciliation, order/API flows,
research, persistence, UI models, installed migrations, and Streamlit rendering. The current
collected counts belong in docs/TEST_RESULTS.md, not in this plan.

```bash
.venv/bin/pytest tests/unit tests/integration -q
```

### 2. Platform safety acceptance suite — `tests/safety/`

`tests/safety/test_safety_invariants.py` — 36 collected cases exercising the real components (no mocks of
the objects under test), mapping to invariants in docs/RISK_POLICY.md. Classes and what they
prove:

- **TestModeLocks** — CANARY_LIVE/LIVE resolve to `DENIED_LIVE_DISABLED` even with perfect paper
  evidence; SHADOW cannot submit; PAPER requires every condition simultaneously (each degraded
  case — empty allowlist, missing/paper-pattern-violating/off-allowlist account id, unverified
  environment, transmission off — yields `NO_ORDERS` with denial reasons); a live-looking `U…`
  account id is denied even if the operator allowlists it.
- **TestHaltPersistence** — missing halt file fails closed (`NEVER_ARMED`); corrupt file fails
  closed (`STATE_CORRUPTION`); a halt survives process restart (fresh store re-reads it); rearm
  with a blank note is rejected and leaves the halt in place.
- **TestRiskEngineDenyByDefault** — an all-zero policy rejects everything (with
  `ZERO_CAPITAL_AUTHORIZED`, `SYMBOL_NOT_ALLOWED`, `STRATEGY_NOT_ALLOWED`); a permissive policy
  approves a clean intent; halt blocks approval; stale quotes block; missing account state
  blocks; the daily-loss limit blocks entries; entries without a stop block; sells without a
  position block (no shorts); a duplicate intent id is rejected on second validation; an internal
  engine exception fails closed (`INTERNAL_ERROR_FAIL_CLOSED`), never approves.
- **TestExecutionGate** — no submission without approval; a forged approval (foreign engine
  token) is refused; an approval for a different intent is refused; halt blocks submission even
  with a valid approval; pending reconciliation blocks submission; the ledger refuses a duplicate
  intent id; a ledger write failure refuses AND halts; a broker event for an unknown intent
  halts (`UNKNOWN_ORDER`).
- **TestStrategyIsolation** — the strategy base module imports no broker code (`ib_async`,
  `chronos.broker`, `chronos.execution.brokers` absent from source); `StrategyProposal` has no
  order-capable fields (no account/quantity/broker/order_id); the risk policy object is frozen
  (mutation raises).

`tests/safety/test_autonomy_contracts.py` — the structural enforcement for ADR-0016 / D-16
(added 2026-07-25). ADR-0004 §5 conceded that "no generative AI in runtime" was verifiable
only *by inspection*; D-16 replaces the prohibition with structure, so the structure is now
tested. Proves: the model plane (`chronos.autonomy`) imports nothing from the
order/broker/execution/risk/api/persistence planes (AST walk **and** subprocess `sys.modules`
probe); `AITradeDecision` carries no account/broker/routing/transmit field anywhere in its
nested model tree and refuses smuggled fields; a decision may not name a broker order id;
exposure-creating decisions must cite evidence and state invalidation conditions;
`AutonomyMandate` is frozen, must expire after it starts, authorizes nothing by default,
cannot exceed its promotion rung, cannot outlive the code-defined live-duration ceiling, must
state its scope explicitly in submitting modes, refuses futures options and known-bad data qualities,
and requires a pseudonymous account scope; no uncovered short option and no `MARKET` order
form are expressible; the startup autonomy mode is not live. Its current structural guard asserts
that only the supervisor and named wiring modules consume the contracts; this replaced the M1
milestone's wired-into-nothing assertion.

`tests/unit/test_option_selection.py`, `tests/unit/test_option_selection_service.py`,
`tests/safety/test_option_selection_cycle.py`, and
`tests/integration/test_option_selection_terminal.py` enforce ADR-0030 / D-34. Together they
prove:

- the model-facing decision cannot express option identity, while the economic request's
  `right` is derived deterministically from strategy;
- bounded chain completion provenance, exact-set acquisition, freshness, identity,
  liquidity, authoritative deliverable, session, market-rule, score, and total-order gates,
  including id-less global pacing interruption, cross-batch qualification prefixes that
  exclude old cache state, and the 256-increment market-rule evidence bound;
- missing volume/open interest always blocks, including with zero numeric floors;
- Decimal/context stability, all input permutations, a golden canonical digest, semantic
  replay after body rehash, and complete rejection-code mutation coverage;
- the selector derives the receipt-bound tick/limit and the existing compiler must reproduce
  the exact contract and price;
- default-off activation, one exact live mode per owner promotion, canonical mandate/policy
  and material-source bindings, post-acquisition replacement/expiry checks, and immediate
  pre-handoff revalidation; runtime code contains no promotion writer;
- commit-before-handoff durability, fresh-session visibility, full-chain and semantic-envelope
  checks, byte-canonical outer envelopes, UTC-equivalent timestamp persistence,
  idempotent exact reuse, conflicting-decision refusal, tamper detection,
  bounded head-link append, and survival of a later crash/rollback; and
- the authenticated bounded GET-only terminal view verifies the full account stream and exact
  receipt semantics without importing or exposing an order mutation surface; the semantic scan
  retains only a bounded receipt page and suppresses oversized, deeply nested,
  invalid-UTF-8, wrong-storage-type, or otherwise corrupt database-derived row
  fields before they cross the driver boundary; and
- missing/conflicting/unknown/source-quality evidence raises the deduplicated system alert,
  while numeric candidate-filter misses do not.

Adapter and lifecycle coverage in `test_callback_bridge.py`, `test_market_data.py`,
`test_ibkr_broker.py`, `test_official_ibkr.py`, and
`test_broker_mutation_inventory.py` additionally pins completion signals, bounded cleanup,
right-specific option ticks, and the unchanged mutation/transmit inventory. All of this is
offline; the tests use fakes and never connect to IBKR or create a live promotion.

```bash
.venv/bin/pytest \
  tests/unit/test_market_data.py \
  tests/unit/test_option_selection.py \
  tests/unit/test_option_selection_service.py \
  tests/safety/test_option_selection_cycle.py \
  tests/integration/test_option_selection_terminal.py -q
```

`tests/safety/test_registry_no_automated_unlock.py` — ADR-0013 §7's holdout-unlock bar,
**retargeted** by D-16: `chronos.autonomy` joins the scanned automated tree so the model
plane cannot reach a research holdout. It is deliberately *not* added to the forbidden-import
list, because M2's deterministic supervisor must import the decision contract to judge it.

```bash
.venv/bin/pytest tests/safety -q
```

### 3. Platform unit / parity / chaos suites — `tests/platform_unit/`, `tests/parity/`, `tests/chaos/`

These are established suites. Their current file inventory is executable state (`rg --files
tests/platform_unit tests/parity tests/chaos`); the durable division of responsibility is:

- `tests/platform_unit/` — per-module deterministic-platform tests, including the state machine,
  ledgers, promotion, simulated and paper-broker behavior, service loop, metrics, and property
  invariants.
- `tests/parity/` — indicator/strategy reference parity and batch-versus-stream equivalence,
  including the Five-Tool path. Honest limit: the base strategy set has no TradingView exports
  (ASSUMPTIONS.md A-03), so those cases verify specifications and references rather than
  TradingView output.
- `tests/chaos/` — deterministic fault injection through backtest, execution, and service-loop
  paths: rejections, partial fills, duplicated events, dropped acknowledgements, and recovery
  behavior. These assert halt/reconciliation behavior rather than trading through ambiguity.

Current shape and dated counts: docs/TEST_RESULTS.md.

```bash
.venv/bin/pytest tests/platform_unit tests/parity tests/chaos -q
```

### 4. Credential-gated IBKR smoke test — `tests/integration/test_ibkr_smoke.py`

Strictly read-only, opt-in, and skipped by default (marker `ibkr`, env gate
`CHRONOS_RUN_IBKR_SMOKE=1`). Requires the OWNER's TWS/IB Gateway running with API enabled — it
cannot run in this build environment or CI. It connects, reads server time, account summary,
qualifies one symbol, reads chain metadata and one quote, cancels the subscription, disconnects.
It calls no order method of any kind (docs/ibkr_setup.md).

```bash
.venv/bin/python scripts/smoke_test_ibkr.py
# equivalent to:
CHRONOS_RUN_IBKR_SMOKE=1 BROKER_MODE=ibkr ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false \
  .venv/bin/pytest -m ibkr tests/integration/test_ibkr_smoke.py
```

## What CANNOT be tested here (owner action required)

- **Real-gateway paper submission.** The paper adapter
  (`src/chronos/execution/brokers/ibkr_paper.py`) is unit-tested against a fake IB object only.
  Actually placing a DAY limit order on a real paper account — and observing real ack/fill/cancel
  events, commission reports, and reconciliation against real `managedAccounts()` — requires
  owner credentials and a running gateway. Until the owner does this under supervision, the
  adapter's behavior against IBKR's live event stream is unproven.
- **Real-gateway reconciliation evidence gathering** (RISK_REGISTER.md R-04 residual).
- **Read-only smoke test** (above) — runnable only by the owner.
- **Real-IBKR autonomous option eligibility.** Both adapters truthfully report
  `authoritative=False` because TWS exposes no OCC deliverable schedule, so the
  expected result is `NO_TRADE`. A future authoritative source and adapter need
  their own offline fixtures plus owner read-only gateway verification before
  this can become a positive integration case.
- **Live option-resolver promotion.** Runtime code has no creator. No artifact is
  shipped or generated by tests; authoring one is a later owner action requiring
  human sign-off after the missing evidence source and gateway verification are
  complete.
- **TradingView parity** — no reference exports exist (A-03).
- **Intraday strategies** — excluded until a certified hourly release exists AND an intraday validation plan (labels, session calendars, half-days) is written. The original premise — no trustworthy intraday data in this environment (A-31) — narrowed 2026-08-21: ADR-0029 built the data path, but a path is not a release and a release is not a plan.

## CI gates

`.github/workflows/ci.yml`, on every push and pull request (Python 3.12, 20-minute timeout, env
pins `BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false`):

```bash
ruff check .
ruff format --check .
mypy src/chronos          # strict mode (pyproject.toml)
mypy --strict worker      # separate worker process/package
pytest -q                 # entire tests/ tree; the IBKR smoke test self-skips
python scripts/verify_release_artifact.py
```

All six must pass. The final command builds and installs the wheel outside the checkout, compares
shipped assets and migrations with source, upgrades a disposable v2 database through head, and
exercises installed entry points. Run the same sequence locally before pushing.

## Full local run

```bash
make gates
```
