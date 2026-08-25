# QQQ PAPER position management — risky-change evaluation

Date: 2026-08-25

## Task contract

```yaml
plan_phase: 1
primary_kpi: safety_integrity
gate_advanced: no promotion rung; adds an inert executable specification for part of the position lifecycle
files: proposal-only management module, safety tests, ADR/index/risk/status docs
verification: focused lifecycle tests, static gates, full repository gates, independent non-author review
evidence_artifact: hash-chained position-management stream schema and exact management-policy digest
owner_gate: required at merge; financial-risk and future authority semantics
open: authenticated PAPER adapter, one-order/one-stream identity, trusted management queue, persistent broker protection, runtime wiring, real PAPER evidence
```

## Assumptions frozen before broker or market evidence

- Actual opening fills, not intended quantity, define the managed position and its leg
  geometry.
- Native stop risk remains 1%/USD 30 at the capped reference base; 1.5%/USD 45 CVaR is an
  independent outer cap. The 2%/USD 60 session limit and 10% drawdown are flattening circuit
  breakers, not per-trade sizing.
- Breakeven requires a complete actual T1 fill. A proposal or partial fill is insufficient.
- The runner uses the selected 22-session high minus 3 ATR after 1R and may only tighten.
- Adverse confirmed regime remains active; long AVWAP, neutral, SMA, and time exits remain
  inactive under the exact source defaults.
- Recording broker truth grants no authority. Every risk reduction must re-enter the
  existing supervisor and order pipeline.

These are design and safety judgments, not evidence that the strategy has edge or that the
broker path will execute them correctly.

## Measurement boundary

No broker, account, market dataset, registry trial, or holdout was opened. The risky-change
measurement requirement is deliberately limited to deterministic and structural evidence
because measuring real PAPER performance before the adapter, queue identity, persistent
protection, and activation contract are frozen would cross the preregistration boundary.

The tests therefore measure that:

- exact candidate/policy/risk/leg identities derive from actual fills;
- over-limit exposure is recorded and latched to flatten rather than hidden;
- fresh LIVE-quality, account-matching, broker-quantity-matching observations are required;
- stops only tighten; T1 breakeven follows complete actual fills; T2 and runner quantities
  remain exact across partial fills, and one broker execution identity cannot reduce two
  directives;
- session loss, drawdown, initial/breakeven/trailing stops, opposite regime, and targets
  have deterministic precedence;
- replay, temporal disorder, ambiguous sends, semantic tampering, and reconciliation
  disagreement fail closed;
- no production module imports the capability, no second broker/order path exists, and no
  parallel authority grant is defined; and
- the known proposal-queue economic-identity collision remains an explicit activation
  blocker.

## Result

**Pragmatic partial, owner-gated.** The lifecycle state machine is a durable, default-off
proposal engine. It closes neither Phase 1 nor PAPER readiness. It cannot protect a position
while Chronos is disconnected or stopped, and it cannot send an order. Owner merge approval
and a separate non-author review are required; activation is out of scope.

## Verification result

```text
PYTHONPATH=src .venv/bin/pytest -q tests/safety/test_paper_position_management.py
# 37 passed

make gates
# ruff: All checks passed
# format: 536 files already formatted
# mypy: 290 source files; worker strict: 10 source files
# pytest: 4,044 passed / 1 skipped / 13 failed

git diff --check
# clean
```

The isolated merged-main worktree at `103d4e642f47b05e98536e085b4d8c1727137d31`
collects 4,021 tests; this branch collects 4,058, exactly 37 more. Its static baseline is
clean (534 formatted files, 289 source files, 10 worker files). The baseline full invocation
reported 4,006 passed / 1 skipped / 14 failed: the same 13 Streamlit 1.62 relative-path
failures plus one concurrent-ledger hard-link transient. That isolated transient passed on
immediate rerun. The branch full invocation reported 4,044 passed / 1 skipped / the same 13
Streamlit failures, with no new failure class.
