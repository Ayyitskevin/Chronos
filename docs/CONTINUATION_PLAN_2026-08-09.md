# Continuation plan — 2026-08-09

Status: **working plan, precedence tier 6.** `docs/VISION_COMPLETION_PLAN.md`
(canonical, 2026-08-01) remains the roadmap authority under the `AGENTS.md`
precedence ladder; this document sequences the next work *inside* that plan and
records a dated examination of what other agents delivered. When they disagree,
the canonical plan wins. Every fact below is a point-in-time claim with the
commit or command that grounds it — re-verify before building on it.

Authored by a Fable session at the owner's request ("examine the work that has
been done, write up your own plan to continue the project, then have Opus
execute it"). Division of labor: Track A and Track B are executable by AI
sessions under the plan-§13 task contract; Track C is owner-only and is
presented, never worked around.

## 1. Examination — state of the repository at `721d7f1`

### 1a. Merged work (default branch `feat/wheel-dashboard-mvp`)

M12 (CHANGELOG, 2026-08-02 → 2026-08-08, PRs #48–#59): the 16-skill
`.claude/skills/` library; twenty-one documents corrected in house style;
terminal emergency-stop buttons (R-43); bounded periodic reconciliation with
evidence expiry (ADR-0020 / D-20, closing plan §6 finding 1); the incident
runbook and backup guide corrected to lead with the live kill switch (doc
halves of findings 2–3); phantom-config removal with a structural guard; the
autonomy handoff reading live writer state; the lockfile-regeneration hazard
record; and `chronos mandate check` (PR #59) with four corrected mandate-
contract claims. Suite baseline at `721d7f1`: **2543 passed, 1 skipped**;
ruff + `mypy --strict` clean.

### 1b. Findings scoreboard (plan §6, re-verified 2026-08-09)

| # | Finding | State |
|---|---|---|
| 1 | Reconciliation one-shot | **CLOSED** (D-20; PRs #50/#51) |
| 2 | Runbook names wrong halt | **CLOSED** (doc corrected; kill switch is step 1) |
| 3 | Restore overstates safety | Doc half CLOSED; **code half OPEN** (`kill_switch.py:83-85` still boots DISENGAGED on missing file) — owner-gated safety-mechanism change |
| 4 | Mandate-vs-arming | **OPEN** — owner decision on which authority model wins (`orders/` has zero mandate awareness; `submission.py` requires the arm) |
| 5 | Supervisor COMPLETE on refusal | **OPEN** — typed outcomes designed in ADR memos; code half owner-gated |
| 6 | Static ingress provenance / non-scoped credential | ~~**OPEN** — ADR-0023 proposed, undecided~~ **CLOSED 2026-08-12** (owner directed Option A; D-24/R-48 — proposer registry, proposal-only credential, drain-time identity; evidence-protocol half remains future ADR work) |
| 7 | Dead economic fields on the decision | **OPEN** — ADR-0021 proposed, undecided |
| 8 | Promotion not evidence-bound | **OPEN** — ADR-0024 proposed, undecided |

Nothing on this board is autonomous work anymore. The platform half is
saturated to its owner gates.

### 1c. Unmerged agent branches (the "other agents" work)

| Branch | Size | Last commit | Content | Disposition |
|---|---|---|---|---|
| `codex/five-tool-confluence-v36` | +8 commits, ~22,000 insertions, 45 files | **2026-08-08** | Deterministic research-plane translation of the owner's `00_five_tool_confluence_aio.pine` (SHA-256 pinned): frozen input contract (`specs/five_tool_confluence_v3_6.yaml`), signal engine, TradingView trace-parity harness, **preregistered six-hypothesis falsification contract** (`docs/FIVE_TOOL_RESEARCH_HYPOTHESES.md`), and a campaign manifest that is `blocked_until_identity_locks_resolve` with zero performance claims and `promotion_authority: none`. Includes its own isolation safety test. Merges into `721d7f1` with **zero conflicts** (verified via `git merge-tree`). | **Track A: integrate + adversarially verify now.** Research-plane, fail-closed, no authority change — normal task-contract work, and it is rotting (37 commits behind and growing). |
| `codex/chronos-option-chain-selection-v1` | +2 commits, ~16,500 insertions | 2026-08-01 | Deterministic option-selection receipts + service; touches broker/market-data/services test surface. | **Track C: owner decision #8 stands** (integrate / rework / drop). Order-plane-adjacent; not autonomous work. It is also rotting — the decision has a real cost of delay. |
| `agent/reconciliation-readiness`, `claude/chronos-trading-system-rrzroq`, `feat/live-wheel-dashboard`, `feat/research-run-repro` | 0 ahead | Jul 2026 | Fully merged history. | Candidates for deletion — owner housekeeping, no action needed. |

### 1d. What the five-tool slice honestly is and is not

It is implementation-fidelity and experiment-design work: a Pine-exact engine,
a parity harness (synthetic trace only — **no real TradingView export exists
yet**, still A-03), and a preregistration that refuses to run. It is **not**
evidence of edge, and its own documents say so correctly. Its manifest is
blocked on, in its words, the missing **certified-reader, replay-artifact,
owner-evidence, and canonical ADR-0013 registry capabilities** — three of
those four are buildable by an AI session; the fourth (owner evidence: frozen
risk limits, power calculation, benchmark economics) is Track C.

Known data constraint it will eventually hit: `research/data/raw` holds five
ETFs (SPY/QQQ byte-exact; IWM/GLD/TLT transcribed at 2 dp — R-08
heterogeneity), no EFA, and the manifest wants 2010+ history. The certified
dataset for the campaign does not exist yet and its sourcing is a Track C
item (data budget / provenance choice), not something to improvise.

## 2. The thesis

The two 10/10 outcomes stand at roughly platform 6/10, proven-trader 1/10
(this session's grading, not owner-ratified). Every remaining platform item is
owner-gated; the trader outcome is starved of exactly the thing the five-tool
slice provides the front half of: a preregistered, fail-closed path from the
owner's own Pine corpus (the SKB — the "knowledge database") to registered,
multiple-testing-honest trial evidence. The owner's stated direction is
indicators-and-strategies paired with that knowledge base.

Therefore: **integrate the five-tool slice, verify it adversarially, build the
three missing non-owner capabilities it names, and put every owner decision in
one dated queue.** Do not run the campaign — it stays blocked until the owner
freezes what only the owner can freeze. A campaign that cannot yet run, but
whose every precondition is named, testable, and either built or on the
owner's desk, is the honest maximum an AI session can contribute to the slow
half.

## 3. Track A — integrate and verify the five-tool research slice (Opus, now)

Plan phase: §3 dependency chain, "certified data → strategy evidence" front
half. KPI: the slice merged, green, and doctrine-verified with zero weakened
gates. File set: the branch's 45 files + drift fixes + CHANGELOG + skill
baseline rows.

1. Merge `origin/codex/five-tool-confluence-v36` into the working branch
   (`--no-ff`, preserving the codex history).
2. Full verification: `.venv/bin/pytest -q`, `ruff check`, `ruff format
   --check`, `mypy --strict src/chronos`. The branch predates M12 by ~37
   commits — fix drift **forward** (new strictness, safety pins, doc-drift
   interactions), never by weakening a gate or deleting a guard.
3. **Adversarial doctrine review** (the burden is on the branch):
   - Isolation: research plane imports no order/broker/execution module —
     run its `test_five_tool_isolation.py` *and* the repo's existing AST
     guards; confirm they FIRE (exercised-test doctrine, not existence).
   - Fail-closed: prove the manifest broker actually refuses data access
     while blocked, and that `ready_for_certified_research` cannot be reached
     by editing prose — run the branch's refusal tests; verify each conjunct.
   - No performance claims anywhere; no threshold defined after observation;
     no holdout touch (QQQ 2022–2024 stays burned; the declared 2026-Q4
     holdout stays future/unopened); trial accounting routes toward the
     ADR-0013 ledger, not self-report.
   - Inert-field scan: any field on the new contracts that nothing reads gets
     the mandate-check treatment — classified and disclosed, or removed.
4. If any BLOCKING doctrine violation is found: **do not merge** — report
   with file:line evidence instead. Integration is conditional, not assumed.
5. Update `CHANGELOG.md` (new section), and the dated suite-baseline rows in
   `chronos-priorities-and-roadmap` §2/§7 and `chronos-validation-and-qa` §2
   (house-style correction, new date).
6. Commit, push, PR to `feat/wheel-dashboard-mvp` with an honest body: this
   is fidelity + experiment-design infrastructure, not evidence of edge.

Acceptance gate: suite green including the branch's own tests; all four
doctrine checks pass with named exercised tests; CHANGELOG and skill baselines
updated; PR open.

## 4. Track B — build the three non-owner missing capabilities (Opus, after A)

In the branch's own declared order of need, each landing fail-closed with
exercised tests, each its own PR:

1. **Canonical ADR-0013 registry integration** — five-tool trials register in
   the hash-chained ledger (which today ships empty), so the multiple-testing
   trial count is enforced, not self-reported.
2. **Certified-reader capability** — a digest-locked dataset reader that
   refuses unlocked/undigested data, built against the existing
   `research/data/raw` manifest discipline. Building the reader is
   engineering; *certifying a dataset* through it waits for Track C item 4.
3. **Replay-artifact capability** — whatever `five_tool/validation.py`
   requires to make a trial run reproducible byte-for-byte.

Explicitly out of scope for Track B: acquiring new data (EFA, 2010+ history),
running any hypothesis test, unblocking the manifest, and anything touching
the order plane.

## 5. Track C — the owner-decision queue (present, never work around)

Consolidated, in rough order of cost-of-delay:

1. **Real-gateway read-only campaign** (plan §7) — still the single
   highest-leverage step and the only calendar-urgent one: every uncaptured
   option day is unrecoverable (ADR-0012). Execute via
   `chronos-real-gateway-campaign` when you supply the gateway.
2. **Phase 0 freezes the five-tool campaign is blocked on** — risk limits,
   power calculation, benchmark/minimum-edge economics (plan §5). The
   manifest's null fields make this ask concrete for the first time.
3. **Certified-dataset sourcing** — approve a provenance path for EFA + 2010+
   daily history (the owner-connected IBKR MCP connector is a candidate
   source under D-07's pattern; fidelity and licensing are your call).
4. **Option-chain branch** (decision #8) — integrate / rework / drop
   `codex/chronos-option-chain-selection-v1`; it degrades while undecided.
5. **Capital envelope** — ≈USD 110 vs ~$3k premise (plan fact, unresolved).
6. **ADR-0021 / 0022 / 0023 / 0024** — accept, amend, or reject; findings
   5–8 close or re-scope on your answer.
7. **Findings 3 and 5 code halves** — boots-kill-engaged recovery; typed
   handoff outcomes.
8. **Arming model** (finding 4) — mandate-replaces-arming vs arm-required.
9. **TradingView reference exports** (A-03) — the parity harness now exists;
   it is spec-level until real exports land in `fixtures/tradingview/`.
10. **Default-branch flip to `main`** + branch protection + retiring the old
    branch; optionally delete the four fully-merged agent branches.

## 6. Anti-goals binding the executor

From plan §3/§10/§12 and AGENTS.md, restated for Tracks A–B: no campaign
execution; no threshold edits (18-vs-20 stays exactly the case the floor
exists for); no new asset vocabulary; no gate weakened to make a test pass; no
performance claim without a registered, reconciled artifact; `NO_TRADE` and
"blocked" are success states when evidence is insufficient.

## 7. Verification

Every Track A/B PR closes with: `.venv/bin/pytest -q` (count stated), `ruff
check .`, `ruff format --check .`, `mypy --strict src/chronos`, and the named
exercised tests for each new refusal path. This document is superseded the day
`docs/VISION_COMPLETION_PLAN.md` §6/§7 state changes; correct it in place, in
house style, rather than letting it claim stale truth.
