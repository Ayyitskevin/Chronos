# TASKS

Working task board for the platform build. Historical wheel-dashboard
milestones (M1–M10) are documented in README.md and docs/architecture.md.

## Done

- [x] Baseline recorded: 951 passed / 1 skipped (credential-gated) on Python 3.12.
- [x] Pine corpus fetched byte-exact from Notion Master Index: 42/42 artifacts
      (00–40 + archived 0A), SHA-256 pinned. Note: brief said ~77; the
      authoritative index contains 42 (ASSUMPTIONS A-01).
- [x] Phase 1 registry: research/strategy_registry.yaml + strategy_catalog.{csv,json}.
- [x] Platform foundation: marketdata, indicators, specs, strategies (2 derived
      + 3 baselines), portfolio, risk engine, execution engine + state machine +
      ledgers, simulated broker with fault injection, IBKR paper adapter,
      reconciliation gate, control plane (modes/halt/promotion), audit log,
      notifications, backtest engine + metrics, research runner, CLI.
- [x] Safety acceptance suite (29 tests) green; full legacy suite green.
- [x] Canonical specs for regime_trend_v1 and mean_reversion_v1.
- [x] ASSUMPTIONS.md, DECISIONS.md, RISK_REGISTER.md, docs/ARCHITECTURE.md,
      docs/RISK_POLICY.md, config/risk.example.yaml.

## In flight

- [ ] Phase 2 forensic audit of all 42 scripts → docs/PINE_AUDIT.md +
      research/pine_findings.json (agent fan-out running).
- [ ] Historical daily OHLCV acquisition with provenance manifest
      (research/data/raw/ + MANIFEST.json) (agent running).
- [ ] Platform unit/parity/chaos test suites (agent running).
- [ ] Operational docs suite: ADRs, IBKR integration/runbook, security,
      deployment, operations, backup, incident response, test plan,
      go-live checklist (agent running).

## Next

- [ ] Phase 6 quantitative validation: chronological partitions, walk-forward,
      cost/slippage stress, baselines, per-symbol results → docs/RESEARCH_REPORT.md,
      docs/STRATEGY_SELECTION.md, research/selection_manifest.json.
- [ ] docs/STRATEGY_CATALOG.md + docs/PARITY_REPORT.md + docs/TEST_RESULTS.md.
- [ ] Independent adversarial review (fresh agents) → docs/INDEPENDENT_REVIEW.md,
      docs/REMEDIATION_REPORT.md; remediate critical/high findings.
- [ ] Final gates (ruff/format/mypy/pytest), CHANGELOG, HANDOFF, push, draft PR.

## Explicitly out of scope for this build

- Canary/live activation of any kind (refused in code; future reviewed release).
- Options execution; short selling; margin; market orders.
- Intraday strategy validation (no trustworthy intraday data here — A-31).
- TradingView-export parity (no exports provided — A-03; owner action).
