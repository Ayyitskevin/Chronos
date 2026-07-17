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
- [PARTIAL] Platform unit/parity/chaos suites — directories exist; being authored in parallel
  (docs/TEST_PLAN.md; counts land in docs/TEST_RESULTS.md).
- [PARTIAL] Pine corpus audit (docs/PINE_AUDIT.md) — in flight (TASKS.md).
- [NOT DONE] docs/TEST_RESULTS.md, docs/RESEARCH_REPORT.md, docs/STRATEGY_SELECTION.md,
  independent adversarial review (TASKS.md "Next").

## Gate 1 — Research/backtest exit (into REPLAY, then SHADOW)

- [PARTIAL] Historical daily OHLCV with provenance manifest in `research/data/raw/` — acquisition
  in flight; `research/data/raw/` is not yet populated.
- [NOT DONE] Quantitative validation: chronological partitions, walk-forward, cost/slippage
  stress at 2/5/10/25 bps, baseline comparisons, per-symbol results, published in
  docs/RESEARCH_REPORT.md with data hashes and policy hashes.
- [NOT DONE] Strategy selection record (docs/STRATEGY_SELECTION.md): which strategy ids/symbols
  are candidates and why, signed off by the owner.
- [DONE] Backtest reproducibility: identical inputs produce identical outputs; every run stamps
  code commit, data SHA-256, policy hash (`src/chronos/research/runner.py`).
- [OWNER] Re-run research from IBKR historical data before trusting mirror-sourced conclusions
  (ASSUMPTIONS.md A-30 caveat).
- [NOT DONE] Promotion record RESEARCH→…→REPLAY→SHADOW written via
  `chronos.control.promotion.write_promotion_record` with all gate checks passing.

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
- [NOT DONE] Independent adversarial review completed and critical/high findings remediated
  (TASKS.md).

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
- [NOT DONE] Risk policy for paper (`config/risk.yaml`) written and reviewed by the owner — the
  example file denies everything by design (`config/risk.example.yaml`; note that file's
  "gitignored" comment is currently inaccurate — see docs/OPERATIONS.md).

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
