# Go-Live Checklist

> **Current roadmap relationship (2026-08-01).** Retain this document for the
> deterministic-platform checklist and reviewed-release doctrine. Use
> [VISION_COMPLETION_PLAN.md](VISION_COMPLETION_PLAN.md) for repository-wide sequencing,
> autonomous scorecards, and the evidence required for each family promotion.

> **Scope note (2026-07-25).** This checklist governs the **deterministic strategy platform**
> (`chronos.execution`/`chronos.risk`) and its `TradingMode` ladder. Gates 4-5 below remain
> accurate for that plane: `resolve_mode_lock` still hard-denies CANARY_LIVE/LIVE, and
> ADR-0016 does not change it. Two things have moved since this was written, and the
> closing sentence "no item on any checklist in this repository authorizes live trading" is
> no longer true repository-wide:
>
> 1. The `chronos.orders` plane gained a gated live branch in Milestone 7 (ADR-0009).
> 2. ADR-0016/D-16 introduce autonomous operation under an owner mandate, with **its own**
>    per-asset-family promotion ladder (BACKTEST → REPLAY → SHADOW → PAPER_AUTONOMOUS →
>    CANARY_LIVE_AUTONOMOUS → CAPPED_LIVE_AUTONOMOUS) and its own frozen criteria, in
>    ADR-0016 §7. Use that ladder for anything autonomous; use this checklist for the
>    deterministic platform.
>
> The reviewed-release doctrine this document establishes — criteria frozen before results,
> single-step promotion, independent adversarial review before any live rung — is retained
> by ADR-0016 §7 and applies to every autonomous promotion.

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
  remediated and regression-tested (docs/INDEPENDENT_REVIEW.md, docs/REMEDIATION_REPORT.md). The
  two MEDIUM findings it deferred (state-level reconciliation, restart order hydration) were
  closed by the M2 service loop (RISK_REGISTER R-22/R-23, `tests/platform_unit/test_reconciliation.py`,
  `test_hydration.py`).
- [DONE] Second independent adversarial review after the continuation milestones (M1–M4), seven
  fresh dimensions, all findings remediated or explicitly accepted with rationale
  (docs/INDEPENDENT_REVIEW_M5.md).

## Gate 1 — Research/backtest exit (into REPLAY, then SHADOW)

- [PARTIAL] Historical daily OHLCV with provenance manifest in `research/data/raw/` — SPY
  (2000-01..2019-11, unadjusted) and QQQ (1999-11..2024-01, adjusted) byte-exact and
  cross-checked to the penny; IWM/GLD/TLT (2019-01..2021-12) added later via a transcribed
  parquet-preview transport — validator-clean and independently cross-checked, but
  dividend-adjusted (IWM/TLT), 2-decimal, and validation-window-only, so the universe is
  provenance-heterogeneous (`research/data/raw/MANIFEST.json`, `DATA_SOURCES.md`). DIA could
  **not** be acquired (confirmed absent from the source panel) and was excluded, not fabricated.
  Still PARTIAL: a uniform, full-history, trusted feed remains the owner action below.
- [DONE] Quantitative validation: chronological partitions (dev/validation/frozen-final-test),
  cost stress (2x commission) and slippage stress (5/10/25 bps), parameter sensitivity,
  baseline comparisons (buy-hold, SMA trend, deterministic random-entry twin), published in
  docs/RESEARCH_REPORT.md with data hashes and policy hash. Selection criteria were frozen
  (`research/selection_manifest.json`) **before** validation results were computed.
- [DONE] Strategy selection record (docs/STRATEGY_SELECTION.md): **zero candidates selected —
  confirmed on the broadened 5-symbol universe.** The binding failure is the frozen ≥20-closed-
  trade floor (C4), which no candidate reaches on any symbol (max 18); criteria were frozen
  before results, re-frozen unchanged before the added symbols were computed, and applied as
  written. This record requires owner review, not owner invention of new criteria after the fact.
- [DONE] Backtest reproducibility: identical inputs produce identical outputs; every run stamps
  code commit, data SHA-256, policy hash (`src/chronos/research/runner.py`).
- [OWNER] Re-run research from IBKR historical data (or another trusted, uniformly-adjusted
  source covering DIA and full histories) before trusting mirror-sourced conclusions
  (ASSUMPTIONS.md A-30 caveat). **Holdout status, honestly:** QQQ's reserved final window
  (2022–2024) was consumed by the M1 re-run and is now seen data (disclosure in
  docs/RESEARCH_REPORT.md §C6); a re-test must reserve a fresh untouched window. The harness now
  requires an explicit `--stage final` to touch any holdout.
- [NOT APPLICABLE] Promotion record RESEARCH→…→REPLAY→SHADOW: with zero candidates passing
  selection, there is nothing eligible to promote. A promotion record would be manufactured
  confidence; none was written.

## Gate 2 — Shadow gate (SHADOW → PAPER eligibility)

SHADOW means: live or replayed data, real intent generation, `NO_ORDERS` capability — nothing can
be submitted anywhere (`src/chronos/control/modes.py`).

- [PARTIAL] Shadow operation. Both a one-shot shadow scan
  (`python -m chronos.cli shadow-scan`) and a supervised service loop
  (`python -m chronos.service`, M2) exist: the service performs ordered startup (halt → hydrate →
  broker evidence → state-level reconciliation → arm), runs the production decision path each
  cycle, audits every decision, and cannot submit in SHADOW (`NO_ORDERS`; null broker raises;
  risk engine denies; capability gate). A read-only monitoring plane (M3:
  `python -m chronos.cli monitor` + a localhost Streamlit page) surfaces halt/reconciliation/
  audit/data state. Still PARTIAL because live bar ingestion is file-based (the operator supplies
  fresh CSVs; no market-data connection exists in this build) and no notification channel is
  wired.
- [NOT DONE] Defined shadow exit criteria (e.g. N consecutive sessions with zero unexplained
  halts, zero illegal transitions, intents matching backtest expectations, data-quality clean).
  Must be written into the promotion record's gate checks before the shadow run starts, not
  after.
- [OWNER] TWS/IB Gateway installed, API enabled, read-only smoke test passing
  (`scripts/smoke_test_ibkr.py`) — first proof this code has ever touched a real gateway.
- [OWNER] Operational discipline rehearsed: halt/rearm, backup/restore, reconnect procedure
  (docs/IBKR_RUNBOOK.md, docs/BACKUP_AND_RECOVERY.md) executed at least once each, for real.
- [DONE] Independent adversarial reviews completed (rounds 1 and 2) and all critical/high
  findings remediated (docs/INDEPENDENT_REVIEW.md, docs/REMEDIATION_REPORT.md,
  docs/INDEPENDENT_REVIEW_M5.md). The two MEDIUM findings previously deferred to this gate
  (state-level reconciliation, restart order hydration) were closed by the M2 service loop
  (R-22/R-23 MITIGATED). One accepted pre-PAPER item remains recorded: position reconciliation
  must be hardened from symbol-membership to signed-share comparison before real broker
  positions are wired in (INDEPENDENT_REVIEW_M5.md #13).

## Gate 3 — Paper gate (PAPER operation)

- [DONE — in code] Paper submission machinery: mode-lock conditions, paper-port pinning
  {7497, 4002}, account pattern `D[UF]\d{4,}`, exact managed-accounts verification before every
  submission, DAY-limit-only orders, `orderRef` idempotency, reconciliation gate with no
  auto-flatten (docs/IBKR_INTEGRATION.md).
- [PARTIAL] The service loop exists (M2) and gates submission on capability AND reconciliation;
  what remains for PAPER is wiring real broker evidence sources (positions/order states from the
  gateway) in place of the SHADOW null sources, plus the signed-share position comparison noted
  above.
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
  considered decision that ~~a ~USD 3,000 cash account~~ **the account as actually funded**
  should trade this system at all. *(Corrected 2026-08-02: the last documented snapshot is
  ≈ USD 110, not ~USD 3,000 — `docs/VISION_COMPLETION_PLAN.md` §2. The capital decision
  itself is open and owner-only; see ASSUMPTIONS A-10.)*

## Gate 5 — LIVE

Same status as Gate 4: refused for the deterministic strategy platform, and further away.

~~No item on any checklist in this repository authorizes live trading.~~ **Corrected
2026-07-25 — see the scope note at the top.** No item on *this* checklist authorizes live
trading, and this platform's mode lock still refuses it in code. Repository-wide the
statement is false: the `chronos.orders` plane has a gated live branch (ADR-0009), and
autonomous live operation is governed by ADR-0016 §7's separate per-family promotion ladder.
