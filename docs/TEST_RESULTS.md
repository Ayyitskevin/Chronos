# Test Results

Companion to [TEST_PLAN.md](TEST_PLAN.md). All commands run from the repo
root on Python 3.12 (`.venv`). Statuses use the plan's vocabulary:
PASSED / FAILED / SKIPPED / NOT RUNNABLE WITHOUT CREDENTIALS /
NOT IMPLEMENTED / REQUIRES OWNER ACTION.

## Summary (this branch, local run)

| Command | Result |
|---|---|
| `.venv/bin/pytest -q` | **1115 passed, 1 skipped** (~13 s) |
| `.venv/bin/ruff check .` | clean |
| `.venv/bin/ruff format --check .` | clean (146 files) |
| `.venv/bin/mypy src/chronos` | clean, strict (89 source files) |
| GitHub Actions `quality` job | failed on an intermediate WIP commit (lint of in-progress test files); green expected from commit `d16d863` onward — verify on the PR |

## Breakdown

| Suite | Count | Status | Notes |
|---|---|---|---|
| Wheel dashboard `tests/unit` + `tests/integration` | 951 | PASSED | untouched baseline preserved |
| `tests/integration/test_ibkr_smoke.py` | 1 | SKIPPED / NOT RUNNABLE WITHOUT CREDENTIALS | opt-in via `CHRONOS_RUN_IBKR_SMOKE=1` + running TWS/Gateway; strictly read-only |
| `tests/safety` (platform safety acceptance) | 29 | PASSED | mode locks (live denial under maximal config; six-condition paper lock), halt persistence/fail-closed, deny-by-default risk engine, execution gating (forged/foreign/mismatched approvals, halt, reconciliation, duplicates, ledger failure, unknown events), strategy isolation |
| `tests/platform_unit` | 99 | PASSED | state machine, sim broker, ledgers (incl. SQLite reopen + schema tamper), sizer, data quality/CSV, audit-log tamper evidence, promotion gates, notifier isolation, specs schema, metrics hand-checks |
| `tests/parity` | 27 | PASSED | hand-computed indicator references; incremental-vs-batch equality at 1e-9; continuity-reset equivalence. **Specification-level parity only — no TradingView fixtures exist (docs/PARITY_REPORT.md)** |
| `tests/chaos` | 9 | PASSED | rejection/partial/duplicate/drop-ack/rogue-event/ledger-failure paths; fault-run determinism |

## Safety-invariant coverage map (brief → test)

| Invariant | Evidence |
|---|---|
| Live mode disabled by default (and at all) | `test_live_modes_are_hard_denied`; promotion tests append failing hard-disabled gate |
| Live capital authorization zero by default | `RiskPolicy` defaults + `test_all_zero_policy_rejects_everything` |
| Empty allowlist blocks submission | `test_paper_requires_every_condition` (empty-allowlist case) |
| Paper cannot reach a live account | account pattern + allowlist + environment cases in the same test; adapter re-verifies managed accounts (`ibkr_paper.verify_account`) |
| Strategies cannot call the broker | `test_strategy_package_does_not_import_brokers`, proposal-field test |
| Strategies cannot modify risk limits | frozen-policy mutation test |
| Restart does not clear a halt | `test_halt_survives_process_restart` |
| Reconnect does not auto-resume | `reconciliation_passed` gating test; chaos halt-then-blocked-submission test |
| Orders blocked until reconciliation | `test_reconciliation_pending_blocks_submission` |
| Duplicate signals cannot duplicate orders | deterministic intent ids + ledger/engine duplicate tests + sim-broker duplicate rejection |
| Unknown order status blocks | unknown-event halt test; state machine UNKNOWN→RECONCILIATION_REQUIRED |
| Loss limits prevent new orders | daily-loss rejection test |
| Stale data prevents new orders | stale-quote rejection test |
| Storage failure prevents new orders | ledger-failure halt tests (safety + chaos) |
| Invalid configuration prevents startup | risk-policy `extra=forbid` tests; settings validators (wheel suite) |
| Tests cannot reach live brokerage endpoints | no test constructs a live-capable lock (impossible via `resolve_mode_lock`); the only network-capable test is the skipped read-only smoke |

## Not implemented / requires owner action

| Item | Status |
|---|---|
| Paper-account submission integration test against real IB Gateway | REQUIRES OWNER ACTION (credentials + TWS; adapter is unit-tested against a fake IB object only) |
| TradingView parity fixtures | REQUIRES OWNER ACTION (exports; see fixtures/tradingview/README.md) |
| Property-based (hypothesis) tests | NOT IMPLEMENTED — invariants covered by deterministic LCG-driven tests instead; adding `hypothesis` is listed as future work in TEST_PLAN.md |
| Long-running shadow service tests | NOT IMPLEMENTED (no such service; shadow-scan is one-shot and tested via its components) |
| Backup/restore drill automation | NOT IMPLEMENTED as a test; manual procedure in docs/BACKUP_AND_RECOVERY.md |

No test result above is claimed without having been run in this environment;
the CI status on the PR is the independent confirmation channel.
