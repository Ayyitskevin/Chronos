---
name: chronos-change-control
description: >
  Classify, authorize, review, and merge Chronos changes. Use before changing safety,
  authority, money, risk limits, configuration, schemas, migrations, promotion criteria,
  roadmap scope, ADRs, decisions, or document precedence; when deciding whether Kevin's
  approval is required; when two repository authorities conflict; or when wording claims
  something is done, validated, mitigated, or closed. Also use for the repository task
  contract, branch/PR protocol, evidence requirements, and ID allocation.
---

# Chronos change control

Use this skill to determine what may change, who may approve it, and what evidence lets it
merge. It is a procedure, not a snapshot. Derive branch names, gate steps, migration state,
IDs, and measured results from the live repository every session.

## Authority map

Read the current sources before acting:

1. `AGENTS.md` defines non-negotiable safety, scope, precedence, and approval boundaries.
2. `docs/AGENT_PROTOCOL.md` defines branches, PRs, gates, review, merges, and ID allocation.
3. `DECISIONS.md` and relevant files under `docs/adr/` define accepted intent and
   architecture.
4. `docs/safety.md`, `docs/limitations.md`, and `RISK_REGISTER.md` define controls,
   capability boundaries, and residual risk.
5. `docs/VISION_COMPLETION_PLAN.md` defines roadmap order, scorecards, acceptance gates,
   owner gates, and the task contract.
6. Historical plans, issue descriptions, chat, and handoffs provide context only.

Current executable facts and unresolved safety or security defects outrank prose. Explicit
owner direction remains bounded by safety and human-approval rules. When authorities
conflict, stop, name both sides, apply the precedence in `AGENTS.md`, and surface the
conflict. Never average incompatible instructions or weaken code to rescue a prose claim.

## Fast decision path

1. Classify the change by its effects, not its filename or apparent size.
2. State the task contract from `docs/VISION_COMPLETION_PLAN.md` before editing.
3. Derive the live default branch, check for unpublished work, and measure a clean
   `make gates` baseline.
4. Work on one logical change in the owning seat's checkout and one PR.
5. Run the live gate, obtain the review required by the change's blast radius, and close
   every HOLD in writing.
6. Merge only when the contract's authority permits it, then verify the exact default-branch
   result and its CI run.

When uncertain between classifications, take the stricter classification and record the
uncertainty in `open:`.

## Classify by effect

### Owner-independent

The owning seat may implement and merge these after the required checks and review:

- A factual documentation correction backed by rerunnable evidence.
- A test or code change that does not alter safety, authority, money, credentials,
  promotion, accepted architecture, or live state.
- An offline schema or migration implementation that does not mutate live data and does
  not change safety- or authority-bearing durable semantics.

Owner-independent does not mean review-free. Apply the review tier in
`docs/AGENT_PROTOCOL.md`, keep the change to one revertable unit, and state
`owner_gate: not applicable`.

### Owner-gated

An agent may prepare a proposal and evidence, but Kevin must approve the substance and
merge an owner-gated PR. Mark `owner_gate: required` until that approval is recorded.
Owner-gated effects include:

- Money-critical, live-broker, credential, authentication, or security-sensitive code.
- Adding or modifying a safety mechanism, refusal, execution boundary, kill behavior,
  reconciliation rule, idempotency rule, or authority-bearing durable state.
- Capital, loss, drawdown, CVaR, concentration, turnover, exposure, and risk-limit changes.
- Holdout unlock, paper mandate, canary authorization, live promotion, cap increases, or
  mutation of live schema/data.
- Broker/account resolution, licensing, legal, tax, regulatory, or account-structure acts.

A green gate proves consistency, not permission. A test, backtest, backup, or agent verdict
cannot satisfy an owner gate. Never activate credentials, flags, mandates, promotions, or
live operations as part of a proposal.

### Proposal-only governance

Agents may draft but may not approve changes to:

- Product scope, either 10/10 definition, KPI definitions, promotion thresholds, or frozen
  criteria.
- Owner gates, document precedence, evidence-reset rules, or accepted authority.
- Mandate scope, order forms, network channels, autonomous ceilings, or other expansions of
  what the system is allowed to do.

Record an approved architectural or authority decision in a new ADR and index it in
`DECISIONS.md`. Annotate superseded text in place so the old rule and its replacement remain
legible. A factual status correction alone does not create authority and does not require an
ADR.

## State the task contract

Copy the canonical structure from `docs/VISION_COMPLETION_PLAN.md` rather than maintaining a
second template. Every implementation and review states all of these fields:

```yaml
plan_phase: <0-5>
primary_kpi: safety_integrity | broker_truth | net_edge_confidence
gate_advanced: <exact acceptance gate or "none">
files: <declared working set>
verification: <rerunnable commands and observed result>
evidence_artifact: <path or "none; code-only change">
owner_gate: <required, satisfied, or not applicable>
open: <remaining risks, conflicts, and deferred work>
```

Declare `files:` before editing. Stop and restate scope if the working set must expand.
`gate_advanced: none` is correct when a change improves maintainability without producing
the exact evidence an acceptance gate requires. Do not infer plan completion from coverage.

## Preflight before editing

Run the repository protocol from the owning seat's clean checkout:

```bash
git fetch origin --prune
git log --oneline --branches --tags --not --remotes
git ls-remote --symref origin HEAD
git checkout -B <seat>/<topic> origin/<derived-default>
make gates
```

- Investigate unpublished commits that overlap the task before rebuilding work. Do not edit,
  switch, or clean another seat's checkout.
- Derive the PR base from the remote symbolic HEAD. Do not copy a branch name from this skill,
  a handoff, or a previous session.
- Record the measured gate result as the baseline. A result copied from CI, documentation, or
  another commit is not your measurement.
- Read the relevant ADRs, exports, immediate callers, tests, and shared utilities before
  changing code.

## Preserve frozen evidence

Freeze statistical, operational, and financial criteria before observing the evidence they
judge. If evidence fails a frozen criterion, reject the candidate or return NO_TRADE; do not
edit the criterion to manufacture progress. A proposed criterion change is a separate,
owner-gated governance change and cannot retroactively judge evidence already observed.

Keep research identities content-addressed where the governing ADR or plan requires it.
Never reuse a holdout, promotion artifact, or evidence identity under altered assumptions.

## Change discipline

- Make the smallest change that satisfies the declared contract. One logical item per PR.
- Preserve fail-closed and deny-by-default behavior. Missing, stale, ambiguous, or
  uncertifiable evidence must not widen authority.
- Tests, CI, and development paths must not place orders or contact a live broker unless an
  explicitly authorized read-only test says so.
- Treat economic-looking fields as mechanically enforced, explicitly advisory, or forbidden.
  Inert authority, risk, exit, or protection fields are release blockers.
- Update `RISK_REGISTER.md` when a control or residual risk changes. Do not mark a risk CLOSED
  merely because its mitigation has a test.
- Correct factual prose toward the weaker true claim. Do not add code just to preserve an
  overstated document claim.

### ADRs and decision IDs

Use a new ADR for a new architectural or authority decision. Use a `DECISIONS.md` row alone
only when the accepted decision is consequential but does not need a full architectural
record. Use a factual doc edit for current-state corrections that change no authority.

Allocate identifiers from the live files in the same session and PR that claims them:

```bash
grep -oE '^\| D-[0-9]+' DECISIONS.md | grep -oE '[0-9]+' | sort -n | tail -1
ls docs/adr/ | grep -oE 'ADR-[0-9]{4}' | sort | tail -1
grep -oE '^\| R-[0-9]+' RISK_REGISTER.md | grep -oE '[0-9]+' | sort -n | tail -1
```

If another PR claims the same next ID, the later unmerged PR renumbers at rebase. Never
renumber a merged identifier because external citations may already depend on it.

### Migrations

Derive migration state instead of naming a revision in prose:

```bash
.venv/bin/alembic heads
.venv/bin/alembic history
```

Read `src/chronos/persistence/migrations/versions/` and
`tests/integration/test_migrations.py`. Verify the supported upgrade paths, model-table
completeness, and installed-artifact migration behavior through the repository gate. A
migration file is not authorization to apply it to live data.

## Gate and review

CI is the source of truth for the gate. `.github/workflows/ci.yml` defines the required
steps; `Makefile` provides the local mirror. Run:

```bash
make gates
grep -E '^\s+run:' .github/workflows/ci.yml
```

If those disagree, fix or surface the mismatch as its own work item. Do not silently select
the easier definition. Record the commands and measured result at the exact candidate SHA.

Apply the review tier in `docs/AGENT_PROTOCOL.md`:

- Trivial factual/cosmetic changes may be self-verified.
- Moderate code, test, refactor, or procedural changes require one non-author seat.
- Owner-gated changes require non-author review plus Kevin's approval.

The reviewer uses a fresh worktree and verifies the exact candidate:

```bash
git worktree add <temporary-review-path> <candidate-sha>
git -C <temporary-review-path> rev-parse HEAD
```

A PASS names the SHA, reviewed scope, and positive evidence. A HOLD names the blocker and
remains open until the same holder re-verifies the fix and withdraws it in writing. A silent,
quota-limited, or empty reviewer response is not a PASS.

## Merge and post-merge proof

For an owner-independent PR, the owning seat may merge after required review and CI succeed.
For an owner-gated PR, the owner merges; the author stops with the proposal, evidence, and
`owner_gate: required`.

After merge, derive the default branch again, fetch it, and verify the integration. For a
merge or rebase strategy that preserves the candidate commit, prove ancestry:

```bash
git fetch origin --prune
git merge-base --is-ancestor <candidate-sha> origin/<derived-default>
```

A squash merge creates a new commit, so candidate ancestry is expected to fail. Instead,
compare the reviewed changed paths byte-for-byte between the candidate and exact default tip,
confirm the PR's changed-file set, record the new commit SHA, and require CI at that SHA. In
all cases, spot-check claimed files on the default branch. For table-document edits, scan for
duplicate IDs.

## Claim discipline

- `MITIGATED` is not `CLOSED`. State the residual that remains.
- `contract` is not `enforced`. Name the executable path and exercising test before using
  enforced language.
- `done`, `working`, `validated`, and plan-score claims require a named evidence artifact.
- A merged badge proves only that GitHub performed a merge against some base. Verify the base,
  content, exact commit, and CI.
- Point-in-time findings from docs, issues, reviews, and handoffs are hypotheses until
  reverified against the current commit.

## Known pitfalls

- **Green gate as permission:** a green gate does not satisfy an owner gate or prove economic
  validity.
- **Stacked PRs:** merged child badges can strand work off the default branch. Do not stack
  without explicit owner acknowledgment and an ancestry plan.
- **Squash ancestry:** the reviewed candidate is not an ancestor after squash. Use changed-path
  content equality and exact-main CI instead of claiming ancestry.
- **Unclosed HOLD:** a fix does not retire a HOLD; only the holder's written re-verification
  does.
- **Wrong-tree review:** switching another seat's checkout mutates its state and invalidates
  measurements. Use `git worktree add` from your own clone.
- **Cached facts:** copied branch names, counts, migration revisions, IDs, line references,
  and status snapshots decay. Keep the command and derive the value.
- **Historical authority:** an old plan, handoff, issue, or observed Git habit cannot override
  the current contract.
- **Scope by filename:** a one-line configuration or documentation edit can still change
  money, authority, security, or promotion and therefore be owner-gated.

## Route elsewhere

- Work selection and plan sequencing: `chronos-priorities-and-roadmap`.
- Test design and proof depth: `chronos-validation-and-qa`.
- Runtime operation and incident commands: `chronos-run-and-operate`.
- Document trust and contradiction inventory: `chronos-docs-map`.
- Statistical methods and holdout mechanics: `chronos-research-methodology`.
- Autonomy objects and mandate mechanics: `chronos-autonomy-and-mandates`.

## Maintenance

Keep this skill procedural. Before editing it, rerun the source-derivation commands above and
the focused contract test. If a source disagrees with this skill, the source wins. Update the
procedure and test together; do not append a dated correction, copied result, or historical
snapshot.
