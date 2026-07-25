# Chronos — Handoff

> **This document describes the deterministic strategy platform as of 2026-07-17 and is
> partly stale.** Since it was written: Milestones 5-7C delivered the gated paper and live
> order pipeline in `chronos.orders` (ADR-0009, ADR-0010), and ADR-0016/D-16 (2026-07-25)
> re-scoped Chronos as a controlled-autonomous, model-driven system. For current state read
> README.md, [ADR-0016](docs/adr/ADR-0016-controlled-autonomous-model-authority.md),
> [docs/safety.md](docs/safety.md), and [docs/limitations.md](docs/limitations.md).

## Current state

- **Branch:** autonomy work is on `claude/chronos-autonomous-governance-jhgfat` into
  `feat/wheel-dashboard-mvp`. (Historical: the platform work landed from
  `claude/chronos-trading-system-rrzroq`; PRs #1 initial build, #2 M2–M4, #3 M1 research
  re-run are merged.)
- **Operating capability (deterministic platform):** RESEARCH / BACKTEST / SHADOW. It starts
  **HALTED** on any fresh deployment (fail-closed) and every live-capable mode is refused in
  code — ADR-0016 does not change this. SHADOW is structurally `NO_ORDERS`.
- **Operating capability (orders plane):** DEMO by default; paper and live are gated
  capabilities (ADR-0009). Autonomous operation is **not** yet implemented — M1 delivered
  the governance and the `chronos.autonomy` contracts only, wired into nothing.
- **Tests:** 1885 passed, 1 credential-gated skip (2026-07-25). `ruff`, `ruff format`, and
  `mypy --strict` clean. CI installs from a hash-verified lockfile.

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

## Safety posture (restated 2026-07-25)

**For the deterministic strategy platform, unchanged:** live mode impossible, live allowlist
not representable, live capital authorization zero, fresh deployment starts halted, no
command/flag/environment variable enables live trading. Verified adversarially twice; the M5
review could not construct a live-order path, an approval forgery, a policy mutation, or a
SHADOW submission. ADR-0016 leaves all of this intact.

**Repository-wide, corrected:** live transmission *is* possible in the `chronos.orders` plane
behind ADR-0009's configuration conjunction plus the ten-gate stack, arming, the durable kill
switch, the drawdown breaker, and the writer lease. Autonomous operation additionally requires
an owner-authored AutonomyMandate; under ADR-0017 it is a persistent file that auto-activates
on boot (revocation survives restart; invalid/wrong-account files boot inert). No test, CI run, or
development path can transmit an order. The M0 audit recorded four kernel defects that
unattended operation makes more dangerous (RISK_REGISTER R-24…R-27). After M2: **R-24 is
MITIGATED with a live residual** — the lease is renewed on a heartbeat and re-checked in
the database immediately before transmit, but IBKR accepts an order without knowing about
our lease, so broker-side fencing remains unavailable. **R-25, R-26 and R-27 remain open**,
and each must be closed before the asset family it governs is promoted. The dormant second
submission path (R-28) is **quarantined**, not retired.

**What M2 built, and what it did not.** `chronos.supervisor` admits or refuses a decision
and independently derives its size; both are tested and both have been through an
adversarial review whose confirmed findings are remediated. What does **not** exist is the
step between the gateway and the order plane: nothing converts an admitted, sized decision
into a `WheelOrderIntent`, because deterministic contract resolution, qualification and
order-form selection are M4. Until that lands, the gateway is a gate with nothing routed
through it — so no part of the system trades autonomously today.
