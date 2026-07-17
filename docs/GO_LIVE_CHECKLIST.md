# Go-Live Checklist

Gate-by-gate path from where the build is today to (eventually) real-money trading. Statuses are
honest and current as of 2026-07-17:

- **[DONE]** implemented and verified in this repository.
- **[PARTIAL]** exists but incomplete or unverified in some material way.
- **[NOT DONE]** does not exist yet.
- **[OWNER]** requires the owner's action, credentials, or judgment — cannot be completed by this
  build environment.

Promotion is single-step and evidence-based (`src/chronos/control/promotion.py`); no gate below
"arms" anything by itself. The mode lock re-derives capability from live evidence every time
(ADR-0007).

**Bottom line as of this build:** the platform machinery (Gate 0) is implemented and tested, and
the research process (Gate 1) ran to completion — but it concluded that **neither derived
strategy currently has a demonstrated edge** (docs/STRATEGY_SELECTION.md). Gates 2 and 3 (shadow,
paper) therefore have no eligible candidate to carry through them yet; the machinery to do so
exists and is tested, but exercising it today would be theater, not evidence. Gates 4 and 5 (live)
remain refused in code regardless.

## Gate 0 — Foundation (prerequisite to everything)

- [DONE] Deterministic platform implemented: marketdata, indicators, specs, strategies (2 derived
  + 3 baselines), portfolio, risk engine, execution engine + state machine + ledgers, simulated
  broker with fault injection, IBKR paper adapter, reconciliation gate, control plane, audit log,
  backtest engine, research runner, CLI (TASKS.md).
- [DONE] Safety acceptance suite green (`tests/safety/`, 29 tests); legacy wheel suite green
  (951 passed / 1 skipped baseline).
- [DONE] CI gates on every push: ruff, format, mypy strict, pytest (`.github/workflows/ci.yml`).
- [DONE] Live-capable modes hard-refused in code; paper capability requires six simultaneous
  conditions (tested).
- [DONE] Persistent fail-closed halt; deny-by-default risk policy schema; hash-chained audit log.
- [DONE] Platform unit/parity/chaos suites: 135 tests (99 unit, 27 parity, 9 chaos), all green
  (docs/TEST_PLAN.md, docs/TEST_RESULTS.md). Full suite incl. legacy wheel dashboard:
  1115 passed, 1 credential-gated skip.
- [PARTIAL] Pine corpus audit (docs/PINE_AUDIT.md) — in flight (TASKS.md); all 42 scripts fetched
  and hash-pinned, semantic audit in progress.
- [DONE] docs/TEST_RESULTS.md, docs/RESEARCH_REPORT.md, docs/STRATEGY_SELECTION.md.
- [DONE] Pine corpus forensic audit complete: all 42 scripts, docs/PINE_AUDIT.md +
  research/pine_findings.json. Distribution: 28 `NON_EXECUTABLE_INDICATOR`,
  13 `PASS_WITH_CONSTRAINTS`, 1 `REQUIRES_REWRITE` (script 08, a trivial
  use-before-declare compile blocker — documented, not fixed upstream). Zero
  `REPAINTING` or `LOOKAHEAD_CONTAMINATED` findings; every `request.security`
  call across the corpus uses the safe `[1]`-offset + `lookahead_on` idiom.
- [DONE] Independent adversarial review across seven dimensions, with all CRITICAL/HIGH findings
  remediated and regression-tested (docs/INDEPENDENT_REVIEW.md, docs/REMEDIATION_REPORT.md). Two
  MEDIUM findings (state-level reconciliation, restart order hydration) are accepted as go-live
  prerequisites blocked on the unbuilt service loop, not silently deferred.

## Gate 1 — Research/backtest exit (into REPLAY, then SHADOW)

- [PARTIAL] Historical daily OHLCV with provenance manifest in `research/data/raw/` — SPY
  (2000-01..2019-11, unadjusted) and QQQ (1999-11..2024-01, adjusted) acquired, integrity-
  validated, and cross-checked to the penny against an independent source
  (`research/data/raw/MANIFEST.json`, `DATA_SOURCES.md`). IWM/DIA/GLD/TLT could **not** be
  trustworthily acquired in this environment and were excluded, not fabricated.
- [DONE] Quantitative validation: chronological partitions (dev/validation/frozen-final-test),
  cost stress (2x commission) and slippage stress (5/10/25 bps), parameter sensitivity,
  baseline comparisons (buy-hold, SMA trend, deterministic random-entry twin), published in
  docs/RESEARCH_REPORT.md with data hashes and policy hash. Selection criteria were frozen
  (`research/selection_manifest.json`) **before** validation results were computed.
- [DONE] Strategy selection record (docs/STRATEGY_SELECTION.md): **zero candidates selected.**
  `mean_reversion_v1` fails the frozen net-positive criterion; `regime_trend_v1` passes three of
  five frozen criteria on QQQ but fails the frozen minimum-trade-count floor by two trades — the
  criterion was applied as written, not relaxed post hoc. This record requires owner review, not
  owner invention of new criteria after the fact.
- [DONE] Backtest reproducibility: identical inputs produce identical outputs; every run stamps
  code commit, data SHA-256, policy hash (`src/chronos/research/runner.py`).
- [OWNER] Re-run research from IBKR historical data (or another trusted source covering
  IWM/DIA/GLD/TLT and a longer SPY history) before trusting mirror-sourced conclusions
  (ASSUMPTIONS.md A-30 caveat). The frozen final-test window (2022-01-01..) was **never consumed**
  and remains available for that re-run.
- [NOT APPLICABLE] Promotion record RESEARCH→…→REPLAY→SHADOW: with zero candidates passing
  selection, there is nothing eligible to promote. A promotion record would be manufactured
  confidence; none was written.

## Gate 2 — Shadow gate (SHADOW → PAPER eligibility)

SHADOW means: live or replayed data, real intent generation, `NO_ORDERS` capability — nothing can
be submitted anywhere (`src/chronos/control/modes.py`).

- [PARTIAL] Shadow operation. A one-shot shadow scan exists
  (`python -m chronos.cli shadow-scan`, `src/chronos/research/shadow.py`): it runs the production
  decision path over the latest closed bars, reports would-be intents and risk decisions, appends
  every report to the audit log, and cannot submit (SHADOW lock = `NO_ORDERS`, no broker adapter
  constructed). **No long-running service exists** — nothing wires live bar ingestion,
  reconciliation evidence gathering, and notifications into a daemon (docs/DEPLOYMENT.md "Future
  work"). Shadow today means running the scan manually after each close.
- [NOT DONE] Defined shadow exit criteria (e.g. N consecutive sessions with zero unexplained
  halts, zero illegal transitions, intents matching backtest expectations, data-quality clean).
  Must be written into the promotion record's gate checks before the shadow run starts, not
  after.
- [OWNER] TWS/IB Gateway installed, API enabled, read-only smoke test passing
  (`scripts/smoke_test_ibkr.py`) — first proof this code has ever touched a real gateway.
- [OWNER] Operational discipline rehearsed: halt/rearm, backup/restore, reconnect procedure
  (docs/IBKR_RUNBOOK.md, docs/BACKUP_AND_RECOVERY.md) executed at least once each, for real.
- [DONE] Independent adversarial review completed and all critical/high findings remediated
  (docs/INDEPENDENT_REVIEW.md, docs/REMEDIATION_REPORT.md). Note: two MEDIUM findings specific to
  this gate (M4 state-level reconciliation, M5 restart order hydration) remain open because they
  require the not-yet-built shadow/paper service loop — they must be closed before a real shadow
  run, not before merging this research build.

## Gate 3 — Paper gate (PAPER operation)

- [DONE — in code] Paper submission machinery: mode-lock conditions, paper-port pinning
  {7497, 4002}, account pattern `D[UF]\d{4,}`, exact managed-accounts verification before every
  submission, DAY-limit-only orders, `orderRef` idempotency, reconciliation gate with no
  auto-flatten (docs/IBKR_INTEGRATION.md).
- [NOT DONE] The service loop that would actually run PAPER mode (same gap as Gate 2).
- [OWNER] First supervised paper submissions against the owner's paper account: verify ack/fill
  event translation, commission reports, ledger and audit records, and reconciliation against
  real broker state. The adapter has never run against a real gateway
  (`src/chronos/execution/brokers/ibkr_paper.py` STATUS note).
- [OWNER] Operator-maintained paper allowlist configured with the real `DU…`/`DF…` account id;
  transmission explicitly enabled at the mode-lock inputs.
- [NOT DONE] Defined paper exit criteria (e.g. M sessions/trades with zero reconciliation
  discrepancies, fills consistent with the backtest fill model net of costs, no SEV-1/SEV-2
  incidents) recorded in a promotion record before the run.
- [NOT DONE] Risk policy for paper (`config/risk.yaml`, gitignored by default) written and
  reviewed by the owner — the example file denies everything by design
  (`config/risk.example.yaml`). Moot until a strategy passes Gate 1 selection.

## Gate 4 — Canary eligibility (CANARY_LIVE)

**Refused by this build. Not a configuration away — refused in code.**

- `resolve_mode_lock` returns `DENIED_LIVE_DISABLED` for CANARY_LIVE and LIVE unconditionally
  (`src/chronos/control/modes.py`); the promotion evaluator appends a failing
  `live_capability_hard_disabled` gate to any live-mode promotion
  (`src/chronos/control/promotion.py`); tests assert both
  (`tests/safety/test_safety_invariants.py`).
- Reaching canary would require a FUTURE REVIEWED RELEASE: a deliberate code change removing the
  hard denial, new live-specific safety code (live account allowlisting, capital authorization —
  neither is even representable in today's schema), new tests, a new independent review, and
  explicit owner approval. None of that exists, and this document confers none of it.
- [OWNER] Everything above, sustained: a clean paper record, incident-free operations, and a
  considered decision that a ~USD 3,000 cash account should trade this system at all.

## Gate 5 — LIVE

Same status as Gate 4: refused by this build, and further away. No item on any checklist in this
repository authorizes live trading.
