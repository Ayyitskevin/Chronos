---
name: chronos-change-control
description: >
  Chronos governance authority: how any change is classified, gated, and reviewed. Load
  this BEFORE editing when you are asking "can I change this", "do I need owner approval",
  "who decides", "is this in scope", "may I widen/raise/enable this limit, mandate, order
  form, ceiling, or network channel", "how do I write an ADR", "how do I supersede a
  decision", or anything touching change control, governance, document precedence, owner
  gates, promotion/threshold edits, safety-mechanism modifications, the AGENTS.md task
  contract, or claim wording ("done"/"validated"/"closed"). Also load it when two repo
  documents contradict each other and you must decide which one binds. Every other skill
  in this library routes authority questions here.
---

# chronos-change-control

How changes are classified, gated, and reviewed in Chronos — the non-negotiables, the
rationale, and the historical incident behind each. Dated 2026-08-02; re-verify volatile
facts per the Provenance section.

Chronos is an autonomous-trading system where a wrong merge can move real money. Its
governance was built in response to real incidents (four inert safety controls, a burned
research holdout, docs claiming fixes that never happened). Nothing below is ceremony.

## 0. The 60-second answer path

1. Classify your change against the table in §1. When in doubt, take the stricter row.
2. If the row says "owner-only" or "owner review": you may build a PROPOSAL (branch, PR,
   draft ADR) but nothing merges or activates without the owner (Kevin Lee). No agent,
   test result, or backtest substitutes (VISION_COMPLETION_PLAN.md:319).
3. Open the task with the §13 YAML contract (§3 below). Close it with rerunnable
   verification and honest `open:` items.
4. If two documents disagree about whether you may do something: apply the precedence
   ladder (§2), then STOP AND SURFACE the contradiction — never average (AGENTS.md:54).

## 1. Change-classification table

"AI session" = you may implement it on a branch/PR; the owner still merges every PR
(observed practice, §9). "Owner review" = explicit owner sign-off on the substance is
required before merge, per written rule. "Owner-only + new ADR" = an agent may only
propose; the change takes effect exclusively through an owner decision recorded in a new
ADR (authoring mechanics in §6).

| Change class | Who may do it | Evidence it needs | Rule (file:line) |
|---|---|---|---|
| Code-only fix (no safety/authority/money semantics) | AI session | §13 YAML; rerunnable verification (the four gates: pytest, ruff check, ruff format --check, mypy — README.md:252-258); `evidence_artifact: none; code-only change` | AGENTS.md:37-39; VISION_COMPLETION_PLAN.md:343-352 |
| Test addition | AI session | Suite green; the test must not place orders or contact a broker/network — "No order is placed by any test, CI run, or development path" | AGENTS.md:33-34; README.md:107 |
| Doc factual-status update | AI session, evidence-backed only | Rerunnable verification of the fact; supersede/correct in place, never silently rewrite (§6) | VISION_COMPLETION_PLAN.md:358 ("Agents may make evidence-backed factual-status updates with rerunnable verification") |
| Config default change (any economic-looking or safety-relevant field) | Owner review | Classification of the field as mechanically enforced / explicitly advisory / forbidden; direction-of-failure analysis (must stay fail-closed) | AGENTS.md:29-30 (inert fields are release blockers); AGENTS.md:33-34 (capital/risk-limit changes need explicit owner review) |
| Schema / migration | AI session (owner merges the PR) | New Alembic revision in `src/chronos/persistence/migrations/versions/` (head `0006_proposal_queue.py` as of 2026-08-02) + the completeness tests in `tests/integration/test_migrations.py` (v2/v3/v4→head upgrades, `test_models_have_no_untracked_tables`, `test_migration_chain_builds_exactly_the_current_models`) green | Observed practice + README.md:258-259 ("Migration verification ... runs inside the pytest step"). No looser written rule exists — treat drift-guard failures as blockers |
| New safety control (adds a refusal; fail-closed direction) | AI session builds; owner review at merge (money-critical) | An `*_exercised`-style test proving the control fires end-to-end, asserting the never-before-seen outcome; revert-the-fix proof per half (the M9-M11 pattern, RISK_REGISTER.md:33-35); a RISK_REGISTER row with disclosed residuals | AGENTS.md:33-34; house proof pattern in chronos-validation-and-qa |
| Safety-mechanism MODIFICATION (touching an existing gate, check, refusal, or default) | Owner review, explicit — and never in the weakening direction to make progress | Proof the blocking direction is preserved; updated RISK_REGISTER row; if the change weakens anything, it is an autonomy-authority change (next row) | AGENTS.md:23-24 ("Never weaken a gate to manufacture progress"); AGENTS.md:33-34; ADR-0017:50-65 (execution-correctness mechanisms are untouchable) |
| Autonomy-authority change: widening a mandate's scope, adding an order form, raising a ceiling, adding a network channel | Owner-only + NEW ADR. Agents propose; the owner decides | A new numbered ADR with explicit scope + a DECISIONS.md row + in-place supersession annotations on whatever it overrides (§6) | The D-11→D-16→D-17 precedent (DECISIONS.md:18,23,26); ADR-0016 §6's own rule that growing `OrderForm` needs an "instrument-specific ADR, tests, and mandate permission" (quoted at ADR-0017:9-11); R-32: an out-of-band alert channel "needs a networked channel and its own ADR" (RISK_REGISTER.md:41) |
| Promotion / threshold / frozen-criterion change | Owner-only. Agents may propose, never approve | The threshold must have been frozen BEFORE the evidence it judges was observed (§5); promotion needs its ladder's evidence artifact | AGENTS.md:27-28, 33-34; VISION_COMPLETION_PLAN.md:314-315 (§11: "live promotion, and every cap increase"), :354-362 |
| Scope change (asset family, either 10/10 definition, KPI, owner gates, precedence itself) | Owner-only | Explicit owner change to the canonical plan, recorded in VISION_COMPLETION_PLAN.md with the evidence and commit that caused it | AGENTS.md:25-26 ("unless the owner explicitly changes the canonical plan"); VISION_COMPLETION_PLAN.md:358-362 |

Two clarifications that recur:

- "The owner liked another bot's design" licenses widening OWNER-SET LIMITS only — never
  execution-correctness mechanisms — and only via a new ADR. That is not an inference;
  it is how ADR-0017 scoped itself (§8 quotes it verbatim).
- A live defect always blocks promotion; roadmap or ADR prose cannot waive it
  (AGENTS.md:46-47). You cannot argue a gate open with a document.

## 2. Document precedence, operationalized

When repository documents disagree (AGENTS.md:41-52), higher tier wins:

1. Explicit owner direction within safety and human-approval boundaries.
2. Current executable facts and unresolved safety/security defects. A live defect always
   blocks promotion; roadmap or ADR prose cannot waive it.
3. Accepted ADRs plus `DECISIONS.md` for intended authority and architecture.
4. `docs/safety.md`, `docs/limitations.md`, `RISK_REGISTER.md` for controls and residuals.
5. `docs/VISION_COMPLETION_PLAN.md` for roadmap order and completion criteria.
6. Historical plans, task boards, briefs, and handoffs — context only.

"Stop and surface a contradiction; never average incompatible instructions."
(AGENTS.md:54, verbatim.)

### Worked example — a real contradiction, resolved

Claim in prose (tiers 3/6): the autonomy mandate replaces session arming.
`docs/live_trading_runbook.md:21-24`: "an active owner-authored **AutonomyMandate**
replaces gates 7 (session arming) and 8 (per-order confirmation) — **and only those
two**". ADR-0017:83-84: "A running backend plus a valid mandate file is now sufficient to
trade; there is no per-boot ritual."

Fact in code (tier 2): the live gate walk unconditionally requires a current arm —
`src/chronos/orders/submission.py:441`: `armed = self._live_arming.is_armed(now=fresh_now)`
— and `grep -rn "mandate" src/chronos/orders/` returns zero matches. The order plane does
not know mandates exist.

Resolution by the ladder: tier 2 beats tier 3. The code IS the current fact — autonomous
LIVE submission is blocked without a session arm; the prose overstates operability, not
safety. What you must NOT do: "fix the docs' promise" by deleting the arming requirement.
That would weaken a gate to match prose — an autonomy-authority change (owner-only + new
ADR, §1). What the repo actually did: recorded the conflict as an open Phase 1 defect —
"Choose and implement one reviewed authority model" (VISION_COMPLETION_PLAN.md:151-153) —
i.e. it stopped and surfaced. Do the same: name both sides with file:line in your task
close and in the owner-decision queue (chronos-priorities-and-roadmap).

### Reverify point-in-time findings

"Reverify point-in-time findings against the current commit before implementing them.
Branches, capabilities, broker APIs, data, and prior handoffs are claims, not live state."
(AGENTS.md:35-36.) The vision plan applies this to itself: its Phase 1 findings say
"Reverify each against the live commit and coordinate with any branch already addressing
it before editing" (VISION_COMPLETION_PLAN.md:140-141). Even the default-branch name is a
dated observation, and "not permission for an agent to rename or rewrite shared history"
(VISION_COMPLETION_PLAN.md:54-56). Which documents routinely fail this test is
chronos-docs-map's ledger; check it before citing anything historical.

## 3. The task open/close contract

AGENTS.md:37-39 (verbatim): "At task start, name the plan phase, KPI, acceptance gate,
and intended file set. At task close, provide rerunnable verification and state what
remains. Do not claim a rung or score advanced without its required evidence artifact."

Every implementation or review must state the §13 block
(VISION_COMPLETION_PLAN.md:343-352, verbatim):

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

Field by field:

- `plan_phase` — which phase 0-5 of VISION_COMPLETION_PLAN.md §5-§10 this task serves.
  If you cannot name one, the task may be off-plan; check chronos-priorities-and-roadmap.
- `primary_kpi` — exactly one of the three literal values (the §4 scorecard KPIs).
- `gate_advanced` — the exact acceptance-gate text you claim to have advanced, or
  `"none"`. Claiming a gate without its evidence artifact is the forbidden move.
- `files` — the working set, declared at START. Scope creep beyond it is visible drift.
- `verification` — commands a stranger can rerun, plus what you actually observed.
- `evidence_artifact` — the named artifact backing any status claim (test file path,
  promotion record, capture file), or the literal `"none; code-only change"`.
- `owner_gate` — `required` (a §11 gate binds and is unmet: stop at proposal),
  `satisfied` (the owner already acted; name where that is recorded), or
  `not applicable`.
- `open` — residual risks, contradictions you surfaced, deferred work. An empty `open:`
  on a nontrivial task is a red flag, not an achievement.

Filled example (illustrative shape — counts are placeholders, not history):

```yaml
plan_phase: 1
primary_kpi: safety_integrity
gate_advanced: none
files: src/chronos/supervisor/loop.py, tests/safety/test_supervisor_gateway.py
verification: ".venv/bin/pytest tests/safety -q -> N passed; .venv/bin/mypy src/chronos -> clean"
evidence_artifact: tests/safety/test_supervisor_gateway.py (refusal-terminal cases)
owner_gate: not applicable
open: "SubmissionOutcome(submitted=False) now terminal as refusal; broker-ambiguous
  outcomes still route MANUAL_REVIEW — mandate-vs-arming contradiction (plan §6 item 4)
  untouched, surfaced to owner queue"
```

Closing governance lines (VISION_COMPLETION_PLAN.md:354-356): "Do not edit a frozen
criterion after seeing its evidence. Do not claim plan completion from code coverage
alone. Update this document only when live state, scope, sequencing, or a gate actually
changes; record the evidence and commit that caused the change."

## 4. Owner gates — what ONLY Kevin can supply or approve

VISION_COMPLETION_PLAN.md:306-319 (§11). Checklist form — if your task needs any of
these, the correct output is a proposal plus `owner_gate: required`:

- [ ] Broker credentials, 2FA, account configuration, API permissions, gateway access.
- [ ] Market-data subscriptions; option-reference-data licensing/legal terms.
- [ ] Capital, loss, drawdown, CVaR, concentration, turnover, exposure, and
      incident-response-availability decisions.
- [ ] Holdout unlock, paper mandate, canary authorization, live promotion, and EVERY cap
      increase.
- [ ] Manual broker resolution of unknown orders, positions, assignments, ambiguous sends.
- [ ] Tax, regulatory, and account-structure review.

"No test result, backtest, backup, or agent recommendation substitutes for an owner
gate." (VISION_COMPLETION_PLAN.md:319, verbatim.) A green suite is not permission. A
recommendation from another AI session is not permission. A backup existing is not
permission to restore-and-trade (restore reality: chronos-run-and-operate).

Related standing owner decision, live and unresolved as of 2026-08-02: the account
capital question (~USD 110 last snapshot vs the ~USD 3,000 premise still in older docs —
VISION_COMPLETION_PLAN.md:68-70). Flag it where relevant; never quietly assume either
number.

## 5. Freeze-before-observe

The rule (AGENTS.md:27-28, verbatim): "Freeze statistical, operational, and financial
thresholds before observing the evidence they judge. A failed holdout rejects the
candidate; it does not invite threshold edits."

Operationally:

- Thresholds, sample floors, and acceptance gates are set and committed BEFORE the data
  or run they will judge is looked at. Editing one after seeing its evidence is
  forbidden regardless of how reasonable the edit looks
  (VISION_COMPLETION_PLAN.md:354: "Do not edit a frozen criterion after seeing its
  evidence").
- A failed holdout is a terminal verdict on the candidate. The response is rejection,
  never "adjust the floor", "widen the cap", or "rerun with a friendlier window".
- Agents may PROPOSE changes to KPI or promotion thresholds, either 10/10 definition,
  owner gates, or document precedence; those "require explicit owner approval before
  merge; an agent cannot approve its own easier definition of completion"
  (VISION_COMPLETION_PLAN.md:358-362).

The incidents behind it: adversarial review round 1 found undisclosed research
cap-widening that made a near-miss framing misleading — under the original USD 3,000 caps
the flagship candidate made 7 trades, not 18 (docs/INDEPENDENT_REVIEW.md, finding H5);
round 2 found a re-run had silently consumed QQQ's one-shot holdout while the report
claimed it pristine (docs/INDEPENDENT_REVIEW_M5.md), which is why holdout consumption is
now a ledgered, owner-typed, single-use unlock (D-15, DECISIONS.md:22). Mechanics of the
statistical gates live in chronos-research-methodology; the holdout-guardian and registry
mechanics too.

## 6. ADR discipline as practiced HERE

### The house pattern: supersede in place, never rewrite

Chronos never quietly edits a decision. The superseded text stays visible, struck or
annotated, dated, pointing at what replaced it. Three verbatim exemplars:

1. DECISIONS.md:18 (D-11 strike-through): "~~No generative model output feeds any
   runtime decision....~~ **SUPERSEDED by D-16 (2026-07-25).** Kept for history; read it
   as the posture through that date, not as current policy."
2. ADR-0016 in-place bracket annotations
   (docs/adr/ADR-0016-controlled-autonomous-model-authority.md:142-143): "*[Superseded by
   ADR-0017 §1: the ceiling is now 365 days. `expires_at` is still required and still
   enforced; renewal is still a fresh owner action.]*" — the original 30-day rule remains
   readable above it. The ADR's status line (:3) records the partial supersession:
   "accepted (owner directive, 2026-07-25); §4 and §6 superseded in part by ADR-0017".
3. Scoped correction at source (docs/adr/ADR-0010-crypto-family.md:118-125): "> 
   **Correction (2026-07-27, M10).** Neither half of that sentence was true when it was
   written, and the paragraph stayed wrong for five milestones. ... The claim is left
   standing above rather than edited away, because an ADR that quietly rewrites its own
   history is worth less than one that shows where it was wrong."

Same pattern elsewhere: TASKS.md:66-70 scope note narrowing the board's exclusions in
place; docs/GO_LIVE_CHECKLIST.md:185-189 strikes through its own no-live-trading claim
and corrects it, dated. When you correct a doc, imitate these exactly: strike or
blockquote-annotate, date it, name the superseding authority, leave the original legible.

### New ADR vs DECISIONS.md row vs doc edit

- NEW ADR (owner-approved): any architectural or authority decision — and mandatorily for
  every autonomy-authority change (§1). Each ADR that decides something gets a
  DECISIONS.md index row (D-nn ↔ ADR-nnnn); ADR-0017's header says "Index entry:
  DECISIONS.md **D-17**".
- DECISIONS.md row alone: a consequential decision too small for a full ADR — precedent
  D-10 (Pine corpus provenance) whose ADR column is "—" (DECISIONS.md:17).
- Doc edit alone: evidence-backed factual-status updates with rerunnable verification
  (VISION_COMPLETION_PLAN.md:358), using the in-place correction pattern above. Never use
  a doc edit to change what is ALLOWED — that is an ADR's job.

### Numbering and status conventions

- Files: `docs/adr/ADR-NNNN-short-slug.md`, four-digit zero-padded. Highest existing:
  ADR-0019 (2026-08-02); the next ADR is 0020.
- Status line is line 3, e.g. "Status: accepted (owner directive, 2026-07-25)" or
  "Status: accepted (design-panel remediated, 2026-07-18)" — the parenthetical names the
  review that earned acceptance. Supersessions are appended to the status line, not
  hidden.
- Known drift: ADR-0012 still says "Status: proposed" and ADR-0014/ADR-0015 "proposed
  (design-review pending)" although their code shipped
  (`src/chronos/research/walkforward.py`, `stats.py`, `campaign.py`;
  `python -m chronos.histdata options`). Status lines were never flipped. Do not read
  "proposed" as "unbuilt" or "accepted" as "verified" — check the code, and see
  chronos-docs-map's contradiction ledger (entry #12f) for the full list.
- An ADR records the decision, its scope, what it supersedes IN PLACE, what it explicitly
  does NOT supersede (ADR-0017's "Not superseded" list is the model — DECISIONS.md:26),
  the residuals it accepts, and the owner as authority when it is an owner directive.

## 7. Claim discipline

Non-negotiable: never state a milestone, control, or strategy is "done", "working", or
"validated" without naming the exact evidence artifact — test file, real-gateway run,
promotion artifact. This project was burned by exactly this failure four times: the four
M0 kernel defects were all "fully wired, documented, tested" controls that structurally
could never fire (RISK_REGISTER.md:31-35):

- R-24 — the writer lease was never renewed in production and was not a fencing token.
- R-25 — `max_opening_orders_per_day` never refused an order: its evidence field was
  never gathered, and the counter that would supply it had zero callers and counted the
  wrong side.
- R-26 — the market-session gate was permanently AMBIGUOUS: its evidence provider
  hard-returned `None` (`liquidHours`/`timeZoneId` live on IBKR's `ContractDetails`, not
  the inner `Contract`).
- R-27 — option-deliverable verification was set by exactly one thing: the demo broker,
  by fiat; a unit test pinned the defect for six milestones.

Full narrative and the prevention pattern: chronos-failure-archaeology (history) and
chronos-ibkr-boundary (the wrong-nested-object class). The claim rules that follow:

- MITIGATED ≠ CLOSED. "All four kernel defects are now mitigated and **none is closed**
  — each keeps a disclosed residual" (README.md:102-103). Every adapter-path control is
  fixture-verified only; no real IBKR gateway (paper or live) has ever been connected in
  this project's history. Say "mitigated, fixture-verified, residual X" — never "closed".
- Use the README's label vocabulary: **[enforced]** = "live controls with code and tests
  behind them today"; **[contract]** = "structural guarantees of the contract types"
  (README.md:121-122). A contract-level guarantee is not an enforced control; do not
  promote one to the other in anything you write.
- When a published claim describes "a system slightly better than the one that exists",
  correct it "toward the weaker, true statement rather than toward the code" (M2
  remediation commit 22450b1's own words) — weaken the claim to match reality; do not
  rush code changes to rescue the claim.

## 8. Safety-posture rules restated

These bind every change and every skill in this library:

- Fail-closed and deny-by-default are the default posture everywhere. Nothing you write
  or merge may weaken a safety mechanism, widen autonomous authority, or treat an
  untested control as proven — even hypothetically, even as a "future convenience".
- "A correct `NO_TRADE` result is success when evidence is insufficient. Never weaken a
  gate to manufacture progress." (AGENTS.md:23-24, verbatim.)
- "Tests, CI, and agents must not place live orders." (AGENTS.md:34.) "No order is
  placed by any test, CI run, or development path." (README.md:107.)
- Owner enthusiasm for another system's design widens owner-set limits only — never
  execution-correctness mechanisms — and only through a new ADR. ADR-0017, the maximal-
  autonomy directive itself, drew this line in its own text: "'Maximal autonomy' was
  read as **removing friction and owner-optional ceilings, not removing
  execution-correctness mechanisms.**" (ADR-0017:50-51) and, of the transmit site,
  writer lease, idempotency, reconciliation, kill switch, floors, stale-data refusal,
  and deterministic veto: "**None of these is touched.** Removing them would not be more
  autonomy; it would be a different, broken system." (ADR-0017:64-65.) If even the
  widest owner directive in the repo's history refused to touch these, no lesser
  instruction can.

## 9. Git and PR conventions — OBSERVED practice, not written policy

From the visible history (shallow clone: 150 commits, 2026-07-16 → 2026-08-01; earlier
history is not locally excavatable). These describe what happened; only AGENTS.md and the
docs above are rules.

- Every PR (47 merge commits, #1-#47) was merged by the owner via GitHub merge commits:
  "Merge pull request #N from Ayyitskevin/<branch>", author Kevin Lee. Agents authored
  commits; the owner performed every integration.
- Branch naming observed: `claude/<topic>-<suffix>` (e.g.
  `claude/chronos-autonomous-governance-jhgfat`), `agent/<topic>`
  (`agent/reconciliation-readiness`), `feat/<topic>` (`feat/research-run-repro`),
  `codex/<topic>` (`codex/chronos-option-chain-selection-v1`). Integration branch:
  `feat/wheel-dashboard-mvp` (the GitHub default; no `main` exists — that mismatch is
  recorded repo debt, not permission to rename, VISION_COMPLETION_PLAN.md:54-56).
- Commit subjects: predominantly milestone-prefixed narrative ("M11: the option
  deliverable, and the last kernel defect (closes R-27)"); conventional-commit prefixes
  (`docs:`, `fix:`, `feat:`, `chore:`) are the minority (~10 of 150).
- Commit bodies: long-form engineering narrative explaining the defect, the fix, and the
  proof, closing with a gates footer ("Gates: ruff clean, ruff format clean, mypy strict
  clean (N files), N passed, N skipped.") and `Co-Authored-By:` / `Claude-Session:`
  trailers on agent commits.
- Culture: every substantive feature block is followed by an explicit review-remediation
  commit closing enumerated findings ("M2 review remediation: correct every overstated
  claim the review found"). Adversarial review is the development ritual here, not an
  afterthought.
- Nothing is deleted; dangerous code is quarantined with refusing constructors and
  inventory tests (R-28 pattern), keeping history and proofs intact.

## 10. When NOT to use this skill

- Deciding WHAT to work on next, sequencing, or the owner-decision queue →
  chronos-priorities-and-roadmap.
- Whether a specific DOCUMENT can be trusted / the stale-doc and contradiction ledger →
  chronos-docs-map (this skill only gives you the precedence ladder to resolve with).
- Test mechanics, suite map, what counts as proof for a code change →
  chronos-validation-and-qa.
- Statistical thresholds themselves (DSR, walk-forward, holdout mechanics) →
  chronos-research-methodology.
- The autonomy stack's actual objects (mandate fields, gateway, promotion machinery) →
  chronos-autonomy-and-mandates.
- The history of the incidents referenced here → chronos-failure-archaeology.

## Provenance and maintenance

Written 2026-08-02 against HEAD `47a8d72` (branch `claude/chronos-skills-library-bfbj29`,
same tip as `feat/wheel-dashboard-mvp`). All file:line citations were verified on that
commit. Line numbers drift; re-verify before quoting onward:

| Volatile fact | Re-verify with (read-only) |
|---|---|
| AGENTS.md rules and precedence ladder line numbers | `grep -n "Never weaken\|Freeze statistical\|owner review\|Reverify point\|At task start\|never average" AGENTS.md` |
| §11 owner gates / §13 YAML contract wording | `sed -n '306,362p' docs/VISION_COMPLETION_PLAN.md` |
| D-11 strike-through; D-16/D-17 rows (note: D-17/D-18 rows sit AFTER D-19 in the table) | `grep -n "D-11\|D-16\|D-17" DECISIONS.md` |
| ADR-0016 status + in-place supersession brackets | `grep -n "Superseded by ADR-0017" docs/adr/ADR-0016-controlled-autonomous-model-authority.md` |
| ADR-0017 scoping quotes | `grep -n "owner-optional ceilings\|None of these is touched" docs/adr/ADR-0017-owner-directed-maximal-autonomy.md` |
| ADR-0010 §4 in-place correction | `grep -n "Correction (2026-07-27, M10)" docs/adr/ADR-0010-crypto-family.md` |
| ADR-0012/0014/0015 still "proposed" | `grep -n "^Status" docs/adr/ADR-001[245]*.md` |
| Highest ADR number (0019 as of 2026-08-02) | `ls docs/adr/ | sort | tail -1` |
| "none is closed" + [enforced]/[contract] key | `grep -n "none is closed\|Bullets marked" README.md` |
| Kernel-defect rows R-24..R-27 all MITIGATED | `grep -n "R-2[4-7] " RISK_REGISTER.md` |
| Arming still unconditional; orders plane mandate-free | `grep -n "is_armed" src/chronos/orders/submission.py; grep -rn "mandate" src/chronos/orders/ \| wc -l` (expect 0) |
| Migration head (0006) and drift guards | `ls src/chronos/persistence/migrations/versions/; grep -n "def test" tests/integration/test_migrations.py` |
| PR/branch/commit conventions | `git log --merges --format='%an %s' \| head; git log --format='%s' \| head -20` |
| Capital question still unresolved (~USD 110 vs ~USD 3,000) | `grep -n "USD 110" docs/VISION_COMPLETION_PLAN.md; grep -n "3,000" ASSUMPTIONS.md` |

If any re-verification disagrees with this skill, the repo wins — update this file using
the in-place correction pattern of §6, and log the change.
