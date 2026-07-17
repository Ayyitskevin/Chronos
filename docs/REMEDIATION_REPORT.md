# Remediation Report

Disposition of every finding in [INDEPENDENT_REVIEW.md](INDEPENDENT_REVIEW.md).
All code fixes shipped with regression tests; the full suite is
**1158 passed, 1 credential-gated skip** after remediation (up from 1115),
lint/format/mypy clean.

## Fixed in code (with regression tests)

| ID | Severity | Fix | File(s) | Test |
|----|----------|-----|---------|------|
| C1 | CRITICAL | Wrote the missing adapter test suite; corrected the false coverage claim | `docs/…`, docstring in `execution/brokers/ibkr_paper.py` | `tests/platform_unit/test_ibkr_paper_adapter.py` (18) |
| H1 | HIGH | Re-read halt immediately before `broker.submit`, closing the TOCTOU | `execution/engine.py` | `test_safety_invariants.py::test_halt_landing_during_submission_is_caught` |
| H2 | HIGH | Fill quantity is authoritative over IB status text: `filled < total` → PARTIAL_FILL | `execution/brokers/ibkr_paper.py` | `test_ibkr_paper_adapter.py::test_filled_status_with_partial_quantity_is_partial_fill` |
| H3 | HIGH | `filled >= total` → FILLED regardless of status string | `execution/brokers/ibkr_paper.py` | `test_ibkr_paper_adapter.py::test_full_quantity_under_ack_status_is_filled` |
| H4 | HIGH | `AuditLog._recover` raises specific `AuditLogCorruptionError`; CLI catches and halts | `auditlog/log.py`, `auditlog/__init__.py`, `cli/main.py` | `test_auditlog.py::test_corrupt_last_line_fails_closed_on_construction` |
| H5 | HIGH | Disclosed the research cap-widening and its trade-count sensitivity; removed "near-miss" framing | `docs/RESEARCH_REPORT.md` | n/a (doc) |
| H6/H7 | HIGH | Reconciled contradictory/stale doc statuses; added HANDOFF.md | `docs/GO_LIVE_CHECKLIST.md`, `TASKS.md`, `CHANGELOG.md`, `docs/TEST_PLAN.md`, `docs/DEPLOYMENT.md`, `docs/TEST_RESULTS.md` | n/a (doc) |
| M1 | MEDIUM | Owner-only (0600) perms on ledger/halt/audit files, symlink- and ownership-checked | `utils/secure_files.py`, `control/halt.py`, `auditlog/log.py`, `execution/sqlite_ledger.py` | `test_secure_files.py` (7) |
| M2 | MEDIUM | Deny-by-default for unrecognized modes in `resolve_mode_lock` | `control/modes.py` | `test_safety_invariants.py::test_unrecognized_mode_denies_by_default` |
| M3 | MEDIUM | Length-prefixed fields in `intent_id` hash input | `execution/intents.py` | `test_intent_identity.py` (5) |
| M6 | MEDIUM | Corrected +19.0% → +18.9% dev figure | `docs/RESEARCH_REPORT.md` | n/a (doc) |
| M7 | MEDIUM | Disclosed validation cold-start | `docs/RESEARCH_REPORT.md` | n/a (doc) |
| L1 | LOW | fsync temp file + directory on halt write | `control/halt.py` | covered by `test_secure_files.py::test_halt_file_is_private` + existing halt tests |
| — | — | Uncaught broker-submit exception → UNKNOWN + reconciliation halt | `execution/engine.py` | `tests/chaos/test_execution_faults.py::TestBrokerSubmitException` |
| — | — | Filled coverage gaps: reconciliation, baselines | `execution/reconciliation.py`, `strategies/baselines.py` | `test_reconciliation.py` (6), `test_baselines.py` (4) |

## Accepted with documentation (not fixed in code this build)

These are real but their code fix belongs to the not-yet-implemented
long-running shadow/paper service loop; fixing them now would mean building
that service, which is explicitly out of scope. Each is recorded as a
go-live prerequisite.

| ID | Severity | Rationale | Where recorded |
|----|----------|-----------|----------------|
| M4 | MEDIUM | `reconcile()` is presence-only; state-level contradiction detection needs the service loop's per-order status evidence, which does not exist yet. The function is pure and has no auto-flatten. | RISK_REGISTER R-04; docs/GO_LIVE_CHECKLIST Gate 2/3; docs/IBKR_INTEGRATION.md |
| M5 | MEDIUM | Startup `_orders` hydration from the ledger is part of the service loop. Current behavior fails closed (halts on the unrecognized event) — safe, but loses the event's evidence trail. | docs/GO_LIVE_CHECKLIST Gate 2; docs/INCIDENT_RESPONSE.md |
| L2 | LOW | Manifest timestamp cosmetic; git history is the authoritative ordering proof. | INDEPENDENT_REVIEW L2 |
| L3 | LOW | Baseline seed choice; no best-of-N selection occurred, so no bias introduced. | INDEPENDENT_REVIEW L3 |

## Not defects (adversarial negatives)

The verified-clean list in INDEPENDENT_REVIEW records the hypotheses that
were tested and did **not** pan out — no look-ahead, no approval forgery, no
secret leakage, no injection, no fill-model cost skipping, genuine
integration tests, 3/3 mutations caught. These are recorded because
"we checked and it holds" is evidence, not silence.

## Net effect

Every CRITICAL and HIGH finding is either fixed in code with a regression
test or (for the two doc-status HIGHs) reconciled across the documentation
set. The two accepted MEDIUMs are structurally blocked on unbuilt scope and
are logged as explicit go-live prerequisites rather than silently deferred.
No finding was dismissed without a recorded rationale.
