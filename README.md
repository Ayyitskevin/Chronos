# Chronos

Chronos is a local-first, model-driven trading system for Interactive Brokers. Its
mission (owner directive, 2026-07-25) is **controlled autonomous trading** across
equities and ETFs, exchange-traded futures, and listed equity and index options: after
the owner activates an approved AutonomyMandate, an approved model may originate trading
decisions without per-order human approval, inside boundaries the owner sets at policy
time. See [ADR-0016](docs/adr/ADR-0016-controlled-autonomous-model-authority.md) and
DECISIONS.md **D-16**, which supersede ADR-0004 §5 / D-11.

**Where that stands today (be precise about this):** the governance and the typed
contracts have landed (Milestone 1). The deterministic decision gateway, the model
worker, and every autonomous execution path have **not** — they are Milestones 2 onward.
Nothing in `chronos.autonomy` is wired into any runtime path, and the shipped order
pipeline is still the human-confirmed one described below. Chronos does not trade
autonomously today.

The repository contains two subsystems:

1. **Live Wheel dashboard** (below) — the order-management system for the options Wheel,
   extended to stocks and (built, disabled-by-default) spot crypto, and the canonical live
   execution plane that autonomous decisions will be compiled into. Every order flows
   proposal → risk → preview → confirmation → a single guarded submission boundary. Demo
   and paper are the defaults; live transmission is a fail-closed *capability* gated behind
   a ten-check stack, session arming, a durable kill switch, a drawdown breaker, and a
   single-writer lease — not a hard-coded block.
2. **Deterministic strategy platform** — research, backtesting, replay, shadow, and
   (gated) paper execution built from the owner's Pine Quant Library corpus.
   See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/STRATEGY_CATALOG.md](docs/STRATEGY_CATALOG.md),
   and [docs/RISK_POLICY.md](docs/RISK_POLICY.md). Quick start:

   ```bash
   .venv/bin/python -m chronos.cli status        # mode banner, halt state, audit chain
   .venv/bin/python -m chronos.cli backtest --strategy regime_trend_v1 --symbol SPY
   .venv/bin/python -m chronos.cli research repro produce --help  # deterministic run manifests
   .venv/bin/python -m chronos.cli shadow-scan   # would-be intents; nothing can submit
   .venv/bin/python -m chronos.cli monitor       # read-only platform monitor
   .venv/bin/python -m chronos.service           # supervised shadow service (one cycle)
   ```

   Research-run produce/replay/compare: [docs/RESEARCH_REPRODUCIBILITY.md](docs/RESEARCH_REPRODUCIBILITY.md).

   This autonomous platform starts **halted** on a fresh deployment and refuses every
   live-capable mode in code — it is separate from, and never imported by, the Live Wheel
   order pipeline in subsystem 1.

Chronos is being built toward autonomous operation, but it is not an investment adviser,
a performance-prediction engine, or a promise of profitable trading. Trading equities,
futures, and options can produce rapid and substantial losses, and an autonomous system
can produce them without waiting for you. Paper fills do not prove live execution
quality, and **IBKR paper accounts do not support crypto at all** — see
[docs/limitations.md](docs/limitations.md).

## Current status

The Live Wheel mission is tracked in [docs/LIVE_WHEEL_GAME_PLAN.md](docs/LIVE_WHEEL_GAME_PLAN.md).
Delivered: the backend + single-writer runtime, the official read-only IBKR adapter, the wheel
engine, the dashboard cutover, the paper order-management pipeline (M5), the live safety layer
(M6), live execution capability validated without trading (M7), and the crypto family (M7C, spot,
fractional, allowlist-gated, off by default). Final hardening (chaos, CI migration checks, docs)
is M8.

**Autonomy programme (ADR-0016).** M0 delivered a read-only autonomy gap audit. **M1 (this
milestone)** delivered the governance reset and the typed contracts —
`chronos.autonomy.AITradeDecision` and `chronos.autonomy.AutonomyMandate` — and **added no
broker behavior**; a test asserts nothing outside the package imports them. Still to come:
M2 the deterministic decision gateway and mandate validation, M3 the persistent brain, M4
the agent and tool layer, M5 the terminal and scheduler, and M6-M10 the per-family
promotions from paper to capped live. Each milestone stops for owner approval.

No order is placed by any test, CI run, or development path. The one and only `transmit=True`
in the order pipeline lives at the submission boundary and is reachable only after the full
gate chain passes. Live trading has never been exercised from this codebase; any live acceptance
is an owner action through the finished app.

## Safety posture

Autonomy changes **who decides**, not **what gates**. Under ADR-0016 the model gains
trade-time authority inside an owner-authored mandate; the deterministic kernel keeps
unconditional veto authority.

Bullets marked **[enforced]** are live controls with code and tests behind them today.
Bullets marked **[contract]** are guarantees of the M1 contract types, which are wired
into nothing yet. Bullets marked **[M2+]** are requirements ADR-0016 places on machinery
that is **not yet built**. Nothing below is a claim that Chronos trades autonomously now.

- **[enforced] One reachable transmit site.** Exactly one `transmit=True` exists in
  `chronos.orders` (the submission boundary); a structural test enforces it. `chronos.orders`
  stays the single canonical execution plane — **no AI-specific submission path is created**.
  The dormant deterministic-plane paper adapter (`chronos/execution/brokers/ibkr_paper.py:120`)
  has its own separate transmit site that this test does **not** scan; it is constructed
  nowhere in production, and retiring or quarantining it is M2 work tracked as
  RISK_REGISTER R-28.
- **[enforced] The model cannot reach a broker.** `chronos.autonomy` imports nothing from the
  order, broker, execution, risk, api, or persistence planes — asserted by an AST walk and a
  subprocess import probe.
- **[contract] A decision cannot express an order.** `AITradeDecision` carries no account,
  broker, routing, or transmit field anywhere in its nested tree, refuses smuggled fields,
  cannot name a broker order id, and cannot name the mandate it is judged against.
- **[contract] The model cannot authorize itself.** The `AutonomyMandate` is owner-authored,
  frozen (including against `model_copy`), expiring (live ≤ 30 days), deny-by-default, and
  promoted per asset family. The model plane has no tool that writes it, changes policy, or
  arms the system — and no such tool exists yet, because the tool layer is M4.
- **[enforced] Demo is the default** and needs no brokerage account; paper and live are
  opt-in config. **[contract]** The autonomy vocabulary's default mode constant is `SHADOW`;
  there is no autonomy startup path yet to read it, and ADR-0016 requires that when one is
  built, an environment variable alone can never activate live autonomous trading.
- **[enforced] Live is fail-closed and gated**, never assumed: a ten-check live stack —
  config, connection, reconciliation, data, risk, preview, session arming, per-order
  confirmation, a durable kill switch, and a session-drawdown breaker — each proven to block
  independently.
- **[enforced] Market orders are impossible by construction** — every order is a
  positive-price limit. **[contract]** The autonomy vocabulary has no `MARKET` order form.
- **[enforced] Cash-secured puts only; no uncovered short options** — enforced today by the
  orders-plane risk engine (`cash_secured_put`, `covered_call_coverage`). **[contract]** The
  autonomy strategy vocabulary additionally cannot express an uncovered short option, so no
  mandate can authorize one.
- **[M2+] An AI failure must never become permission to trade.** ADR-0016 §8 requires that
  when the model, broker, data, clock, database, lease, resolver, risk engine, or
  reconciliation state is unavailable, ambiguous, stale, or inconsistent, the system create
  no new exposure, permit only deterministic risk-reducing behavior, record the denial, and
  alert the owner. This is a requirement on the M2 gateway, not a control that exists today.
- Chronos never asks for or stores an IBKR username or password.
- Missing broker data is represented as missing; it is never fabricated.
- **Crypto is disabled by default** (empty `CRYPTO_ALLOWLIST`); long-only spot, fractional,
  same gated boundary.

See [docs/safety.md](docs/safety.md) for the threat model and [docs/live_trading_runbook.md](docs/live_trading_runbook.md)
for the live gates, arming, kill switch, and per-family operational notes.

## Setup

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python scripts/initialize_database.py
```

Do not commit `.env`; it is ignored.

**Demo (default, no account).** The defaults launch deterministic demo mode:

```bash
.venv/bin/streamlit run src/chronos/app.py    # or: .venv/bin/python scripts/run_demo.py
```

`DEMO_PROFILE=safety_cases` (default) shows a deliberately conflicted portfolio; set
`DEMO_PROFILE=empty_account` in `.env` to walk the full candidate → risk → what-if →
approval-rehearsal path. Demo can never submit.

**Paper.** Point `.env` at a paper TWS/Gateway (`BROKER_MODE=ibkr`, `IB_ENVIRONMENT=paper`,
`IB_ACCOUNT_ID`/`IB_ACCOUNT_ALLOWLIST`); `ALLOW_ORDER_TRANSMIT=true` enables the paper submission
boundary. Paper trades options and stocks — **not crypto** (IBKR paper has no crypto).

**Live.** Live transmission requires the strict conjunction documented in
[docs/live_trading_runbook.md](docs/live_trading_runbook.md) (official adapter + LIVE + a
U-pattern account on a non-empty allowlist + the transmit switch + arming/typed-confirmation
flags), then per-session arming and a disengaged kill switch. Enabling crypto additionally
requires a non-empty `CRYPTO_ALLOWLIST` on a live account. Read the runbook before going live.

See [docs/ibkr_setup.md](docs/ibkr_setup.md) for TWS/Gateway configuration and the opt-in smoke test.

## Wheel state model

Broker positions, open orders, executions, and the Chronos ledger reconcile into one of:
`FLAT`, `SHORT_PUT_PENDING`, `SHORT_PUT_OPEN`, `LONG_STOCK`, `SHORT_CALL_PENDING`,
`SHORT_CALL_OPEN`, `CLOSING`, or `MANUAL_REVIEW`. UI selections never own strategy state.

`MANUAL_REVIEW` is the safe outcome when partial assignment, a corporate action, a manual
trade, or an unexplained mismatch makes the state ambiguous.

## Development gates

```bash
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/chronos
```

CI runs these four in order. Migration verification (fresh-DB init, v2/v3 → head upgrades,
and a no-un-migrated-drift completeness check) runs inside the pytest step. The separately
marked IBKR smoke test is skipped by default and remains strictly read-only.

## Documentation

- [ADR-0016 — Controlled Autonomous Model Authority](docs/adr/ADR-0016-controlled-autonomous-model-authority.md)
  (the authority model, model isolation, the mandate, the promotion ladder)
- [Live Wheel game plan & status](docs/LIVE_WHEEL_GAME_PLAN.md)
- [Live trading runbook](docs/live_trading_runbook.md) (gates, arming, kill switch, per-family notes)
- [Limitations](docs/limitations.md)
- [Architecture](docs/architecture.md)
- [Safety model](docs/safety.md)
- [Financial formulas](docs/formulas.md)
- [IBKR setup](docs/ibkr_setup.md)

## Limitations

Chronos is pre-release software. The complete, current list of honest limitations —
IBKR-paper-has-no-crypto, the official `ibapi` adapter's owner-gateway verification seam,
reconciliation scope, the crypto validation path, and more — lives in
[docs/limitations.md](docs/limitations.md).
