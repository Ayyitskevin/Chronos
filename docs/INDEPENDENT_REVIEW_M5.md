# Independent Adversarial Review — Round 2 (M5)

Seven fresh, independent reviewer agents audited the platform at the
post-M1..M4 HEAD, each on one dimension, each instructed to hunt for real
defects, cite `file:line`, rate severity/confidence, and to say plainly when a
seam holds rather than manufacture findings. This document records their
verdicts and the remediation, which landed in the same change as this file.

## Verdicts by dimension

| Dimension | Verdict |
|---|---|
| Risk engine & safety seams (live-trading prevention) | **HOLDS.** No live-order path, approval forgery, policy mutation, or SHADOW submission constructible. Four independent layers block a SHADOW submit. One low-severity halt-reader robustness gap (fixed, below). |
| Execution / state machine / reconciliation | **Correct on the named threats** (transition table, terminal absorption, rehydration, R-22 bucket comparison incl. the broker-FILLED-vs-ledger-WORKING case, monitor SQL). Two medium asymmetries found and fixed (below). |
| Monitoring plane (M3) | **CLEAN on all five attack axes.** Transitive `sys.modules` trace confirms no broker/network reach; strictly read-only; no fabricated P&L; no control surface. Test hardening adopted (below). |
| Data provenance (M1) | **HONEST.** All hashes/bytes/rows recomputed and exact; NYSE calendar verified row-by-row; 13/14 spot values penny-exact; adjustment signatures genuine; DIA absence honestly disclosed. One mislabeled spot-check annotation (fixed). |
| Research methodology & honesty | **Numbers accurate** (every reported cell reproduced from the raw JSON, zero mismatches); re-freeze genuinely unchanged (git-verified); determinism reproduces to full float precision; multiple-testing handling "exemplary." **One HIGH honesty defect found** (final-window overclaim — corrected, below). |
| Supply chain / lockfile / CI | **SOUND** (fully hash-pinned, complete, correct `--no-deps` split; `aeventkit` verified as ib_async's genuine dependency). Two doc-level gaps fixed (below). |
| Test integrity | Property invariants **individually well-constructed and true**; CLI tests drive real paths. **One HIGH scope gap found** (limit checks relying on a vacuously-satisfiable property — closed, below). |

No Critical findings. No finding was manufactured; each reviewer explicitly
separated defects from documented design.

## Findings and remediation

| # | Sev | Finding | Remediation |
|---|-----|---------|-------------|
| 1 | HIGH | `docs/RESEARCH_REPORT.md` claimed the reserved final window was "not consumed / pristine," but the M1 re-run's `--stage all` computed and committed final-window results (`research_all.json`), consuming QQQ's one-shot holdout. | Report corrected: the final-window numbers are disclosed in full, with the statement that they did not (and could not) influence selection — rejection is decided by C4 on the validation window — and that QQQ's 2022–2024 holdout is now burned; future re-tests must reserve a fresh window. Process fix: `scripts/run_research.py --stage all` no longer includes `final`; consuming the holdout now requires an explicit `--stage final`. |
| 2 | HIGH | The deny-monotonicity property test is vacuously satisfied by a *disabled* limit check; ~11 risk-engine limits had no concrete "breach ⇒ deny" test anywhere, so deleting e.g. the aggregate-exposure check would pass the whole suite. | New `tests/platform_unit/test_risk_engine_limits.py`: a generous baseline policy approves a reference intent, then each of 12 limits is breached one at a time and must produce its specific rejection code (DIRECTION, AGGREGATE_EXPOSURE, SYMBOL_EXPOSURE, RISK_PER_TRADE, MAX_POSITIONS, MAX_OPEN_ORDERS, WEEKLY_LOSS, DRAWDOWN, CONSECUTIVE_LOSSES, PRICE_DEVIATION, PYRAMIDING + approving baseline). Deleting any one check now fails its test. |
| 3 | MED | `FILLED` event path accepted a *decreasing* cumulative fill silently (the symmetric partial-fill path halts), and overwrote `filled_quantity` even for a no-op duplicate on a terminal machine. | `engine.py`: FILLED now raises `OrderTransitionError` (→ STATE_CORRUPTION halt) on a fill-count regression, and assigns `filled_quantity` only when the transition actually applied. Tests: `test_engine_fill_guards.py`. |
| 4 | MED | Two halt paths (UNKNOWN_ORDER; illegal-transition-from-terminal) left `reconciliation_passed=True`, so a rearm-without-restart could resume trading on dropped evidence. | Both paths now clear `reconciliation_passed` unconditionally. Tests: `test_engine_fill_guards.py`. |
| 5 | MED | The CLI "no live path" test proved only a spelling denylist (a differently-named live command would pass); its docstring overclaimed. | Replaced with two tests: the denylist retained *as* a labeled regression guard, plus a structural test asserting every CLI-reachable mode resolves to a non-transmitting capability and the CLI imports no broker adapter; docstring corrected. Banner test now pins the "hard-disabled / no override exists" content, not just the label. |
| 6 | LOW | `HaltStore.read()` raised uncaught `AttributeError` on valid-but-non-object JSON (`[]`, `null`, `123`) instead of reading as HALTED/STATE_CORRUPTION per its documented contract (still fail-closed-by-crash; no order could escape). | Explicit `isinstance(payload, dict)` guard → `STATE_CORRUPTION`. Parametrized regression test added to `tests/safety/test_safety_invariants.py`. |
| 7 | LOW | `validate_series` silently passed `inf` prices, `NaN` volume (`NaN < 0` is False), and had no finiteness check. | New blocking `NON_FINITE_VALUE` issue kind; explicit `math.isfinite` check on OHLCV. Tests added. |
| 8 | LOW | The monitoring no-broker test was AST-only (own imports of three files) and would miss a future transitive broker import. | Added a subprocess `sys.modules` probe asserting no broker-ish module loads when importing every monitoring entry point. |
| 9 | LOW | GLD manifest spot-check labeled 193.89 (the 2020-08-06 *close*) as the "high" (true high 194.45); TLT "~15% below nominal" described the window end, not the ~19% start. | Both annotations corrected in `MANIFEST.json`/`DATA_SOURCES.md` (data itself was verified correct). |
| 10 | LOW-MED | `docs/DEPLOYMENT.md` still said "there is no lockfile in this repository," contradicting SECURITY.md and the R-15 MITIGATED status; SECURITY.md's "everything is hash-checked" overclaimed (PEP 517 build backend and pip itself are outside the gate). | DEPLOYMENT.md now installs from the lock with `--require-hashes`; SECURITY.md and R-15 state the build-backend/pip residual explicitly. `aeventkit` verified as ib_async's genuine declared dependency (ib-api-reloaded's republication of eventkit), recorded in SECURITY.md. |
| 11 | INFO | Property-test `_MONOTONE_LIMIT_FIELDS` comment implied two inert fields deny; presence-only assertions on `code_commit`/`policy_hash` in the runner manifest test. | Comment corrected with honest scope notes; manifest test now asserts real value shapes. |
| 12 | INFO (accepted) | `RiskEngine.token` is a readable property and `ModeLock`'s constructor is public — defense-in-depth smells, not exploitable through the strategy seam (strategy code receives only bars + position and cannot reach either object); even a fabricated PAPER lock cannot reach a live broker (the only real adapter independently re-verifies paper account + paper-only port). | Accepted as documented design; recorded here rather than churning a working safety seam. |
| 13 | INFO (accepted, pre-PAPER gate) | Position reconciliation compares symbol membership, not share quantity; in SHADOW there are no broker positions so no current exposure. | Deferred by scope note: must be hardened (compare expected signed shares) before any PAPER wiring supplies real positions. Recorded in the go-live checklist expectations via this document. |

## Residual risks (unchanged by this round)

R-08 (research-grade data), R-09 (backtest inherent bias), R-12 (ib_async
maintenance), R-18 (clock drift) remain OPEN/ACCEPTED as documented in
RISK_REGISTER.md. The consumed QQQ holdout is now itself a recorded research
limitation (RESEARCH_REPORT C6 disclosure).
