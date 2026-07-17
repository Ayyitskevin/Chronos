# TASKS

Working task board for the platform build. Historical wheel-dashboard
milestones (M1–M10) are documented in README.md and docs/architecture.md.

## Done

- [x] Baseline recorded: 951 passed / 1 skipped (credential-gated) on Python 3.12.
- [x] Phase 1 — Pine corpus fetched byte-exact from Notion Master Index: 42/42
      artifacts (00–40 + archived 0A), SHA-256 pinned in
      `research/strategy_registry.yaml` (+ CSV/JSON catalogs). Brief said ~77;
      the authoritative index contains 42 (ASSUMPTIONS A-01).
- [x] Phase 2 — forensic audit of all 42 scripts: `docs/PINE_AUDIT.md` +
      `research/pine_findings.json`. 28 NON_EXECUTABLE_INDICATOR, 13
      PASS_WITH_CONSTRAINTS, 1 REQUIRES_REWRITE (script 08 compile blocker);
      zero repainting/lookahead-contaminated. Duplication analysis in
      `docs/STRATEGY_CATALOG.md` (≈4 genuinely distinct executable systems).
- [x] Phase 3/4 — canonical specs for `regime_trend_v1` and
      `mean_reversion_v1`; deterministic implementations; specification-level
      parity (`docs/PARITY_REPORT.md`; no TradingView exports — A-03).
- [x] Phase 5 — market-data plane, fail-closed quality checks, CSV provider.
- [x] Phase 5/6 — historical data acquired (SPY, QQQ) with provenance manifest
      + validation; chronological research harness; validation with cost/
      slippage stress, sensitivity, baselines; `docs/RESEARCH_REPORT.md` +
      `docs/STRATEGY_SELECTION.md` + `research/selection_manifest.json`.
      **Outcome: zero candidates selected.**
- [x] Phase 7–12 — deny-by-default risk engine, order state machine, execution
      engine, simulated broker + fault injection, IBKR paper adapter,
      reconciliation gate, control plane (modes/halt/promotion), audit log,
      notifications, backtest engine + metrics, research runner, shadow scan,
      CLI.
- [x] Phase 14 — persistent order ledger (SQLite + memory), hash-chained audit
      log, owner-only file permissions.
- [x] Phase 16 — safety/unit/parity/chaos suites: 217 platform tests; full
      suite 1158 passed / 1 skipped (`docs/TEST_PLAN.md`, `docs/TEST_RESULTS.md`).
- [x] Phase 17/18 — CLI, operational docs suite (ADRs 0001–0008, IBKR
      integration/runbook, security, deployment, operations, backup, incident
      response, go-live checklist).
- [x] Independent adversarial review (7 dimensions) +
      remediation of all CRITICAL/HIGH findings:
      `docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md`.
- [x] Tracking docs: ASSUMPTIONS, DECISIONS, RISK_REGISTER, CHANGELOG, HANDOFF.

## Open (owner action or future work)

- [ ] Owner: provide TradingView reference exports to upgrade parity from
      specification-level to TradingView-verified (`fixtures/tradingview/`).
- [ ] Owner: re-run research from IBKR historical data (or another trusted
      feed covering IWM/DIA/GLD/TLT and a longer SPY history). The frozen
      final-test window (2022+) is unconsumed and reserved for this.
- [ ] Future work (out of scope this build): the long-running shadow/paper
      service loop — live bar ingestion, startup reconciliation wiring
      (hydrate `_orders` from the ledger; invoke `reconcile()`), notifications
      daemon. Two accepted MEDIUM review findings (M4 state-level
      reconciliation, M5 restart order hydration) are blocked on this.
- [ ] Owner: if a strategy ever clears the frozen research criteria, author a
      reviewed `config/risk.yaml` and a promotion record before any shadow run.

## Explicitly out of scope for this build

- Canary/live activation of any kind (refused in code; future reviewed release).
- Options execution; short selling; margin; market orders.
- Intraday strategy validation (no trustworthy intraday data here — A-31).
