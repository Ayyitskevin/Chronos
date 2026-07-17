# Chronos — Handoff

## Current state

- **Branch:** `claude/chronos-trading-system-rrzroq` into
  `feat/wheel-dashboard-mvp`. Earlier continuation PRs (#1 initial build,
  #2 M2–M4, #3 M1 research re-run) are merged; this PR carries the M5
  adversarial review + remediation and this handoff refresh.
- **Operating capability:** RESEARCH / BACKTEST / SHADOW. The platform starts
  **HALTED** on any fresh deployment (fail-closed) and every live-capable
  mode is refused in code. SHADOW is structurally `NO_ORDERS`.
- **Tests:** 1255 passed, 1 credential-gated skip. `ruff`, `ruff format`,
  and `mypy --strict` clean. CI installs from a hash-verified lockfile.

## What was built (cumulative)

A deterministic research/backtest/shadow trading platform alongside the
existing wheel-strategy dashboard (unchanged). Full architecture in
`docs/ARCHITECTURE.md`. Highlights:

- **Corpus:** all 42 Pine scripts from the Notion Master Index fetched
  byte-exact, hash-pinned, and forensically audited
  (`docs/STRATEGY_CATALOG.md`, `docs/PINE_AUDIT.md`). The brief's "~77" does
  not match the authoritative index of 42 (ASSUMPTIONS A-01).
- **Two derived strategies** (`regime_trend_v1`, `mean_reversion_v1`) with
  schema-validated canonical specs; specification-level parity only (no
  TradingView exports exist — `docs/PARITY_REPORT.md`).
- **Safety architecture:** deny-by-default risk engine, broker-evidence order
  state machine, persistent fail-closed halt, multi-condition mode locks
  (live hard-refused), reconciliation gate with no auto-flatten, hash-chained
  audit log, owner-only state files, simulated broker + IBKR paper adapter.
- **Service loop (M2):** a supervised, deterministic shadow/paper daemon on
  injectable ports — ordered startup (halt → hydrate → evidence → reconcile →
  arm), state-level reconciliation (closed R-22), restart order hydration
  (closed R-23), fail-closed exit codes. Testable with zero network access.
- **Monitoring plane (M3):** read-only view over persisted state (halt,
  reconciliation, audit chain, data freshness, active limits, ledger orders/
  positions/fills) as a CLI command and a localhost Streamlit page. Imports
  no broker adapter — enforced by an AST test *and* a transitive
  `sys.modules` probe. P&L is deliberately not fabricated.
- **Hardening (M4):** coverage for the previously-untested CLI/runner/shadow
  modules; property-based (hypothesis) tests for intent identity,
  state-machine legality, sizer bounds, and risk deny-monotonicity; a
  hash-verified dependency lockfile (`requirements-dev.lock`) that CI and the
  documented deploy path install from (closed R-15).
- **Two independent adversarial reviews:** round 1
  (`docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md`) and round 2
  after M1–M4 (`docs/INDEPENDENT_REVIEW_M5.md`) — seven fresh dimensions,
  all findings remediated or explicitly accepted with rationale. Round 2
  confirmed the live-trading seams hold, the data provenance is genuine, and
  every reported research number reproduces from the raw results.

## Headline research result

**Zero strategies selected for promotion — unchanged after broadening the
universe from 2 to 5 symbols** (SPY, QQQ, IWM, GLD, TLT; DIA unobtainable).
The binding failure is structural: no candidate reaches the frozen ≥20
closed-trade floor on any symbol (max 18). Criteria were frozen before
results existed, re-frozen unchanged before the new symbols were computed,
and applied as written. Two honest re-test hypotheses surfaced
(`regime_trend_v1` on liquid equity indices; `mean_reversion_v1` on
small-caps) — hypotheses, not results. Full evidence and caveats in
`docs/RESEARCH_REPORT.md` and `docs/STRATEGY_SELECTION.md`.

**Material disclosure:** the M1 re-run accidentally consumed QQQ's reserved
final-test window (2022–2024) by running the harness's then-default `all`
stage. The numbers are disclosed in the report's C6 section; they did not
influence selection (rejection is decided by the validation window), but a
future "run once, blind" final test on QQQ is no longer possible. The
harness now requires an explicit `--stage final` to touch a holdout.

## Precise status label

**Research prototype / backtest-executed / shadow-capable engineering.**
NOT shadow-validated (no strategy qualifies to enter a shadow evaluation),
NOT paper-eligible, NOT live-eligible. No strategy has demonstrated a
defensible edge on the available data.

## Known blockers and remaining risks

- **Data:** the broadened universe is heterogeneous — SPY/QQQ unadjusted &
  byte-exact; IWM/TLT dividend-adjusted, GLD nominal, all three transcribed
  (2-decimal) and 2019–2021 only; DIA absent. A uniform, full-history,
  trusted feed (ideally IBKR) is the single highest-value research input
  (RISK_REGISTER R-08).
- **QQQ holdout consumed** (see disclosure above); reserve fresh data for any
  re-test.
- **No TradingView parity fixtures** (owner-gated; R-07 / A-03).
- **IBKR paper adapter has never touched a real gateway** (no credentials in
  this environment); the read-only smoke test is an owner action.
- **Position-quantity reconciliation** compares symbol membership only; must
  be hardened to signed-share comparison before any PAPER wiring supplies
  real positions (accepted M5 finding #13).
- **Supply-chain residual:** the PEP 517 build backend and pip itself sit
  outside the lockfile's hash gate (documented in `docs/SECURITY.md`).

## Owner action items

1. Provide a trusted historical dataset (IBKR export preferred): uniform
   adjustment, 2000–present, 6–10 liquid ETFs. Re-run research; reserve a
   fresh final window.
2. Optionally provide TradingView strategy-tester exports for true parity
   (`fixtures/tradingview/README.md`).
3. Run TWS/IB Gateway locally and execute the read-only smoke test
   (`CHRONOS_RUN_IBKR_SMOKE=1`) — the first time this code touches a real
   gateway.
4. Decide whether short selling should ever be enabled (BEAR+ translation is
   parked behind this decision).
5. Decide whether a ~USD 3,000 cash account should pursue automated trading
   at all given the cost economics documented in RESEARCH_REPORT.

## Safety posture (unchanged, restated)

Live mode: impossible in this build. Live allowlist: not representable. Live
capital authorization: zero. A fresh deployment starts halted. No command,
flag, or environment variable enables live trading. Verified adversarially
twice; the M5 review could not construct a live-order path, an approval
forgery, a policy mutation, or a SHADOW submission.
