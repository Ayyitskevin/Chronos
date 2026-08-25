# QQQ Five-Tool candidate overlay — risky-change evaluation

Date: 2026-08-25

## Task contract

```yaml
plan_phase: 0
primary_kpi: net_edge_confidence
gate_advanced: none
files: QQQ Five-Tool overlay, typed blocked compiler, ADR/index/status docs, safety tests
verification: focused source/contract/replay tests plus repository gates at the change commit
evidence_artifact: specs/qqq_five_tool_candidate_v1.json
owner_gate: required at merge; financial-risk and price-domain semantics
open: base campaign bindings, certified data, holdout, identities, TradingView parity, paper lifecycle, short evidence
```

## Assumptions selected before evidence

- One causal, point-in-time total-return OHLC domain should feed all price geometry; mixing
  raw EMA, adjusted ATR, and synthetic AVWAP would create incoherent levels.
- Rebasing the decision history so the current adjusted close equals the current raw close
  preserves causal corporate-action treatment while leaving the produced level in current
  tradable units.
- Historical volume should change only for causal splits, not cash distributions.
- A corporate action between signal and next-session handoff invalidates the entry; rolling
  a signal-time level through an overnight factor would be an untested second model.
- Source defaults remain exact, including the master short switch being off. The enabled
  short submodules do not imply executable or research short authority.
- Native stop-risk sizing remains the strategy engine; CVaR and owner limits remain outer
  vetoes/caps rather than being relabeled as source behavior.

These are design judgments, not observed evidence of edge or execution quality.

## Live measurement boundary

No market dataset, broker, account, registry, or holdout was opened. Running an economic
measurement before the overlay, catalog, costs, power, and evaluation identities are frozen
would invert the required evidence order. Structural evaluation therefore checks:

- exact overlay bytes and all base Pine/contract/semantic/campaign identities;
- independent authentication of the referenced constitution bytes;
- all 219 source defaults with no override and the critical effective daily settings;
- explicit decision-versus-raw-execution price domains and corporate-action invalidation;
- exact unit-exposure CVaR, 1% native risk, 1.5% CVaR, 2% loss halt, 10% drawdown,
  gross/leverage, downward-only handoff quantity, and no-upsize relationships;
- the complete Confluence stop/target/breakeven/runner/exit stack; and
- a compiler that can return only blockers and imports no data, holdout, trial, broker,
  order, execution, or promotion capability.

## Negative cases

The safety tests require any overlay byte mutation to refuse before interpretation, a
constitution-only mutation to refuse, every pre-data blocker to survive, zero
authority/trials, and an exact single-module Chronos import allowlist. Base identity is
recomputed from live repository bytes, so a stale overlay cannot authenticate a changed
constitution, Pine source, input contract, semantic contract, or campaign manifest.

## Result

**Pragmatic partial, owner-gated.** The QQQ candidate translation is exact and blocked.
No Phase 0 exit or economic evidence gate advances. Owner review of the exact overlay/ADR
is required at merge, and a non-author PASS/HOLD review remains required separately.

## Verification result

```text
PYTHONPATH=src .venv/bin/pytest -q tests/safety/test_qqq_confluence_candidate.py tests/unit/test_five_tool_contract.py tests/unit/test_five_tool_replay.py
# 42 passed

.venv/bin/ruff check .
# All checks passed!
.venv/bin/ruff format --check .
# 534 files already formatted
.venv/bin/mypy src/chronos
# Success: no issues found in 289 source files
.venv/bin/mypy --strict worker
# Success: no issues found in 10 source files
PYTHONPATH=src .venv/bin/python -m pytest -q
# 4,007 passed / 1 skipped / 13 failed
```

The 13 failures are the same local Streamlit 1.62 relative-path failures measured at the
post-merge `origin/main` baseline (4,001 passes). This candidate adds six passing safety tests and no
new failure class. `git diff --check` is clean.
