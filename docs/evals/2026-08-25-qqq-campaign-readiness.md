# QQQ campaign readiness — risky-change evaluation

Date: 2026-08-25

## Task contract

```yaml
plan_phase: 0; Phase 3 prerequisite
primary_kpi: net_edge_confidence
gate_advanced: none
files: one content-addressed readiness spec, one authentication-only compiler, seam-level safety tests, owner checklist, ADR/index/risk/status docs
verification: red-green focused tests, realistic artifact mutation matrix, static isolation probes, full repository gates, independent non-author review
evidence_artifact: specs/qqq_campaign_readiness_v1.json and this evaluation
owner_gate: required at merge; financial research identity and owner-input classification
open: certified data and attestation, holdout map, benchmark/cost/power/evaluator/parity identities, base Five-Tool blockers, short evidence, real PAPER activation evidence
```

## Assumptions frozen before implementation

- Readiness composes the existing frozen identities; it does not edit their economics or
  thresholds.
- Owner actions, Chronos build work, unavailable short evidence, and deferred activation
  work must remain distinguishable.
- Implemented inert PAPER code is not real PAPER evidence and is not broker protection.
- The six-symbol QQQ release and seven-symbol base Five-Tool intake are separate identities.
- Any referenced artifact drift, authority expansion, or blocker deletion must refuse.

## Primary-source check

IBKR documents that historical trades are filtered and that `TRADES` bars are adjusted for
splits but not dividends. It also documents request pacing, unavailable histories, and
possible throttling/disconnection for large requests. Those facts support independent
corporate-action attestation and certification; they do not justify treating a broker export
as self-certifying. The Deflated Sharpe Ratio supports preserving trial/multiplicity identity.
No source was used to weaken or tune a frozen threshold.

Sources: [historical bars](https://interactivebrokers.github.io/tws-api/historical_bars.html),
[historical limitations](https://interactivebrokers.github.io/tws-api/historical_limitations.html),
and [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

## Measurement boundary

No broker credential, account, gateway, market-data byte, holdout, trial, order, funding,
PAPER runtime, or promotion artifact was accessed. These are real repository-artifact and
mutation measurements, not a market-distribution or broker-behavior test.

## Red-green and realistic mutation evidence

The first test failed because the public module did not exist. Subsequent red tests exposed
and then closed three aggregation hazards: referenced artifact overrides initially skipped
the constitution/control/candidate copies; child authority changes initially passed; and a
child blocker deletion initially passed.

The final focused file contains 15 exercised cases:

- exact blocked posture, exact five-artifact identity, typed responsibility ledger, and the
  six-symbol/seven-symbol data boundary (4);
- one-byte drift in each real referenced artifact copy (5);
- authority escalation in each inherited compiler (2);
- blocker deletion in each inherited compiler (2);
- readiness-document authority drift before interpretation (1); and
- direct AST plus fresh-process transitive import isolation (1).

The companion QQQ control and candidate tests add 12 existing cases, for 27 focused passes.

## Verification result

Preflight at exact `origin/main` `08bb98e88a15b3746ce773ef64ca0efeeb7dc70d`:

```text
make gates
# ruff: All checks passed
# format: 539 files already formatted
# mypy: 292 Chronos source files; worker strict: 10 source files
# pytest: 4,067 passed / 1 skipped / 13 failed
```

The 13 failures are the inherited Streamlit 1.62 `AppTest.from_file` relative-path cluster in
`test_backend_ui_pages`, `test_monitor_streamlit_app`, and `test_streamlit_app`. No byte had
been changed when this baseline was measured.

Focused implementation evidence:

```text
.venv/bin/ruff check src/chronos/research/qqq_campaign_readiness.py \
  tests/safety/test_qqq_campaign_readiness.py
# All checks passed

.venv/bin/mypy src/chronos/research/qqq_campaign_readiness.py
# Success: no issues found in 1 source file

PYTHONPATH=src .venv/bin/pytest -q tests/safety/test_qqq_campaign_readiness.py \
  tests/safety/test_qqq_control_preregistration.py \
  tests/safety/test_qqq_confluence_candidate.py
# 27 passed
```

Full post-change evidence:

```text
make gates
# ruff: All checks passed
# format: 541 files already formatted
# mypy: 293 Chronos source files; worker strict: 10 source files
# pytest: 4,082 passed / 1 skipped / 13 failed
```

The 13 failures are exactly the same inherited Streamlit 1.62 relative-path cases measured
at preflight; this change adds 15 passing cases and no new failure. The exact-head independent
review verdict is recorded on the pull request and in the fleet handoff so the reviewed commit
does not move merely to copy its own review result into this file.

## Result

**Pragmatic partial, owner-gated.** The repository now has a deterministic, fail-closed
answer to what is locked and what remains. No evidence, trial, PAPER, or execution gate is
advanced. Owner merge approval and an exact-head non-author review remain required.
