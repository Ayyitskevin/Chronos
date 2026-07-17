# CHANGELOG

## [Unreleased] — deterministic strategy platform

### Added
- Pine corpus ingestion: 42 scripts fetched byte-exact from the Notion
  "Pine Quant Library — Master Index" into `research/pine/`, SHA-256 pinned in
  `research/strategy_registry.yaml` (+ CSV/JSON catalogs) via
  `scripts/build_strategy_registry.py`.
- Platform packages under `src/chronos/`: `marketdata`, `indicators`, `specs`,
  `strategies`, `portfolio`, `risk`, `execution` (engine, state machine,
  ledgers, reconciliation, simulated broker, IBKR paper adapter), `control`
  (modes, halt, promotion), `auditlog`, `notifications`, `backtest`,
  `research`, `cli`.
- Derived strategy implementations with canonical YAML specs:
  `regime_trend_v1` (core of Pine 01 BULL+ v1.1), `mean_reversion_v1`
  (executable derivation of Pine 11 MR Extremes Study v1.1); baselines
  (buy-and-hold, SMA 50/200, deterministic random entries).
- Safety acceptance test suite (`tests/safety/`) covering mode locks, halt
  persistence, deny-by-default risk, execution gating, and strategy isolation.
- Deny-by-default risk policy schema + `config/risk.example.yaml`.
- Complete documentation set: `docs/ARCHITECTURE.md`, `docs/RISK_POLICY.md`,
  `docs/STRATEGY_CATALOG.md`, `docs/PINE_AUDIT.md`, `docs/PARITY_REPORT.md`,
  `docs/RESEARCH_REPORT.md`, `docs/STRATEGY_SELECTION.md`, `docs/TEST_PLAN.md`,
  `docs/TEST_RESULTS.md`, `docs/SECURITY.md`, `docs/DEPLOYMENT.md`,
  `docs/OPERATIONS.md`, `docs/BACKUP_AND_RECOVERY.md`,
  `docs/INCIDENT_RESPONSE.md`, `docs/IBKR_INTEGRATION.md`,
  `docs/IBKR_RUNBOOK.md`, `docs/GO_LIVE_CHECKLIST.md`, ADRs 0001–0008,
  `docs/INDEPENDENT_REVIEW.md`, `docs/REMEDIATION_REPORT.md`; plus
  `ASSUMPTIONS.md`, `DECISIONS.md`, `RISK_REGISTER.md`, `TASKS.md`, `HANDOFF.md`.
- Independent adversarial review across seven dimensions with all
  CRITICAL/HIGH findings remediated (see REMEDIATION_REPORT).
- Owner-only (0600) permissions on platform ledger/halt/audit files;
  halt-write fsync durability; collision-resistant order-intent ids;
  deny-by-default for unrecognized trading modes.
- Dependencies: `pyyaml` (+ `types-PyYAML` dev).

### Changed
- `.gitignore`: runtime state files under `data/` (json/jsonl/tmp) ignored.
- `pyproject.toml`: dependency additions only; existing wheel-dashboard code
  untouched.

### Safety posture (unchanged and extended)
- Wheel dashboard: live-money transmission remains hard-disabled; IBKR order
  methods still raise unconditionally.
- Platform: live-capable modes resolve to a hard-denied capability; paper
  submission requires six simultaneous independently-verified conditions; a
  new deployment starts halted until first operator rearm.
