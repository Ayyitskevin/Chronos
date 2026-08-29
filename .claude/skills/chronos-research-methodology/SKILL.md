---
name: chronos-research-methodology
description: >-
  Audit or run Chronos research evidence. Use for interpreting backtests or
  strategy claims, checking sample, overfit, holdout, trial, certification, or
  replay evidence, and planning or running research campaigns. Differentiator:
  classify the requested data touch and derive criteria, dataset identity,
  multiplicity, and authority from the checked-out revision before quoting a
  number or executing.
---

# Chronos research methodology

Chronos research is an evidence pipeline, not a favorable-metric hunt. Preserve
the distinction between a mechanism that exists, evidence produced through that
mechanism, and an owner-authorized promotion decision.

## Start with the request

Write one falsifiable question before inspecting results. State what observation
would reject it, the requested output, and the highest claim the requester hopes
to support.

Route adjacent work deliberately:

- Use `chronos-docs-map` to find canonical project documents.
- Use `chronos-priorities-and-roadmap` to choose work from the current plan.
- Use `chronos-change-control` before changing code, criteria, manifests, or
  durable research state.
- Use `chronos-validation-and-qa` for repository verification and release
  evidence.

Do not treat this skill as a cached statement of current results, thresholds,
inventory, or readiness.

## Establish the live revision

Run these before interpreting repository state:

```bash
git fetch origin --prune
git status --short --branch
git rev-parse HEAD
git ls-remote --symref origin HEAD
git log --oneline --branches --tags --not --remotes
git ls-files -- 'research/**' 'specs/**'
rg -n '^Status:' docs/adr
.venv/bin/python -m json.tool research/selection_manifest.json >/dev/null
.venv/bin/python -m json.tool research/data/raw/MANIFEST.json >/dev/null
.venv/bin/python -m json.tool research/data/history/HOLDOUTS.json >/dev/null
.venv/bin/python scripts/build_current_state.py --check
```

Record the revision and whether it matches the remote default branch. If the
working tree is dirty, identify which evidence may reflect uncommitted code.
Derive current state from tracked files and executable checks; never copy a
point-in-time count from this skill.

## Read the authority chain

Read the smallest authoritative set that answers the question:

- Governance and authority: `AGENTS.md`, `docs/AGENT_PROTOCOL.md`,
  `docs/VISION_COMPLETION_PLAN.md`, and `DECISIONS.md`.
- Registry and holdout design:
  `docs/adr/ADR-0013-experiment-registry-holdout-guardian.md`.
- Statistical and campaign proposals:
  `docs/adr/ADR-0014-walkforward-and-statistics.md` and
  `docs/adr/ADR-0015-revalidation-campaign.md`.
- Frozen selection and data identity: `research/selection_manifest.json`,
  `research/data/raw/MANIFEST.json`, and
  `research/data/history/HOLDOUTS.json`.
- Published interpretation: `docs/RESEARCH_REPORT.md`,
  `docs/STRATEGY_SELECTION.md`, `RISK_REGISTER.md`, and
  `docs/limitations.md`.

An ADR status is evidence about design authority. Source and tests are evidence
about implemented capability. Neither alone proves a research claim.

For implementation questions, inspect the current executable surfaces:

- Statistics: `src/chronos/research/stats.py`
- Legacy runners: `src/chronos/research/walkforward.py` and
  `src/chronos/research/campaign.py`
- Reproduction: `src/chronos/research/repro.py`
- Data identity and release: `src/chronos/research/certification.py`,
  `src/chronos/research/certified_data.py`, and
  `src/chronos/research/dataset_release.py`
- Trial orchestration: `src/chronos/research/trial_runner.py` and
  `src/chronos/research/five_tool_trials.py`
- Registry state: `src/chronos/registry/runs.py`,
  `src/chronos/registry/trials.py`, and
  `src/chronos/registry/holdout_guardian.py`

Then read the exercising tests, not only the implementation:

- `tests/unit/test_research_stats.py`
- `tests/unit/test_walkforward.py`
- `tests/unit/test_campaign.py`
- `tests/unit/test_research_repro.py`
- `tests/unit/test_registry_trials.py`
- `tests/unit/test_research_trial_runner.py`
- `tests/safety/test_research_isolation.py`

If authority, implementation, and tests disagree, stop and report the conflict.
Do not silently blend them.

## Classify effects before touching data

Classify the requested action and declare its state effects:

| Effect class | Typical purpose | State boundary |
|---|---|---|
| Inspect-only interpretation | Read existing evidence | No durable writes |
| Local replay probe | Reproduce an existing artifact | Writes only to a disposable output directory; does not register a new trial |
| Registered legacy run | Exercise walk-forward or campaign paths | May write a caller-selected legacy registry, halt state, or result artifacts |
| Canonical brokered trial | Perform selection-relevant evaluation | Writes canonical start and terminal events plus retained replay evidence |
| Certification or release | Bind or transform dataset identity | Writes certification, release, or exported data artifacts |
| Holdout consumption | Unmask reserved evidence | Owner-typed, single-use, durable burn before bytes are returned |

No-order is not no-mutation. A command can place no order and still change a
registry, halt file, output tree, certification record, or holdout state.

For anything beyond inspection, require explicit authorization and declared state effects.
Also require exact input and output locations and a rollback or disposable path.
Never infer permission to consume a holdout or rewrite durable evidence.

## Use the evidence ladder

Name the strongest supported rung:

1. Accepted design — the governing decision has the required status.
2. Implemented capability — code and exercising tests implement the mechanism.
3. Authenticated input identity — content, partition, and certification are bound.
4. Registered data touch — the evaluation is represented in the applicable registry.
5. Retained replay evidence — inputs, code/config identity, and outputs can be checked.
6. Statistical verdict — the frozen criteria were applied to the declared sample.
7. Promotion artifact — a governed selection or promotion record exists.

Mechanism is not evidence. Report the strongest supported rung and all missing
or contradictory lower rungs. Do not promote a claim beyond its evidence rung.

## Freeze before observation

Freeze before observation:

- question and hypothesis identity;
- dataset and partition identity;
- code and configuration identity;
- cost model and execution assumptions;
- trial family and multiplicity rule;
- sample units and statistical criteria;
- holdout boundary and promotion rule.

A criteria change is a separate owner-gated proposal, not a repair to an
unfavorable result. Failed holdout rejects remain failed. Treat seen or contaminated
data accordingly; documentary burn wins when ledgers conflict.
Absence from one ledger does not make data clean.

Never hand-edit a registry or holdout record. Never reinterpret `NO_TRADE` or
`INSUFFICIENT_EVIDENCE` as positive evidence.

## Trace both trial lifecycles

Chronos has distinct legacy and canonical paths. Trace both trial lifecycles
before reporting multiplicity.

### Legacy path

`register_run` appends an `experiment_run` through the caller-selected
legacy ledger. The legacy walk-forward path registers after the verdict. Its
count is ledger-local and can explain that specific runner, but it is not the
canonical multiplicity count.

### Canonical path

`CanonicalTrialRegistry` records `trial_started` before bytes become
available and then records a `trial_terminal` outcome. `FiveToolTrialBroker`
uses that lifecycle, retains attempt evidence, and exposes a
`multiplicity_snapshot` and `registered_trial_count` for its governed work.

Use canonical multiplicity for canonical claims. Never substitute a legacy
ledger count, campaign cell count, or replay count for it. Explain any mismatch
instead of choosing the more convenient number.

## Bind the data claim

For every result, identify:

- content digest and manifest identity;
- clean, seen, burned, development, validation, or holdout partition;
- certification and reconciliation status;
- calendar, symbol, frequency, and adjustment assumptions;
- missingness, exclusions, and transformations;
- exact code/config and cost-model identity.

Certification proves a declared data contract and identity. A dataset release
maps certified material into research-visible partitions. Neither establishes
strategy edge.

Holdout consumption must go through the guardian. It records the consume before
unmasking, making the burn durable even if the later analysis fails.

## Separate reproducibility from validity

Reproducibility is not validity. Deterministic replay can reproduce a flawed,
underpowered, contaminated, or irrelevant experiment. Replay comparison proves
identity or drift of an existing artifact; it does not prove edge and does not
register a new trial.

If a replay becomes selection-relevant—for example, a human uses it to choose
among hypotheses—classify and register that data touch through the applicable
trial lifecycle before reading selection data.

Derive every statistic, formula, threshold, sample floor, and verdict rule from
the checked-out implementation, accepted decisions, frozen manifest, and
exercising tests. Do not quote a remembered default.

## Produce one evidence packet

Return a compact packet. Use `missing` explicitly rather than omitting a field,
and preserve contradictory evidence instead of averaging it:

```yaml
question: one falsifiable question
revision: checked-out revision and remote relationship
claim_rung: strongest supported rung
criteria_identity: frozen rule or missing
hypothesis_identity: immutable identity or missing
dataset_identity: manifest and content identity
partition_status: clean, seen, burned, or contradictory
certification: status and evidence
code_config_identity: exact source and configuration identity
cost_model: assumptions and source
trial_lifecycle: legacy, canonical, replay-only, or missing
multiplicity_snapshot: applicable canonical snapshot or missing
sample_units: declared independent units and coverage
statistics: estimates, uncertainty, and criteria outcome
robustness: perturbations and negative controls
holdout_status: sealed, granted, burned, contaminated, or not applicable
replay_status: retained, reproduced, drifted, or missing
verdict: pass, reject, insufficient_evidence, no_trade, or not evaluated
promotion_status: governed artifact or not promoted
owner_gate: required, satisfied with evidence, or not applicable
residuals: limitations and unresolved contradictions
verification: rerunnable commands and observed outcomes
```

One packet answers one question. Split unrelated hypotheses instead of pooling
their evidence.

## Execute fail closed

Inspect command surfaces before using them:

```bash
.venv/bin/python -m chronos.cli research --help
.venv/bin/python -m chronos.cli registry verify
.venv/bin/python -m chronos.cli registry stats
.venv/bin/python -m chronos.cli holdout status
```

These status and verification commands are the read-first path. Tests are the smoke path
for mutation-capable research commands; do not discover behavior by running them
against durable state.

When execution is authorized:

- use a disposable output directory for probes;
- resolve and record all input, registry, halt, replay, and output paths;
- confirm which lifecycle will count the touch before bytes are read;
- preserve failed and partial artifacts with their terminal status;
- stop on identity, certification, registry, or holdout verification failure.

Never execute `holdout unlock` from this skill. Holdout access requires the
separate human/owner ceremony defined by current governance and the guardian.

## Common failure modes

- Quoting the best cell without the trial family and multiplicity snapshot.
- Treating an accepted design, implemented class, or passing test as observed
  strategy performance.
- Calling a CLI read-only because it places no orders while ignoring file writes.
- Registering after results when the claim requires canonical start-before-data
  semantics.
- Using reproducibility to stand in for sample independence or statistical power.
- Treating an undocumented data touch as clean because one registry lacks it.
- Rewriting criteria, exclusions, or costs after seeing the result.
- Equating a statistical pass with promotion or trading authority.

## Close the work

For edits to this skill, run:

```bash
.venv/bin/python -m pytest -q tests/unit/test_research_methodology_skill_contract.py
.venv/bin/python scripts/build_current_state.py --check
git diff --check
make gates
```

For an executed research question, add the changed-path focused tests and all
evidence-packet verification commands. Record skipped or
unavailable checks honestly. Moderate changes require non-author review against
the exact candidate revision; promotion and owner-gated actions require the
current project authority in addition to technical verification.
