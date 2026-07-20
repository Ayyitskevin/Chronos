# Paper-ops milestone handoff

**Branch:** `feat/paperops-pipeline-wire` (stacks on `feat/paper-ops-milestone`)  
**Base:** paperops package + research readiness gate  
**Date:** 2026-07-20  
**Scope:** Operational audit/replay layer for extended **paper** sessions, now **wired into the real order pipeline**.  
**Not in scope:** New strategies, backtest optimization, live enablement, credential changes.

---

## Architecture

### Package `src/chronos/paperops/`

| Module | Role |
|--------|------|
| `reasons.py` | Stable `DecisionKind` / `PaperReasonCode` / `DecisionOutcome` |
| `records.py` | `DecisionRecord` + secret-safe `sanitize_payload` |
| `ledger.py` | Append-only JSONL decision ledger, hash chain, fail-closed verify |
| `data_quality.py` | Paper quote/option quality gates (stale/missing/crossed/greeks/clock) |
| `controls.py` | Pure portfolio controls (halt, kill switch, exposure, concentration, daily loss, duplicate, cooldown) |
| `control_memory.py` | Restart-safe rehydration of fingerprints/cooldown from the ledger |
| `decision.py` | Combined pure evaluation + stable `order_identity_fingerprint` |
| `session.py` | `record_paper_decision` — lock across rehydrate → evaluate → append |
| `pipeline.py` | **Thin adapter:** order intent/risk/submit → ledger (this slice) |
| `replay.py` | Deterministic re-eval of recorded `decision_inputs` |
| `review.py` | Compact operator review |

### Order pipeline wiring

`OrderManagementService` accepts optional `decision_ledger: DecisionLedger | None`:

- **`None` (default):** no recording — backward compatible with all existing callers.
- **Injected ledger:** audited mode — corrupt ledger fails closed on the recording call.
  - **propose:** after risk evaluate → `PipelineRecorder.record_propose` (risk PASS/FAIL → allow/deny)
  - **submit:** after boundary → `record_submit` (submitted or refusal with stable reason code)
  - Lifecycle remains authoritative on the order service; paperops is observational audit.

**Import rule (cycle-safe):** do not eager-import `chronos.paperops.pipeline` from
`chronos.paperops` package `__init__`. The service lazy-imports the adapter only when a
ledger is injected. Cold `import chronos.paperops` and CLI `paperops verify|review|replay`
must work in a fresh interpreter (`tests/safety/test_paperops_cold_import.py`).

`strategy_version` is explicitly **`unknown`** (wheel intents have no strategy version field).  
`config_hash` = secret-free `settings_config_hash(settings)`.  
`order_fingerprint` = `intent_id` for pipeline stages.

CLI (read-only; no order transmit):

```bash
python -m chronos.cli paperops review  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops replay  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops verify  --ledger data/paper_decision_ledger.jsonl
```

### Design choices

- **Layer, don't fork:** Does not replace `chronos.orders` submission or `chronos.auditlog`.
- **Live remains blocked:** Default/paper settings still `LIVE TRADING BLOCKED`; wiring never sets live flags.
- **Restart-safe + concurrent:** exclusive flock; empty-fp stable identity; same-fp race ≤1 ALLOW.
- **No secrets:** pipeline extras never include account ids, tokens, or credentials.

### Data flow

```
OrderManagementService.propose
    → risk.evaluate (authoritative)
    → PipelineRecorder.record_propose → DecisionLedger
OrderManagementService.submit
    → OrderSubmissionBoundary.submit (authoritative)
    → PipelineRecorder.record_submit → DecisionLedger
Operator: paperops verify | review | replay
```

---

## Tests run

```text
tests/integration/test_pipeline_decision_ledger.py — real service path + ledger
tests/integration/test_order_pipeline.py — regression (ledger optional/off)
paperops unit + restart/race + live_block
Full suite: see {SCRATCH}/pytest.txt
```

Key modules:

- `tests/integration/test_pipeline_decision_ledger.py` — happy path propose+submit rows; risk deny; submit refusal; review/verify; live blocked; secret scan
- `tests/unit/test_paperops_*.py` — ledger/replay/data quality/controls/restart-race
- `tests/safety/test_paperops_isolation.py`, `tests/unit/test_live_block.py`

---

## Known gaps

1. **Production wiring of ledger path** — service accepts injection; HTTP/backend bootstrap may still pass `None` until operator configures a ledger path for paper sessions.
2. **Fill auto-hook** — `PipelineRecorder.record_fill` exists; tracker/callback auto-call not wired (manual/tests only).
3. **No Streamlit page** — CLI review remains the operator surface.
4. **Research readiness still NOT READY** — INSUFFICIENT_EVIDENCE; ledger wiring is operational, not scientific readiness.
5. **Replay of submit stage** — submit rows are stage events; full re-eval match is strongest on propose rows.

---

## Exact evidence still required before paper-trading *results* can be evaluated

Operational machinery ≠ scientific readiness. Before treating paper P&amp;L as strategy evidence:

1. Research readiness paper gate met (`docs/RESEARCH_READINESS.md`): ≥1 walk-forward **PASS**, sealed holdout, bound research manifest.
2. Paper session runs with `decision_ledger` injected end-to-end (propose → risk → submit → fill).
3. `paperops verify` green; `paperops review` shows considered/rejected/acted; no unresolved data-health anomalies on authorizing path.
4. Paper soak report + decision-ledger review agree; kill-switch/halt exercised once.
5. Owner accepts LIVE-quality paper quotes only authorized opens (DEMO/DELAYED never authorize).

Until (1), paper sessions are **machinery drills**, not edge evaluation.

---

## PR-ready status

- Isolated branch only; no merge, deploy, credential change, or live enablement.
- Suggested title: `feat: wire paperops decision ledger into paper order pipeline`
