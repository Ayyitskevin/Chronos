# Chronos

> **AI agents and contributors: start here.** Read the repository contract in
> [AGENTS.md](AGENTS.md) and the canonical
> [Vision Completion Plan](docs/VISION_COMPLETION_PLAN.md)

Chronos is a local-first, model-driven trading system for Interactive Brokers. Its
mission (owner directive, 2026-07-25) is **autonomous trading** across equities and
ETFs, exchange-traded futures, and listed equity and index options: an approved model
originates trading decisions without per-order human approval, inside boundaries the
owner sets at policy time. See
[ADR-0016](docs/adr/ADR-0016-controlled-autonomous-model-authority.md) (D-16, which
superseded ADR-0004 §5 / D-11) and
[ADR-0017](docs/adr/ADR-0017-owner-directed-maximal-autonomy.md) (D-17, the
owner-directed maximal-autonomy supersession of parts of ADR-0016), and
[ADR-0030](docs/adr/ADR-0030-deterministic-option-selection-and-evidence-receipts.md)
(D-34, deterministic option selection and receipts).

**Where that stands today (be precise about this):** the whole autonomy stack is built
and wired — contracts (M1), gateway/admission/sizing (M2), durable state (M3), the
compiler and queue (M4), session counters (M5), alert delivery (M6), the tick runtime
(M7), and the app-plane wiring (M7.5/ADR-0017). A backend booted with a valid
`AUTONOMY_MANDATE_FILE` auto-activates it and drives the autonomy tick; proposals
arriving over the ingress are judged, sized, compiled, and handed to the same
propose → preview → confirm → submit pipeline every human order walks. With **no**
mandate file configured, autonomy is inert and the pipeline is the human-confirmed
one described below.

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

   This deterministic platform starts **halted** on a fresh deployment and refuses every
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

**Autonomy programme (ADR-0016/ADR-0017).** M0 delivered a read-only gap audit; M1 the
governance reset and typed contracts (`AITradeDecision`, `AutonomyMandate`); M2 the
deterministic gateway, admission, and sizing; M3 durable supervisor state; M4 the
compiler, decision queue, and injection tests; M5 market-local session counters; M6
owner-alert delivery; M7 the time-driven tick runtime (events coalesce into hints, never
triggers); and **M7.5 (ADR-0017)** the owner-directed maximal-autonomy supersession: the
persistent auto-activating mandate, model self-sizing under an explicit
`model_discretion` grant, protected (collared, never unbounded) market orders, and the
app-plane wiring that assembles facts, mandate, runtime, and the order-plane handoff in
the backend lifespan; and **M8a (ADR-0018)** the operator terminal — a command registry and
panel read-models in Python (`chronos.terminal`) served as JSON to a build-free browser
client, mounted same-origin at `/terminal/app`. **M8b** added the session cookie that lets that client authenticate (`POST /terminal/session`
exchanges the local API token for an httpOnly cookie scoped to `/terminal`, so the browser
never attaches it to the order plane). **M8c (ADR-0019)** added the chart: `Broker.historical_bars`, a cached and
self-pacing bars plane, and a dependency-free candle panel. **M9** closed R-26: IBKR `liquidHours` now supplies the equity/option session gate, which
had been permanently ambiguous — and therefore permanently blocking — since M5. **M10** closed
R-25: `max_opening_orders_per_day` had never refused an order, because its evidence was never
gathered *and* the repository method that would have supplied it counted SELLs only, hiding
every stock and crypto opening. The cap now counts openings of any side since **market-local**
midnight (a UTC boundary would hand out a second allowance every evening), counts them at
creation rather than at fill, and treats an uncountable day as UNKNOWN → blocked rather than
as a passing zero. **M11** closed the last of the four M0 kernel defects, R-27: option
deliverable verification had exactly one setter — the demo broker, by fiat — so
`standard_deliverable_verified` FAILed every option order against a real gateway and the
option path was unproven outside demo. Both IBKR adapters now screen each qualified option
on five necessary, conjunctive conditions, the strongest being that the OCC root still
equals the symbol (a suffixed root is how OCC marks an adjusted deliverable). All four
kernel defects are now mitigated and **none is closed** — each keeps a disclosed residual,
and per-family live promotion still needs owner verification against a real gateway.
**ADR-0030** now implements the first autonomous option-selection scope:
`OPEN` equity-option cash-secured puts and covered calls resolve through bounded read-only
chain/contract/quote/market-rule/session/deliverable evidence into a deterministic,
hash-chained `SELECTED` or typed `NO_TRADE` receipt. The selector derives a receipt-bound
tick-conforming limit and the existing compiler must reproduce it exactly. The feature is
off by default; no live resolver-promotion artifact is shipped. Both real IBKR adapters
still return non-authoritative deliverable facts, so real IBKR selection remains
`NO_TRADE` until an authoritative schedule source exists. Broader option shapes and the
deferred terminal mandate-authoring/streaming work remain.

No order is placed by any test, CI run, or development path. The one and only `transmit=True`
in the order pipeline lives at the submission boundary and is reachable only after the full
gate chain passes. Live trading has never been exercised from this codebase; any live acceptance
is an owner action through the finished app.

## Safety posture

Autonomy changes **who decides**, not **what gates**. Under ADR-0016 the model gains
trade-time authority inside an owner-authored mandate; the deterministic kernel keeps
unconditional veto authority. Under ADR-0017 the owner directed the envelope itself to
be maximal — a persistent auto-activating mandate, self-sizing under an explicit
`model_discretion` grant, protected market orders — while every execution-correctness
gate below stands unweakened.

Bullets marked **[enforced]** are live controls with code and tests behind them today.
Bullets marked **[contract]** are structural guarantees of the contract types.

- **[enforced] One reachable transmit site.** Exactly one `transmit=True` exists in
  `chronos.orders` (the submission boundary); a structural test enforces it. `chronos.orders`
  stays the single canonical execution plane — **no AI-specific submission path is created**.
  A repository-wide inventory pins every transmit-enabling site across `src/` and `scripts/`,
  matching keyword and attribute spellings and any computed value, and separates the two
  sites that *originate* transmit authority from the five that merely propagate a value some
  originating site already decided. The dormant deterministic-plane paper adapter
  (`chronos/execution/brokers/ibkr_paper.py`) is the second originating site: it is
  **quarantined** (R-28) — constructed nowhere in production, and refusing construction
  outright unless passed an acknowledgement that nothing in `src/` passes.
- **[enforced] The model cannot reach a broker.** `chronos.autonomy` imports nothing from the
  order, broker, execution, risk, api, or persistence planes — asserted by an AST walk and a
  subprocess import probe.
- **[enforced] Opening option identity is selected, never model-authored.** For ADR-0030's
  cash-secured-put and covered-call scope, the model cannot name a right, strike, expiry,
  route, trading class, or `conId`; strategy deterministically derives the request's right.
  Bounded exact-set reads produce one canonical receipt, which is durably committed and
  semantically replayed before handoff. Missing volume/open interest, completion provenance,
  authoritative deliverables, or any other required fact is typed `NO_TRADE`; system-data
  failures alert the owner.
- **[contract] A decision cannot express an order.** `AITradeDecision` carries no account,
  broker, routing, or transmit field anywhere in its nested tree, refuses smuggled fields,
  cannot name a broker order id, and cannot name the mandate it is judged against.
- **[contract] The model cannot authorize itself.** The `AutonomyMandate` is owner-authored,
  frozen (including against `model_copy`), expiring (live ≤ 365 days under ADR-0017), and
  promoted per asset family. The model plane has no tool that writes it, changes policy, or
  arms the system. Scope stays deny-by-default; capital **ceilings** invert to
  owner-optional only under an explicit `model_discretion` grant, and the cash/buying-power
  **floors** are required in every mode — discretion over size is not discretion over the
  reserve (ADR-0017 §2).
- **[enforced] Demo is the default** and needs no brokerage account; paper and live are
  opt-in config. **[enforced]** With no `AUTONOMY_MANDATE_FILE` configured, autonomy is
  inert — no runtime is constructed at all. With one configured, ADR-0017 supersedes the
  old env-var rule: a valid, account-matching mandate file **auto-activates on boot**
  (digest-stamped, so the audit trail records which text granted authority). A revoked
  mandate stays revoked across restart; an invalid or wrong-account file boots inert with
  a CRITICAL alert.
- **[enforced] Live is fail-closed and gated**, never assumed: a ten-check live stack —
  config, connection, reconciliation, data, risk, preview, session arming, per-order
  confirmation, a durable kill switch, and a session-drawdown breaker — each proven to block
  independently.
- **[enforced] Every order is a positive-price limit — including "market" orders.**
  ADR-0017 added `OrderForm.MARKET` to the autonomy vocabulary, but it compiles to a
  **protected** marketable limit (quote ± a 1% collar): market-order fill behavior on any
  ordinary day, a price ceiling on the catastrophic one. It must be granted in the
  mandate's `order_forms` to be selectable, and a literally unbounded venue market order
  remains unexpressible anywhere in the system.
- **[enforced] Cash-secured puts only; no uncovered short options** — enforced today by the
  orders-plane risk engine (`cash_secured_put`, `covered_call_coverage`). **[contract]** The
  autonomy strategy vocabulary additionally cannot express an uncovered short option, so no
  mandate can authorize one.
- **[enforced] An AI failure never becomes permission to trade.** When the model, broker,
  data, clock, database, lease, resolver, risk engine, or reconciliation state is
  unavailable, ambiguous, stale, or inconsistent, `chronos.supervisor.admission` creates no
  new exposure and records the denial with its reason. Risk-*reducing* decisions may still
  proceed — refusing a close because a quote feed went stale would trap the position at
  exactly the wrong moment — unless the degradation is one that leaves position truth
  unknown, in which case nothing proceeds. Each reason declares which kind it is and
  **defaults to the blocking kind**. Facts the wiring cannot gather are never invented;
  a tick without facts refuses to run and alerts the owner.
- **[enforced] The autonomous path is the human path.** An admitted, sized, compiled
  decision is handed to `order_plane_handoff`, which walks the full existing pipeline —
  propose → risk → preview → confirm → submit. Nothing is skipped; a refusal at any
  surface returns to the cycle as a refusal. Autonomy added a gate stack and removed none.
- **[enforced] The terminal shows and asks; it never decides.** `chronos.terminal` imports
  nothing from the broker, order, or execution planes, and its read routes are deliberately
  *not* writer-gated so a demoted backend still shows state rather than going dark. Its two
  owner actions (acknowledge an alert, revoke a mandate) are writer-gated, token-gated, and
  require a typed confirmation; revocation additionally requires a reason and binds to the
  grant the owner was shown. **Unknown renders as unknown, never as a plausible zero** — an
  unset ceiling under `model_discretion` is shown as *no ceiling*, never as `0`, and an
  unobserved drawdown is shown as unknown, never as no drawdown.
- **[enforced] Model narrative can be seen, never act.** Every server string reaches the DOM
  through `textContent`; a structural test reads the shipped client and fails on any HTML or
  eval sink, and a Content-Security-Policy over `/terminal/*` makes the next one inert.
- **[enforced] The terminal's own credential cannot reach the order plane.** The browser
  session cookie (M8b) is scoped to `path=/terminal`, so it is never attached to `/orders/*`
  — asserted at the server, not by trusting the browser: an order route asked with the
  session and no token header still refuses. Sessions live in memory only, so a restart
  signs every terminal out, and signing in grants no writer authority.
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

Autonomous option **evaluation** is a separate default-off capability:
`ENABLE_AUTONOMY_OPTION_SELECTION=true`. That flag creates no live authority. CANARY/LIVE
also require an owner-authored, exact-mode resolver promotion at
`AUTONOMY_OPTION_RESOLVER_PROMOTION_FILE`, and Chronos ships no creator for that artifact.
None has been created by this release. Real IBKR evaluation remains `NO_TRADE` until an
authoritative option-deliverable source is integrated.

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
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src/chronos
.venv/bin/mypy --strict worker
.venv/bin/pytest
.venv/bin/python scripts/verify_release_artifact.py
```

CI runs these six in order. Migration verification (fresh-DB init, historical-schema → head
upgrades, and a no-un-migrated-drift completeness check) runs inside the pytest step. The release
gate builds the current source as a wheel, installs it with hash-locked dependencies in a clean
venv outside the checkout, and verifies package origin, static assets, the complete importable
migration namespace at head, the `chronos` console entry point, and every source-declared
`python -m` entry point. The separately marked IBKR smoke test is skipped by default and remains
strictly read-only.

## Documentation

- [Repository contract for AI agents and contributors](AGENTS.md)
- [Canonical Vision Completion Plan](docs/VISION_COMPLETION_PLAN.md)
- [ADR-0016 — Controlled Autonomous Model Authority](docs/adr/ADR-0016-controlled-autonomous-model-authority.md)
  (the authority model, model isolation, the mandate, the promotion ladder)
- [ADR-0030 — Deterministic Option Selection and Evidence Receipts](docs/adr/ADR-0030-deterministic-option-selection-and-evidence-receipts.md)
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
