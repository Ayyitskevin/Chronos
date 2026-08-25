# QQQ SMA control preregistration — risky-change evaluation

Date: 2026-08-25

## Task contract

```yaml
plan_phase: 0
primary_kpi: net_edge_confidence
gate_advanced: none
files: exact QQQ control specification, typed blocked compiler, ADR/index/status docs, safety tests
verification: focused tests plus repository gates at the change commit
evidence_artifact: specs/qqq_sma_control_v1.json
owner_gate: required at merge; financial-risk semantics
open: certified data, holdout map, benchmark/cost/power/evaluator identities, TradingView parity, short evidence
```

## Assumptions selected before evidence

- Exact equality is informationless: it cannot initialize direction and cannot reverse an
  initialized state.
- A fresh next-session protected limit is a more conservative sizing reference than a
  favorable gap; quantity can shrink but never expand.
- One entry attempt per direction transition is preferable to a stale chase because the
  signal is daily and no later bar supplied a new entry event.
- Projected all-in round-trip cost capped at 10% of the applicable CVaR dollar budget is a
  deterministic small-account floor, not an estimate of expected edge.
- SMA-150/SMA-250 and the already reserved 1%/five-close alternatives are sufficient
  one-axis neighbors; combinations are forbidden to contain multiplicity.

These are design judgments. No market observation validates them.

## Live measurement boundary

No market dataset, account snapshot, broker connection, research registry, or holdout was
opened. Running an economic measurement here would violate the constitution's
freeze-before-read ordering. The appropriate evaluation is therefore structural:

- exact artifact bytes are SHA-256 pinned;
- the referenced constitution bytes are independently read and SHA-256 authenticated;
- the pinned artifact itself defines unit-exposure CVaR and the complete permitted-target-
  notional composition, so sizing semantics do not depend on unpinned ADR prose;
- any byte drift refuses before interpretation;
- all strategy, promotion, order, and live authority remain absent;
- the compiler reports every pre-data blocker and can never return executable/read-ready;
- import isolation prevents the specification loader from possessing data, holdout, trial,
  broker, order, or execution capabilities; and
- the primary and four one-axis cells are fixed and cannot be result-selected.

## Negative cases

The safety tests require that an authority mutation changes the digest and refuses, a
constitution mutation refuses even while the preregistration bytes remain exact, the
blocker set is complete, every compiled plan remains non-executable, and the loader has no
other `chronos.*` import at all. These cases guard the characteristic failure mode where a
correctly worded identity or blocker is structurally unable to block.

## Result

**Pragmatic partial, owner-gated.** The control rules and identity are exact and remain
blocked before data. No Phase 0 exit or economic evidence gate advances. Owner review of
the exact artifact/ADR is required at merge; a non-author review is separately required by
the repository protocol.

## Verification result

```text
PYTHONPATH=src .venv/bin/pytest -q \
  tests/safety/test_qqq_control_preregistration.py \
  tests/safety/test_qqq_v1_constitution.py
# 10 passed

.venv/bin/ruff check .
# All checks passed!
.venv/bin/ruff format --check .
# 532 files already formatted
.venv/bin/mypy src/chronos
# Success: no issues found in 288 source files
.venv/bin/mypy --strict worker
# Success: no issues found in 10 source files
PYTHONPATH=src .venv/bin/python -m pytest -q
# 4,001 passed / 1 skipped / 13 failed
```

The 13 failures are the same local Streamlit 1.62 relative-path failures measured at the
untouched base (which had 3,995 passes); this repaired change adds six passing safety tests
and no new failure class. `git diff --check` is clean. The independent reviewer must still
re-verify and withdraw the written HOLD before owner merge.
