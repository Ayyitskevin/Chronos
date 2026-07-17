# Independent Adversarial Review

Seven fresh review agents, none of which authored the modules they examined,
were tasked to *demonstrate* defects (not summarize design) across the
dimensions the build brief names. Each was told to write throwaway repros in
`/tmp` and prove claims with file:line evidence or reproduction output.
Findings and their remediation status are below; the Pine semantic-parity
dimension was covered by the Phase 2 forensic audit (docs/PINE_AUDIT.md).

Remediation detail: [REMEDIATION_REPORT.md](REMEDIATION_REPORT.md).

## Reviews performed

| Dimension | Reviewer mandate | Outcome |
|---|---|---|
| Quantitative methodology | look-ahead, overfitting, leakage, cost modeling, misleading reporting, multiple-testing | 1 HIGH (transparency), 3 MEDIUM, 1 LOW; **"zero candidates" conclusion upheld as trustworthy** |
| Risk architecture | risk-limit bypass, accidental live activation, halt bypass, duplicate orders, state corruption | 1 HIGH (halt TOCTOU), 2 MEDIUM latent, 1 LOW; core controls otherwise sound |
| Brokerage integration | account mismatch, disconnect recovery, partial fills, callback dedup, reconciliation | 1 CRITICAL (doc overclaim), 2 HIGH (fill translation), 2 MEDIUM |
| Security | secret leakage, audit-log contents, dependencies, CI, injection, file perms | 1 MEDIUM (file perms), rest verified clean |
| Failure recovery | restart mid-order, torn writes, audit corruption, durability, no-auto-flatten | 1 HIGH (audit-log crash), 1 MEDIUM, 1 LOW; atomicity/durability confirmed |
| Test quality | count accuracy, assertion strength, mutation testing, coverage gaps, flakiness | 3/3 mutations caught, counts exact, no flakiness; coverage gaps found |
| Documentation accuracy | every claim vs. code, cross-doc consistency, stale statuses | 2 HIGH (stale/contradictory statuses), 3 MEDIUM, all doc-only |

## Findings by severity

### CRITICAL

- **C1 — `ibkr_paper.py` docstring and TEST_RESULTS claimed unit-test coverage that did not exist.** The adapter had zero tests; every scenario in the brokerage review was being exercised for the first time. *Fixed:* wrote `tests/platform_unit/test_ibkr_paper_adapter.py` (18 tests); corrected the docstring and TEST_RESULTS.

### HIGH

- **H1 — Halt TOCTOU (risk review).** `submit_approved` read the halt once at the top, then did ledger I/O (a real fsync) before `broker.submit`; a halt landing in that window was missed and the order still reached the broker (reproduced). *Fixed:* re-read the halt immediately before submission; regression test `test_halt_landing_during_submission_is_caught`.
- **H2 — `drain_events` could emit FILLED with `filled < total` (brokerage review).** IB status text was trusted over fill quantity; an inconsistent poll would terminally mark an order FILLED and silently lose the unfilled remainder (reproduced). *Fixed:* fill quantity is now authoritative for every fill-relevant kind; tested.
- **H3 — Full fill disguised as ACKNOWLEDGED could be dropped (brokerage review).** A `filled == total` under a non-Filled status was reported as ACKNOWLEDGED, which the engine treats as a no-op, so the fill never reached the ledger (reproduced). *Fixed:* same authoritative-quantity change reports FILLED; tested.
- **H4 — `AuditLog.__init__` crashed uncleanly on a corrupt last line (recovery review).** Unlike `verify_chain`, `_recover` had no exception handling; a truncated final line raised a raw `json.JSONDecodeError` out of construction, and the only caller had no handler. *Fixed:* `_recover` now raises a specific `AuditLogCorruptionError`; the CLI shadow-scan caller catches it and halts with `AUDIT_LOG_FAILURE`; tested.
- **H5 — Undisclosed research cap-widening made the near-miss framing misleading (quant review).** The research policy raised notional/capital caps to USD 10M in the same commit that froze selection criteria, without disclosure; under the original USD 3,000 caps the flagship candidate makes 7 trades, not 18. The pass/fail *outcome* is unchanged (both fail the 20-trade floor) and no look-ahead/leakage/fabrication was found — but the "missed by 2" narrative was cap-dependent. *Fixed:* docs/RESEARCH_REPORT.md now discloses the cap sensitivity explicitly and removes the near-miss language.
- **H6 & H7 — Stale/contradictory documentation status (docs review).** GO_LIVE_CHECKLIST asserted the Pine audit was both "in progress" and "complete"; TASKS.md/CHANGELOG/TEST_PLAN/DEPLOYMENT described finished work as in-flight. *Fixed:* statuses reconciled across all affected docs; HANDOFF.md added.

### MEDIUM

- **M1 — Platform state files were world-readable (security review).** The wheel dashboard hardens its SQLite/logs to `0600`, but `platform_ledger.db`, `platform_halt.json`, and `platform_audit.jsonl` got default umask perms. *Fixed:* new `chronos.utils.secure_files.secure_owner_only` (symlink- and ownership-checked) applied to all three (+ WAL/SHM sidecars); tested.
- **M2 — `resolve_mode_lock` had no deny-by-default for unrecognized modes (risk review, latent).** An unhandled mode value fell through into the PAPER evaluation. Not reachable today (all callers pass enum literals) but unsafe if a config-driven selector is added. *Fixed:* explicit deny-by-default; tested.
- **M3 — `intent_id` was not collision-resistant against embedded delimiters (risk review, latent).** Pipe-joined free-text fields could collide across different strategy attributions. Not reachable with valid enum-anchored economic content, but the duplicate-suppression key must be robust. *Fixed:* length-prefixed fields; collision tests added.
- **M4 — `reconcile()` is presence-only (brokerage review).** It compares id sets, not per-order state; an order present in both systems but in contradictory internal states is not caught. *Accepted with documentation:* the reconciliation caller (the shadow/paper service loop) is not yet implemented, and this is disclosed in docs/IBKR_INTEGRATION.md. Recorded as RISK_REGISTER R-04 residual and a go-live prerequisite.
- **M5 — Restart does not hydrate `_orders`; an in-flight order's event is dropped and halts (recovery review).** Safe (fails closed with `UNKNOWN_ORDER`) but loses the event's evidence trail. *Accepted with documentation:* startup `_orders` hydration from the ledger is part of the not-yet-built service loop; recorded as a go-live prerequisite.
- **M6 — One dev-window figure mis-stated (quant review): +19.0% vs. actual +18.9%.** *Fixed.*
- **M7 — Validation indicators cold-start at the window boundary (quant review).** Conservative (shrinks the sample, works against the candidate), but undisclosed. *Fixed:* disclosed in docs/RESEARCH_REPORT.md confidence limitations.

### LOW

- **L1 — Halt-file write lacked fsync (risk review).** Atomic rename prevented torn reads but not power-loss durability. *Fixed:* fsync of the temp file and containing directory.
- **L2 — Manifest self-reported timestamp 14 min ahead of its commit (quant review).** Immaterial to the freeze-before-results ordering, which git history independently confirms. *Accepted, noted.*
- **L3 — Random-baseline seed encodes the build date (quant review).** No best-of-N seed selection occurred. *Accepted, noted.*

## Verified-clean (adversarial negative findings worth recording)

- No look-ahead bias in the backtest engine: decisions at bar t use closed
  bars, fills occur no earlier than t+1, stop checks do not peek forward
  (quant review, traced).
- Costs applied to every fill; whole-share rounding consistent (quant review).
- All reported validation numbers reproduce bit-for-bit from a fresh run
  (quant review).
- Risk-approval forgery refused by object-identity token; strategies cannot
  construct a valid approval or call the broker (risk review, reproduced).
- Halt file atomicity holds; SQLite WAL + synchronous=FULL actually applied;
  duplicate-intent race degrades to a fail-closed halt, not a bypass
  (recovery review, reproduced).
- No secrets in git history or working tree; `.env.example` placeholder-only;
  audit log's single call site writes no credentials; SQL fully parameterized;
  no shell/command injection; CI exposes no secrets (security review).
- Test suite is genuine integration (real components wired together), catches
  3/3 injected mutations, and has no time/randomness flakiness (test review).
- Account verification runs before every submission with exact list equality;
  unknown broker events uniformly halt; no cancel-replace path exists to
  mis-handle (brokerage review, reproduced).

## Reviewer verdict on the headline conclusion

The quantitative reviewer's overall verdict, quoted: *"The 'zero candidates
selected' conclusion is trustworthy as a bottom line: I found no look-ahead
bug, no fabricated or mismatched validation numbers, no cherry-picked
sensitivity subset, and the criteria-freeze-before-results ordering is real."*
The one substantive issue was transparency (H5), now remediated.
