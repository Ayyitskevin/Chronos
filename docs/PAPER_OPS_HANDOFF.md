# Paper-ops milestone handoff

**Branch:** `feat/paperops-runtime-fill` (stacks on `feat/paperops-pipeline-wire`)  
**Date:** 2026-07-20  
**Scope:** Production paperops path — runtime ledger bootstrap + fill recording.  
**Not in scope:** New strategies, live enablement, credentials, research PASS engineering.

---

## Architecture

### Package `src/chronos/paperops/`

| Module | Role |
|--------|------|
| `bootstrap.py` | **`open_paper_decision_ledger(settings)`** — paper-only ledger open (no orders imports) |
| `ledger.py` / `session.py` / `pipeline.py` | Hash-chained ledger, record_paper_decision, order-pipeline adapter |
| `decision.py` / `controls.py` / `control_memory.py` | Pure evaluation + restart-safe memory |
| `review.py` / `replay.py` | Operator review + deterministic replay |
| `reconcile.py` | **Soak DB ↔ decision-ledger** unified audit + honest mismatch flags |

### Production wiring

1. **Runtime bootstrap** (`src/chronos/runtime.py` → `_build_order_management`):
   - Calls `open_paper_decision_ledger(settings)`
   - PAPER + `enable_paper_decision_ledger=true` → injects `DecisionLedger` into `OrderManagementService`
   - LIVE environment → always `None` (never auto-record on live capital path)

2. **OrderManagementService** (optional `decision_ledger`):
   - **propose** → `PipelineRecorder.record_propose`
   - **submit** → `record_submit`
   - Binds **fill audit** on the tracker when ledger enabled

3. **OrderTracker.ingest**:
   - On successful transition to `PARTIALLY_FILLED` or `FILLED`, invokes fill audit sink → `record_fill`

### Settings

| Setting | Default | Notes |
|---------|---------|--------|
| `ENABLE_PAPER_DECISION_LEDGER` | `true` | Paper only |
| `PAPER_DECISION_LEDGER_FILE` | `data/paper_decision_ledger.jsonl` | Path under data/ |

### Import safety

- Do **not** eager-import `pipeline` from `chronos.paperops` package `__init__`.
- Service lazy-imports `PipelineRecorder` only when a ledger is injected.
- Cold `import chronos.paperops` and CLI `paperops verify|review|replay` must work.

CLI:

```bash
python -m chronos.cli paperops review  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops replay  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops verify  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops audit   --ledger data/paper_decision_ledger.jsonl \
  --database sqlite:///data/chronos.db
```

`paperops audit` reconciles SQL soak metrics (`build_soak_report`) with ledger
pipeline stages (propose/submit/fill). Corrupt/missing ledger fails closed for
the ledger half; mismatch flags are explicit (not forced equality).

---

## Tests

| Module | Covers |
|--------|--------|
| `tests/unit/test_paperops_bootstrap.py` | Paper opens ledger; LIVE never; disabled flag |
| `tests/integration/test_runtime_decision_ledger.py` | Runtime-like OMS composition + propose/submit + fill/partial |
| `tests/unit/test_tracker_fill_audit.py` | Fill sink on PARTIAL/FILLED; late bind |
| `tests/integration/test_pipeline_decision_ledger.py` | Service injection path (prior slice) |
| `tests/safety/test_paperops_cold_import.py` | Subprocess cold import + CLI |
| `tests/unit/test_paperops_reconcile.py` | Soak↔ledger match, missing/corrupt fail-closed, mismatch flags |

---

## Known gaps

1. Full `build_runtime()` still needs a broker connection — hermetic tests mirror `_build_order_management` ledger composition rather than spinning TWS.
2. No Streamlit surface for the decision ledger (CLI review/audit remains the operator UI).
3. Research readiness still **INSUFFICIENT_EVIDENCE** — ops ledger does not authorize treating paper P&L as edge proof.
4. Demo/LIVE modes do not auto-enable the paper ledger (by design for LIVE).
5. Soak↔ledger reconcile is honest multi-plane audit: perfect 1:1 event equality is not required; flags surface missing halves.

---

## Exact evidence still required before paper P&L evaluation

1. Research paper gate met (`docs/RESEARCH_READINESS.md`): ≥1 walk-forward PASS, sealed holdout, bound manifest.
2. Paper session with ledger enabled end-to-end (runtime bootstrap) including fills.
3. `paperops verify` green; `review` shows propose/submit/fill; no unauthorized data-health labels on opens.
4. Soak report + ledger counts agree; halt/kill-switch exercised once.
5. Owner accepts only LIVE-quality paper quotes authorized opens.

Until (1), paper sessions are **machinery drills**, not edge evaluation.

---

## PR-ready

Isolated branch only — no merge, deploy, credentials, or live enablement.
