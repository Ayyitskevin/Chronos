# Chronos

Chronos is a local-first trading research and decision-support system for Interactive
Brokers, containing two subsystems:

1. **Live Wheel dashboard** (below) — a human-in-the-loop order-management system for the
   options Wheel, extended to stocks and (built, disabled-by-default) spot crypto. Every
   order flows proposal → risk → preview → typed confirmation → a single guarded submission
   boundary. Demo and paper are the defaults; live transmission is a fail-closed *capability*
   gated behind a ten-check stack, session arming, a durable kill switch, a drawdown breaker,
   and a single-writer lease — not a hard-coded block.
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

Chronos is decision-support software. It is not an autonomous trading bot, investment adviser,
performance-prediction engine, or promise of profitable trading. Options and crypto can produce
rapid and substantial losses. Paper fills do not prove live execution quality, and **IBKR paper
accounts do not support crypto at all** — see [docs/limitations.md](docs/limitations.md).

## Current status

The Live Wheel mission is tracked in [docs/LIVE_WHEEL_GAME_PLAN.md](docs/LIVE_WHEEL_GAME_PLAN.md).
Delivered: the backend + single-writer runtime, the official read-only IBKR adapter, the wheel
engine, the dashboard cutover, the paper order-management pipeline (M5), the live safety layer
(M6), live execution capability validated without trading (M7), and the crypto family (M7C, spot,
fractional, allowlist-gated, off by default). Final hardening (chaos, CI migration checks, docs)
is M8.

No order is placed by any test, CI run, or development path. The one and only `transmit=True`
in the order pipeline lives at the submission boundary and is reachable only after the full
gate chain passes. Live trading has never been exercised from this codebase; any live acceptance
is an owner action through the finished app.

## Safety posture

- **One reachable transmit site.** Exactly one `transmit=True` exists in `chronos.orders`
  (the submission boundary); a structural test enforces it. Nothing else in the Live Wheel path
  can send; the dormant autonomous-plane paper adapter has its own separate, never-instantiated
  transmit site behind a halt store that defaults HALTED (see [docs/limitations.md](docs/limitations.md)).
- **Demo is the default** and needs no brokerage account. Paper and live are opt-in config.
- **Live is fail-closed and gated**, never assumed: a ten-check live stack — config, connection,
  reconciliation, data, risk, preview, session arming, per-order typed confirmation, a durable
  kill switch, and a session-drawdown breaker — each proven to block independently.
- **Market orders are impossible by construction** — every order is a positive-price limit.
- **Cash-secured puts only; naked short calls are not config-enableable.**
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
