# Test Results

Companion to [TEST_PLAN.md](TEST_PLAN.md). All commands run from the repo
root on Python 3.12 using the existing `.venv` directly. Statuses use the plan's vocabulary:
PASSED / FAILED / SKIPPED / NOT RUNNABLE WITHOUT CREDENTIALS /
NOT IMPLEMENTED / REQUIRES OWNER ACTION.

## Summary (current — re-measured 2026-08-23)

Measured on the option-chain salvage merge result (`main` `93a26f6` + codex's
`ae9d256`), Python 3.12, all five gates run in CI order and in CI's own default
test order. **These numbers drift with every merged test** — re-run before
citing them.

| Command | Result |
|---|---|
| `pytest -q` | **3976 passed, 1 skipped** (~153 s; 3977 collected) |
| `ruff check .` | clean |
| `ruff format --check .` | clean (525 files) |
| `mypy src/chronos` | clean (286 source files) |
| `mypy --strict worker` | clean (10 source files) |
| GitHub Actions `quality` job | verify on the current PR |

The single skip is the opt-in, credential-gated, read-only IBKR smoke test — it
has never been run against a real gateway, which is why it skips; it does not
fail. For comparison, `main` alone collects 3630 tests at `93a26f6`.

## Summary (historical — re-measured 2026-08-02, superseded)

Measured on this date against the merge commit `7f2d208`, Python 3.12, all four gates run
in CI order. **These numbers drift with every merged test** — re-run before citing them;
the authoritative live command is the first row.

| Command | Result |
|---|---|
| `.venv/bin/pytest -q` | **2489 passed, 1 skipped** (~89 s; 2490 collected) |
| `.venv/bin/ruff check .` | clean |
| `.venv/bin/ruff format --check .` | clean (379 files) |
| `.venv/bin/mypy src/chronos` | clean, strict (218 source files) |
| GitHub Actions `quality` job | verify on the current PR |

The single skip is the opt-in, read-only IBKR smoke test
(`tests/integration/test_ibkr_smoke.py`, marker `ibkr`, enabled with
`CHRONOS_RUN_IBKR_SMOKE=1`). It is skipped because **no real gateway has ever been
connected** — not because it fails.

## ADR-0030 focused verification (2026-08-01)

| Command | Result |
|---|---|
| `PATH="$PWD/.venv/bin:$PATH" BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false ALLOW_LIVE_TRADING=false pytest -q tests/unit/test_request_registry.py tests/unit/test_callback_bridge.py tests/unit/test_ibkr_broker.py tests/unit/test_official_ibkr.py tests/unit/test_market_data.py tests/unit/test_option_selection.py tests/unit/test_option_selection_service.py tests/safety/test_supervisor_durable_state.py tests/safety/test_option_selection_cycle.py tests/integration/test_option_selection_terminal.py` | **512 passed**, 1 Starlette deprecation warning (12.05 s) |

This focused run covers the bounded market-data seam, pure resolver and golden
replay, promotion/partial-evidence service, durable cycle integration, and
authenticated GET-only receipt inspection. It used only offline fakes. The
credential-gated real-IBKR smoke was not run, no live resolver-promotion artifact
was created, and no order was placed.

## Final repository gate (2026-08-01)

All commands used `BROKER_MODE=demo ALLOW_ORDER_TRANSMIT=false
ALLOW_LIVE_TRADING=false` and `PATH="$PWD/.venv/bin:$PATH"`.

| Command | Result |
|---|---|
| `ruff check .` | **PASSED** — all checks passed |
| `ruff format --check .` | **PASSED** — 380 files already formatted |
| `mypy src/chronos` | **PASSED** — no issues in 220 source files |
| `pytest -q` | **2,836 passed, 1 skipped, 5 warnings** (64.21 s) |

The single skip is the explicitly opt-in, credential-gated, read-only IBKR
smoke test. The five warnings are existing Starlette/FastAPI HTTP-status
deprecations. The older snapshots below remain historical evidence, not a claim
about the present working tree.

A green suite proves the code behaves as its tests specify. It does **not** prove
gateway conformance, live-execution quality, or strategy edge: every adapter path is
fixture-verified only, and zero strategies have cleared the promotion ladder
(`docs/VISION_COMPLETION_PLAN.md` §2).

## Summary (historical — M2a, 2026-07-25, superseded)

*(Relabeled 2026-08-02: this section was headed "current" while reporting the M2a
counts, ~588 tests behind reality.)*

| Command | Result |
|---|---|
| `.venv/bin/pytest -q` | 1901 passed, 1 skipped (~61 s) |
| `.venv/bin/ruff check .` | clean |
| `.venv/bin/ruff format --check .` | clean (324 files) |
| `.venv/bin/mypy src/chronos` | clean, strict (190 source files) |

At that date `tests/safety/` held ~90 tests, including `test_autonomy_contracts.py`
(the ADR-0016 / D-16 structural suite: model-plane import isolation, decision
order-incapability, mandate immutability under `model_copy`, per-family promotion,
floors, and the M1 milestone guard).

## Summary (historical — post-M5 snapshot, superseded)

| Command | Result |
|---|---|
| `.venv/bin/pytest -q` | 1255 passed, 1 skipped (~27 s) |
| `.venv/bin/ruff check .` | clean |
| `.venv/bin/ruff format --check .` | clean (173 files) |
| `.venv/bin/mypy src/chronos` | clean, strict (100 source files) |
| GitHub Actions `quality` job | green on the merged continuation PRs (#2, #3) |

The per-layer tables below date from that snapshot; their counts are historical.

The 1255 total is 953 (wheel + monitor-page integration) + 36 (safety) + 226
(platform_unit) + 27 (parity) + 13 (chaos) + 1 skipped; the platform suites
grew across two independent-review remediations
(docs/REMEDIATION_REPORT.md, docs/INDEPENDENT_REVIEW_M5.md) and the M2–M4
milestones.

## Breakdown

| Suite | Count | Status | Notes |
|---|---|---|---|
| Wheel dashboard `tests/unit` + `tests/integration` (+ monitor-page AppTest) | 953 | PASSED | wheel baseline preserved; adds the read-only monitor Streamlit page render tests |
| `tests/integration/test_ibkr_smoke.py` | 1 | SKIPPED / NOT RUNNABLE WITHOUT CREDENTIALS | opt-in via `CHRONOS_RUN_IBKR_SMOKE=1` + running TWS/Gateway; strictly read-only |
| `tests/safety` (platform safety acceptance) | 36 | PASSED | mode locks (live denial under maximal config; six-condition paper lock; unrecognized-mode deny-by-default), halt persistence/fail-closed incl. non-object-JSON corruption, deny-by-default risk engine, execution gating (forged/foreign/mismatched approvals, halt, halt-during-submission TOCTOU, reconciliation, duplicates, ledger failure, unknown events), strategy isolation |
| `tests/platform_unit` | 226 | PASSED | state machine (incl. terminal fill-smear guard), sim broker, ledgers (incl. SQLite reopen + schema tamper), sizer, data quality/CSV (incl. non-finite guards), audit-log tamper + corrupt-recovery, promotion gates, notifier isolation, specs schema, metrics hand-checks, IBKR paper adapter, state-level reconciliation (R-22), restart hydration (R-23), service loop + startup, monitoring plane (incl. transitive no-broker probe), CLI end-to-end, research runner/shadow, property-based invariants (hypothesis), concrete breach⇒deny risk-limit matrix, engine fill guards |
| `tests/parity` | 27 | PASSED | hand-computed indicator references; incremental-vs-batch equality at 1e-9; continuity-reset equivalence. **Specification-level parity only — no TradingView fixtures exist (docs/PARITY_REPORT.md)** |
| `tests/chaos` | 13 | PASSED | rejection/partial/duplicate/drop-ack/rogue-event/ledger-failure/broker-submit-exception paths; fault-run determinism; service-loop fault handling |

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
| Model cannot choose an option contract | request-schema structural test plus strategy-derived-right service test; no `conId`, strike, expiry, route, or trading-class request field |
| Missing or partial option evidence cannot rank | resolver mutation matrix, exact-set market-data/service tests, completion-provenance tests, and unknown-liquidity-with-zero-floors test |
| Option receipt is durable before handoff | independent-session visibility, crash-after-commit, full-chain semantic tamper, conflicting reuse, and pre-handoff recheck tests |
| Live option resolver authority cannot drift | exact-one-mode artifact validation, canonical mandate/policy/source bindings, post-acquisition expiry/replacement, and immediate pre-handoff validation tests |

## Not implemented / requires owner action

| Item | Status |
|---|---|
| Paper-account submission integration test against real IB Gateway | REQUIRES OWNER ACTION (credentials + TWS; the adapter is unit-tested against a fake IB object in `tests/platform_unit/test_ibkr_paper_adapter.py`, never against a real gateway) |
| Positive real-IBKR autonomous option selection | NOT IMPLEMENTED: TWS exposes no authoritative deliverable schedule, both adapters return `authoritative=False`, and the expected current result is `NO_TRADE` |
| Live option-resolver promotion | REQUIRES OWNER ACTION + HUMAN SIGN-OFF after an authoritative source and gateway verification; runtime has no writer and this change created no artifact |
| TradingView parity fixtures | REQUIRES OWNER ACTION (exports; see fixtures/tradingview/README.md) |
| Property-based (hypothesis) tests | IMPLEMENTED (M4): `tests/platform_unit/test_property_invariants.py` — intent identity, state-machine legality, sizer bounds, deny-monotonicity; complemented by the concrete breach⇒deny matrix (`test_risk_engine_limits.py`, M5) |
| Long-running shadow service tests | IMPLEMENTED (M2): `tests/platform_unit/test_service.py`, `tests/chaos/test_service_faults.py` |
| Backup/restore drill automation | NOT IMPLEMENTED as a test; manual procedure in docs/BACKUP_AND_RECOVERY.md |

No test result above is claimed without having been run in this environment;
the CI status on the PR is the independent confirmation channel.
