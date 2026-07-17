# Chronos — Handoff

## Current state

- **Branch:** `claude/chronos-trading-system-rrzroq` (draft PR #1 into
  `feat/wheel-dashboard-mvp`).
- **Operating mode:** `RESEARCH` / `BACKTEST` only. The platform starts
  **HALTED** on any fresh deployment (fail-closed) and every live-capable
  mode is refused in code.
- **Tests:** 1158 passed, 1 credential-gated skip. `ruff`, `ruff format`,
  and `mypy --strict` clean.

## What was built

A deterministic research/backtest/shadow trading platform added alongside the
existing wheel-strategy dashboard (which is unchanged). Full architecture in
`docs/ARCHITECTURE.md`. Highlights:

- **Corpus:** all 42 Pine scripts from the Notion Master Index fetched
  byte-exact, hash-pinned, and forensically audited
  (`docs/STRATEGY_CATALOG.md`, `docs/PINE_AUDIT.md`,
  `research/pine_findings.json`). The brief's "~77" does not match the
  authoritative index of 42 (ASSUMPTIONS A-01). ≈4 genuinely distinct
  executable systems; 28 of 42 are non-executable indicators/studies by
  design.
- **Two derived strategies** (`regime_trend_v1`, `mean_reversion_v1`) with
  schema-validated canonical specs; specification-level parity only (no
  TradingView exports exist — `docs/PARITY_REPORT.md`).
- **Safety architecture:** deny-by-default risk engine, broker-evidence order
  state machine, persistent fail-closed halt, multi-condition mode locks
  (live hard-refused), reconciliation gate with no auto-flatten, hash-chained
  audit log, owner-only state files, simulated broker + IBKR paper adapter.
- **Independent review:** seven adversarial dimensions; all CRITICAL/HIGH
  findings fixed with regression tests (`docs/INDEPENDENT_REVIEW.md`,
  `docs/REMEDIATION_REPORT.md`).

## Headline research result

**Zero strategies selected for promotion.** `mean_reversion_v1` is
net-negative on both available symbols; `regime_trend_v1` shows a
plausible-but-unproven regime-gated profile on QQQ (PF 2.64, low drawdown,
cost- and parameter-robust) but fails the frozen ≥20-trade sample floor, on
one symbol, over one favorable window. Criteria were frozen before results
existed and applied as written. Full evidence and honest caveats in
`docs/RESEARCH_REPORT.md` and `docs/STRATEGY_SELECTION.md`. This is the
correct, evidence-based outcome — not a failure of the platform.

## Precise status label

**Research prototype / backtest-executed.** NOT shadow-validated, NOT
paper-eligible, NOT live-eligible. No strategy has demonstrated a defensible
edge on the available data.

## Known blockers and remaining risks

- **Data breadth:** only SPY and QQQ could be trustworthily acquired here;
  IWM/DIA/GLD/TLT and a longer SPY history need a trusted feed (ideally IBKR).
  Research conclusions carry this caveat (RISK_REGISTER R-08).
- **No TradingView parity:** parity is specification-level (RISK_REGISTER R-07,
  ASSUMPTIONS A-03).
- **Service loop not built:** the long-running shadow/paper daemon (live bar
  ingestion, startup reconciliation wiring, notifications) is out of scope.
  Two accepted MEDIUM review findings (R-22 state-level reconciliation, R-23
  restart order hydration) are blocked on it and are go-live prerequisites.
- **IBKR paper adapter never touched a real gateway** (no credentials here);
  unit-tested against a fake IB object only.

## Recommended next milestone

Re-run the research harness (unchanged) against a trusted, broader,
dividend-adjusted daily dataset. The frozen final-test window (2022+) is
unconsumed and reserved for exactly this. If `regime_trend_v1` then clears
all frozen criteria including a ≥20-trade sample on ≥2 symbols, proceed to
the shadow gate in `docs/GO_LIVE_CHECKLIST.md` — which first requires
building the service loop and closing R-22/R-23.

## Files requiring owner review

- `docs/RESEARCH_REPORT.md`, `docs/STRATEGY_SELECTION.md`,
  `research/selection_manifest.json` — the research conclusion and its
  frozen criteria.
- `docs/GO_LIVE_CHECKLIST.md` — gate-by-gate status.
- `docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md` — what was found
  and fixed.
- `ASSUMPTIONS.md` — every conservative assumption made without owner input.
- `config/risk.example.yaml` — deny-everything template; a real
  `config/risk.yaml` is the owner's to author, and only matters if a strategy
  ever clears research.

## Owner action items

1. Provide a trusted historical dataset (IBKR export preferred) covering more
   ETFs and longer history; re-run research.
2. Optionally provide TradingView strategy-tester exports for true parity
   (`fixtures/tradingview/README.md`).
3. Run TWS/IB Gateway locally and execute the read-only smoke test
   (`CHRONOS_RUN_IBKR_SMOKE=1`) — the first time this code touches a real
   gateway.
4. Decide whether a ~USD 3,000 cash account should pursue automated trading
   at all given the cost economics documented in RESEARCH_REPORT.

## Safety posture (unchanged, restated)

Live mode: impossible in this build. Live allowlist: not representable. Live
capital authorization: zero. A fresh deployment starts halted. No command,
flag, or environment variable enables live trading.
