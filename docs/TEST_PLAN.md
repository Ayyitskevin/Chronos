# Test Plan

The test taxonomy as it exists in this repository, what each layer proves, exact commands, and —
just as important — what cannot be tested from this environment. Final counts and run evidence
land in docs/TEST_RESULTS.md (being produced separately; see TASKS.md).

## Layers

### 1. Existing wheel-dashboard suite — `tests/unit/`, `tests/integration/`

The pre-platform baseline: 951 passed, 1 skipped (the credential-gated IBKR smoke test) on Python
3.12 (TASKS.md). Covers the wheel dashboard's broker adapters, reconciliation, candidate/risk/
what-if/approval boundaries, persistence, and UI models. This suite must stay green; the platform
was added without modifying it (ADR-0001).

```bash
.venv/bin/pytest tests/unit tests/integration -q
```

### 2. Platform safety acceptance suite — `tests/safety/`

`tests/safety/test_safety_invariants.py` — 29 tests exercising the real components (no mocks of
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
cannot exceed its promotion rung, cannot outlive the 30-day live ceiling, must state its
scope explicitly in submitting modes, refuses futures options and known-bad data qualities,
and requires a pseudonymous account scope; no uncovered short option and no `MARKET` order
form are expressible; the startup autonomy mode is not live. It also asserts that **M1 wired
the contracts into no runtime path** — a milestone guard that M2's gateway tests replace.

`tests/safety/test_registry_no_automated_unlock.py` — ADR-0013 §7's holdout-unlock bar,
**retargeted** by D-16: `chronos.autonomy` joins the scanned automated tree so the model
plane cannot reach a research holdout. It is deliberately *not* added to the forbidden-import
list, because M2's deterministic supervisor must import the decision contract to judge it.

```bash
.venv/bin/pytest tests/safety -q
```

### 3. Platform unit / parity / chaos suites — `tests/platform_unit/`, `tests/parity/`, `tests/chaos/`

Being authored in parallel by another workstream; files are landing while this plan is written.
Coverage:

- `tests/platform_unit/` — per-module platform tests. Current files: `test_auditlog.py`,
  `test_ledgers.py`, `test_metrics.py`, `test_notifier.py`, `test_portfolio_sizer.py`,
  `test_promotion.py`, `test_quality_and_csv.py`, `test_sim_broker.py`, `test_specs.py`,
  `test_state_machine.py`. Intended scope also includes IBKR paper adapter behavior against a
  fake `IBLike` object (construction gates, account verification, order shape, event
  translation).
- `tests/parity/` — indicator/strategy parity: `test_indicator_reference.py` (Pine-semantics
  indicator values against references), `test_incremental_vs_batch.py` (incremental computation
  equals batch recomputation). Honest limit: no TradingView exports exist (ASSUMPTIONS.md A-03),
  so parity is verified against specifications and references, never against TradingView output.
- `tests/chaos/` — fault injection through the simulated broker's `FaultPlan`
  (`src/chronos/execution/brokers/simulated.py`): `test_execution_faults.py`,
  `test_backtest_faults.py` — rejections, partial fills, duplicated events, dropped acks —
  asserting the engine halts/reconciles rather than trading through ambiguity, and that
  backtests remain deterministic under identical fault plans.

Final shape and counts: docs/TEST_RESULTS.md once that work lands.

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
- **TradingView parity** — no reference exports exist (A-03).
- **Intraday strategies** — excluded until a certified hourly release exists AND an intraday validation plan (labels, session calendars, half-days) is written. The original premise — no trustworthy intraday data in this environment (A-31) — narrowed 2026-08-21: ADR-0029 built the data path, but a path is not a release and a release is not a plan.

## CI gates

`.github/workflows/ci.yml`, on every push and pull request (Python 3.12, 10-minute timeout, env
pins `BROKER_MODE=demo`, `ALLOW_ORDER_TRANSMIT=false`, `ALLOW_LIVE_TRADING=false`):

```bash
ruff check .
ruff format --check .
mypy src/chronos          # strict mode (pyproject.toml)
pytest -q                 # entire tests/ tree; the IBKR smoke test self-skips
```

All four must pass. Run the same four locally before pushing.

## Full local run

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . \
  && .venv/bin/mypy src/chronos && .venv/bin/pytest -q
```
