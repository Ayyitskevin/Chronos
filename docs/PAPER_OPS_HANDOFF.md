# Paper-ops milestone handoff

**Branch:** `feat/paper-ops-milestone`  
**Base:** `feat/research-readiness-gate` (includes LIVE TRADING BLOCKED + research readiness)  
**Date:** 2026-07-20  
**Scope:** Operational audit/replay layer for extended **paper** sessions.  
**Not in scope:** New strategies, backtest optimization, live enablement, credential changes.

---

## Architecture

New package: `src/chronos/paperops/`

| Module | Role |
|--------|------|
| `reasons.py` | Stable `DecisionKind` / `PaperReasonCode` / `DecisionOutcome` |
| `records.py` | `DecisionRecord` + secret-safe `sanitize_payload` |
| `ledger.py` | Append-only JSONL decision ledger, hash chain, fail-closed verify |
| `data_quality.py` | Paper quote/option quality gates (stale/missing/crossed/greeks/clock) |
| `controls.py` | Pure portfolio controls (halt, kill switch, exposure, concentration, daily loss, duplicate, cooldown) |
| `control_memory.py` | **Restart-safe** rehydration of fingerprints/cooldown from the decision ledger |
| `decision.py` | Combined pure evaluation: data health + controls + optional risk |
| `session.py` | `record_paper_decision` — rehydrate durable controls, evaluate, append under flock |
| `replay.py` | Deterministic re-eval of recorded `decision_inputs`; mismatch flags |
| `review.py` | Compact operator review (considered / rejected / acted / risk / data / anomalies) |

CLI (read-only; no order transmit):

```bash
python -m chronos.cli paperops review  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops replay  --ledger data/paper_decision_ledger.jsonl
python -m chronos.cli paperops verify  --ledger data/paper_decision_ledger.jsonl
```

### Design choices

- **Layer, don't fork:** Reuses risk/halt/kill-switch *concepts*; does not replace `chronos.orders` submission or `chronos.auditlog`. Decision ledger is schema-strict for replay.
- **Pure evaluation:** Broker I/O is out of the hot path so unit tests drive real shipped functions hermetically.
- **Only LIVE authorizes opens:** DEMO / DELAYED / SYNTHETIC / STALE / UNKNOWN are labeled and **non-authorizing**.
- **Live remains blocked:** Review and controls reaffirm `LIVE TRADING BLOCKED`; default settings stay non-transmitting.
- **Restart-safe controls:** `record_paper_decision` holds an exclusive lock across rehydrate → bind effective fingerprint → evaluate → append. Empty `order_fingerprint` is bound to a stable `order_identity_fingerprint` (excludes control-memory fields and wall clock) and that value is persisted for rehydration.
- **Single-writer serialization:** `DecisionLedger.append` / critical section use exclusive `fcntl` locks; concurrent same-fingerprint races allow at most one ALLOW.

### Data flow

```
PaperDecisionInput
    → rehydrate_control_memory(ledger)  # durable fingerprints/cooldown
    → apply_durable_control_memory
    → evaluate_paper_decision (data_quality + controls + optional risk)
    → DecisionEvent
    → DecisionLedger.append (fcntl exclusive lock + re-read head + hash chain)
    → OperatorReview / replay_ledger
```

---

## Tests run

```text
paperops unit suite: 40+ passed
live_block + research isolation (regression): green
Full suite: see verification capture {SCRATCH}/pytest.txt
```

Key test modules:

- `tests/unit/test_paperops_ledger.py` — provenance, secrets stripped, corrupt fail-closed
- `tests/unit/test_paperops_replay.py` — clean match, deliberate diverge, empty/corrupt fail-closed
- `tests/unit/test_paperops_data_quality.py` — stale/missing/crossed/greeks/clock/degraded labels
- `tests/unit/test_paperops_controls.py` — halt/kill/duplicate/cooldown/loss/exposure/malformed; live blocked
- `tests/unit/test_paperops_restart_and_race.py` — **restart rehydration** (duplicate + cooldown) and **concurrent multi-process append** under exclusive flock
- `tests/unit/test_paperops_review.py` — operator report content + corrupt fail-closed

---

## Known gaps

1. **Not wired into live `OrderManagementService` submit path yet** — vertical slice is callable (`record_paper_decision`) and CLI-reviewable; production order service still has its own risk/audit. Next integration: call `record_paper_decision` from paper propose/risk/submit boundaries without changing live branch.
2. **No Streamlit page** — CLI/review report is the operator surface for this milestone (soak report remains separate DB summary).
3. **Research readiness still NOT READY** — INSUFFICIENT_EVIDENCE remains; this milestone does not claim strategy edge or green-light paper *evaluation* of alpha.
4. **Fills** — `PAPER_FILL` kind exists; fill recording from broker callbacks is not auto-hooked in this slice (can append events manually/tests).

---

## Exact evidence still required before paper-trading *results* can be evaluated

Operational machinery ≠ scientific readiness. Before treating paper P&amp;L as strategy evidence:

1. Research readiness paper gate met (see `docs/RESEARCH_READINESS.md`): ≥1 walk-forward **PASS** under frozen criteria, sealed holdout, bound research manifest.
2. Paper session uses this decision ledger end-to-end (propose → risk → submit → fill all recorded).
3. `paperops replay` clean on the session ledger; `paperops review` shows no unresolved data-health anomalies on authorizing path.
4. Paper soak report + decision-ledger review agree on counts; kill-switch/halt exercised at least once in the session.
5. Owner accepts that DEMO/DELAYED data never authorized opens (or session was LIVE-quality paper quotes only).

Until (1), paper sessions are **machinery drills**, not edge evaluation.

---

## PR-ready status

- Isolated branch only; no merge, deploy, credential change, or live enablement.
- Suggested title: `feat: paper-ops decision ledger, replay, data guards, operator review`
- Preserve `feat/research-readiness-gate` commits in history.
