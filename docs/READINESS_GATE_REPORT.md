# Research readiness gate — hardening report

**Branch:** `feat/research-readiness-gate`  
**Base:** `origin/feat/wheel-dashboard-mvp` @ `2608977`  
**Date:** 2026-07-20  
**Scope:** Scientific honesty + operational safety only. No strategy features, no live enablement, no holdout unseal, no merge/deploy.

---

## Findings (plane map)

### Research plane
- Modules: `chronos.research.*`, `chronos.registry.*`, `chronos.histdata.*`, CLI campaign/backtest, `config/risk.research.yaml`.
- Execution: deterministic backtest → **simulated** broker only.
- Isolation: AST + `sys.modules` probes forbid `chronos.orders` / `chronos.broker` from research edge modules (including new `manifest` / `readiness`).

### Paper plane
- Path: `OrderSubmissionBoundary._submit_paper` when `settings.transmission_possible` (IBKR + PAPER + `ALLOW_ORDER_TRANSMIT` + account + not live).
- Mode lock: ADR-0007; live modes hard-denied as `DENIED_LIVE_DISABLED`.

### Live plane
- Path: `OrderSubmissionBoundary._submit_live` → single `transmit=True` site in `submission.py`.
- Gates: ADR-0009 conjunction, live grant, ten-gate stack, kill switch re-read, arming, typed confirmation.
- **Explicit outcome:** `LIVE TRADING BLOCKED` (`chronos.orders.live_block`).

### Residual risks (unchanged by this work)
1. Live **capability code** exists and is integration-tested with spies — production defaults still refuse; do not confuse tests with enablement.
2. Historical research data is provenance-heterogeneous (see `docs/RESEARCH_REPORT.md`); re-run on IBKR before trusting paper promotion.
3. Autonomous `chronos.execution` retains a separate simulated/paper transmit site; not wired from production orders path (documented single-site invariant covers `chronos.orders`).
4. QQQ holdout was historically consumed in an earlier M1 re-run; campaign wall still seals 2022+ for C4 automation.

---

## What shipped

| Item | Location |
|------|----------|
| Explicit LIVE TRADING BLOCKED gate | `src/chronos/orders/live_block.py`; wired into live submission refusal detail |
| Deterministic research-run manifests | `src/chronos/research/manifest.py` (config/data/code hashes + fingerprint) |
| Readiness assessor (paper vs live-review contracts) | `src/chronos/research/readiness.py` — live always blocked; INSUFFICIENT_EVIDENCE first-class |
| Fail-closed data quality in campaign + runner | Blocking quality → exclude / raise; no stats on contaminated series |
| Research isolation extended | `manifest`, `readiness` in safety AST/`sys.modules` probes |
| Operator evidence contracts | `docs/RESEARCH_READINESS.md` |
| Tests | `tests/unit/test_live_block.py`, `test_research_manifest.py`, `tests/safety/test_research_cannot_transmit.py` |

**Not changed:** risk thresholds, holdout seal, selection criteria, broker credentials, live enablement flags.

---

## Verdict honesty (current evidence)

A synthetic campaign under the research policy yields:

- `overall_verdict`: **`insufficient_evidence`**
- `paper`: **`not_ready`**
- `live_review`: **`not_eligible`**
- `live_trading_blocked`: **`true`** / outcome **`LIVE TRADING BLOCKED`**

This matches the production research conclusion (zero candidates; trade-floor / sample honesty). Insufficient evidence was **not** engineered into PASS.

---

## Tests run

```text
Full suite:  1798 passed, 1 skipped (credential-gated IBKR smoke)
Safety + research focused: 152 passed
Live-block / isolation: 82 passed
Verdicts: 16 passed
Manifest/runner: 9 passed
Data fail-closed + holdout: 16 passed
```

No new failures. Only intentional skip: `CHRONOS_RUN_IBKR_SMOKE` opt-in.

---

## Exact next gate for paper trading

Do **not** start paper trading until:

1. At least one strategy×symbol walk-forward cell is **PASS** under frozen criteria (CI > 0, DSR ≥ 0.95, OOS trades ≥ 20).
2. Manifest binds `code_commit`, `policy_hash`, holdout-free `data_hashes`, `config_hash`.
3. Holdout remains sealed for automation; any holdout use is owner-mediated via C2 guardian.
4. Owner has reviewed `docs/STRATEGY_SELECTION.md` + `docs/RESEARCH_REPORT.md` and accepts zero-candidate honesty if still no PASS.

**Today:** next paper gate is **not met**. Continue research / data quality / strategy work under the same frozen criteria — or explicitly re-freeze criteria **before** new results if the owner changes the science bar.

**Live:** remains **LIVE TRADING BLOCKED**. Live *review* eligibility requires paper readiness + soak + trusted re-validation + shadow + owner-signed go-live gates (`docs/RESEARCH_READINESS.md`, `docs/GO_LIVE_CHECKLIST.md`).

---

## PR-ready status

- Dedicated branch only; **no merge, deploy, credential change, or order**.
- Suggested PR title: `feat: research readiness gate — LIVE TRADING BLOCKED + auditable manifests`
- Suggested body: link this report + `docs/RESEARCH_READINESS.md`.
