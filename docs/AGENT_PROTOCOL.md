# Agent operating protocol — every seat, every session

The canonical, agent-agnostic operating protocol for AI work in this repository:
branches, PRs, gates, review, merges, and ID allocation. `AGENTS.md` says what may be
built and who decides; this document says how a session moves work through the repo
without stranding, clobbering, or overstating it. It binds every seat equally.

Written 2026-08-22, the day the default branch flipped to `main` and two superseded
branch tips were re-pushed against a 98-commit-stale base (§11, case 4). Every rule
below exists because something already went wrong without it. Volatile facts appear as
commands to run, never as copied values — copied values are how sixteen skills went
stale in three weeks. Re-verify per the Provenance section; if the repo disagrees with
this document, the repo wins.

```text
MANDATORY PREFLIGHT — mechanical, before any edit, no exceptions
----------------------------------------------------------------
git fetch origin --prune
DEFAULT=$(git ls-remote --symref origin HEAD | awk '/^ref:/{sub("refs/heads/","",$2); print $2}')
git checkout -B <seat>/<topic> "origin/$DEFAULT"
make gates          # record the measured counts — they are your baseline
# Read order: AGENTS.md -> this document -> DECISIONS.md + the ADRs your change touches
```

## 1. Seats and branch prefixes

Multiple AI seats work this repository, and every one of them pushes as the same
GitHub account. The branch prefix is therefore the only durable authorship signal the
repo itself carries — which is why it is mandatory, not decoration.

| Seat | Branch prefix |
|---|---|
| Claude Code | `claude/` |
| Codex | `codex/` |
| Grok | `grok/` |
| Kimi | `kimi/` |
| Cursor | `cursor/` |
| OpenCode, and any seat without its own prefix | `agent/` |
| GLM | reviewer seat — posts verdicts, pushes no branches |

Branch names: `<prefix><topic>`, lowercase, hyphenated, specific
(`claude/chronos-hourly-bars-lane`, not `claude/fixes`).

**Coordination surfaces are not repo authority.** Buzz chat, Athena issues, and
`shared/handoffs/` live on the host machine (mickey). They are invisible from the
repository — to CI, to other checkouts, to any future reader of the history. They are
where seats talk; they are never where decisions live. Anything load-bearing — a
verdict, a HOLD, an owner direction, a measured result — gets copied INTO the repo:
a PR comment, a commit message, a DECISIONS.md row, a doc annotation. A rule that
exists only in a chat channel does not exist.

## 2. Session preflight, expanded

The box above, line by line — each line is load-bearing:

1. **`git fetch origin --prune`** — your local refs are claims about a remote that has
   moved since they were written. Prune, because merged branches auto-delete here and
   a stale local ref is exactly the raw material of case 4 (§11).
2. **Derive the default branch by command** — `git ls-remote --symref origin HEAD`.
   Never from memory, never from a document (including this one), never from what it
   was last week. The default has flipped once already (2026-08-22,
   `feat/wheel-dashboard-mvp` → `main`, old branch deleted); every stale copy of the
   old name was a PR aimed at the wrong base waiting to happen, and one actually
   happened (§11, case 4).
3. **Branch from the live default tip** — `git checkout -B <seat>/<topic>
   origin/<default>`. Not from a local branch, not from another seat's branch, not
   from where HEAD happens to sit. Confirm where you actually are before editing and
   before trusting any verification: `git rev-parse --abbrev-ref HEAD` — a reviewer
   has switched a shared checkout's branch despite read-only instructions, and three
   fixes briefly landed on the wrong tree (§11, case 2).
4. **Run the gates and record the measured counts** — `make gates` (§4). What passes
   and how many is your baseline, measured at `origin/<default>`, at preflight, by
   you. A documented number is not a baseline; it is a historical claim.
5. **Read order** — `AGENTS.md` (the binding rules and precedence ladder), this
   document, then `DECISIONS.md` and the ADRs the change touches. The task contract
   every PR must state is the §13 YAML in `docs/VISION_COMPLETION_PLAN.md` §13 —
   canonical there, restated nowhere.

## 3. Branch and PR rules

- **The PR base is the default branch. Always.** Verify it, don't assume it:
  `gh pr view <n> --json baseRefName` against the derive command in §2. A PR's MERGED
  badge is a claim about the PR's *base*, not about the default branch — a PR merged
  into anything else is work stranded with a green light on it (§11, case 1).
- **No stacked PRs without explicit owner acknowledgment.** Stacked review may be
  owner-approved; stacked landing is not. Merge base-first. After each parent lands,
  rebase and retarget every descendant to the live default before merging it, rerun CI
  and exact-candidate review when the candidate changes, then apply §6 to that PR's
  result. A PR merged into a stack parent remains stranded regardless of its badge; do
  not postpone integration proof until the end of the stack.
- **One item per PR.** One logical change, one reviewable narrative, one revert unit.
- **A merged PR is never reused.** No new commits to a merged branch, no reopening.
  New work is a new branch from the live default tip.
- **Never re-push a superseded tip.** Before pushing any ref you did not create this
  session, compute its true divergence:
  `gh api repos/Ayyitskevin/Chronos/compare/<default>...<ref> --jq .ahead_by`
  — against the default derived in §2, never against a local or remembered base.
  `ahead_by=0` means the default already contains everything the ref has: there is
  nothing to preserve, and pushing it manufactures branch clutter, red CI, and false
  "unique commits" claims (§11, case 4).
- **Preservation is a ref, not a PR.** History worth keeping that does not belong on
  a branch goes to `refs/preserve/<seat>/<name>-<YYYYMMDD>`:
  `git push origin <sha>:refs/preserve/<seat>/<name>-<YYYYMMDD>`.
  No CI runs, no branch list clutter, no PR — retrievable forever by anyone who
  fetches the ref. A preservation ref is claimed once and never force-updated.
- `delete_branch_on_merge` is on: a branch vanishing after merge is the system
  working, not an incident. Do not recreate it.

## 4. The gate

CI is ground truth for what the gate *is* — the canonical definition lives in
`.github/workflows/ci.yml`, and if this section ever disagrees with that file, the
workflow wins. Run what CI runs, locally, before pushing:

```bash
make gates    # the local mirror of the workflow; if the two disagree, the workflow wins
grep -E '^\s+run:' .github/workflows/ci.yml    # the authoritative step list, derived not remembered
```

- The `main-integrity` ruleset makes the default branch PR-only with the `quality`
  check required: a red gate does not merge, there are no bypass actors, and no seat
  is special. Force-push and deletion of the default branch are refused at the remote.
- **Measured counts, always.** The commit's gates footer carries what *you* measured
  at *your* commit, in the house form:
  `Gate: ruff check clean, format clean, mypy clean on <N> source files, <N> passed / <N> skipped / 0 failed.`
  Copying a count from a document or a previous commit is a claim you did not verify.
- Commit trailers are part of the spec: `Co-Authored-By:` for the authoring model,
  plus the seat's session trailer (e.g. `Claude-Session:`) where the harness provides
  one. Narrative bodies — what was wrong, what changed, what proves it — are the house
  style; the history is the reviewable record.
- **A check red at your measured base is its own work item, not your PR's silent
  absorption.** Note it, leave it, let the lane that owns it fix it. Fixing it in
  passing widens your diff and steals another lane's verification story.

## 5. Review

Review scales with blast radius, never with who wrote the change:

- **Trivial** (typo, doc fact-fix, local rename) — self-verify per §4; no cross-seat
  review.
- **Moderate** (feature work, refactors, new tests) — one review by a seat that did
  not write the change. Reviewer ≠ author, always; no standing default reviewer.
- **Owner-gate** (money, authority, safety mechanisms, schema against live state,
  security-sensitive code, promotion, scope) — cross-seat review *and* Kevin. The
  seats advise; the owner decides (`docs/VISION_COMPLETION_PLAN.md` §11).

**HOLD / PASS semantics.** A verdict is written, cites evidence, and names its scope.
PASS means "I verified X at commit Y and found no blocker." HOLD means "this must not
merge until Z" — and a HOLD is closed **only by the holder re-verifying in writing**.
The author's fix, however correct, does not close a HOLD; it answers one. A HOLD whose
fix merged without the holder's written withdrawal is an open loop wearing a merge
commit (§11, case 3). "A guard with a known bypass is worse than none" was a HOLD
worth having; it deserved — and got — a written close.

**Reviewer hygiene:**

- Review in a **fresh worktree** (`git worktree add`), never in a checkout another
  lane is using. Read-only means read-only: a reviewer does not switch branches,
  does not edit, does not "quickly fix" (§11, case 2).
- Before trusting any verification you ran: `git rev-parse HEAD` and
  `git rev-parse --abbrev-ref HEAD`. Confirm you measured the tree you think you
  measured.
- **An empty review result is not a pass.** Zero findings is indistinguishable from
  zero reviewers until you check that the reviewers actually ran — exit status,
  token-limit errors, output artifacts. A review's first run once reported zero
  findings because all four reviewers had died on a usage limit (§11, case 2). A
  clean verdict must carry positive evidence of execution: what ran, what it
  examined, what survived.

## 6. Merge and after

- The owner merges owner-gate PRs. "The owner merging the PR is the review act"
  (`docs/CONTINUATION_PLAN_2026-08-12.md`) — do not pre-merge, do not self-merge a PR
  whose contract says `owner_gate: required`.
- **Post-merge, verify the result rather than the badge:**
  1. Fetch, derive the default branch again (§2), and read the exact PR identities:
     `gh pr view <n> --json baseRefName,headRefOid,mergeCommit,state`. Require `MERGED`,
     the derived default as the base, the reviewed SHA as `headRefOid`, and a non-null
     `mergeCommit.oid`. Call that last value `<merge-result-sha>`.
  2. Prove the merge result itself is on the default branch:
     `git merge-base --is-ancestor <merge-result-sha> origin/<default>`. This check
     applies to every merge strategy and catches a result stranded on a stack parent.
  3. Determine whether the merge preserved the reviewed candidate's identity:
     - For a merge or fast-forward that preserves it, run
       `git merge-base --is-ancestor <candidate-sha> <merge-result-sha>`.
     - A squash or rebase merge rewrites identity, so candidate ancestry is expected to fail.
       Derive and inspect the exact PR path set with
       `gh api repos/Ayyitskevin/Chronos/pulls/<n>/files --paginate --jq '.[] | .filename, (.previous_filename // empty)'`,
       then require byte-for-byte final-state equality on those paths:
       `git diff --exit-code <candidate-sha> <merge-result-sha> -- <changed-paths>`.
       Pass the inspected paths explicitly; do not use unquoted command substitution.
  4. Require exact default-branch CI at `<merge-result-sha>` and read every required
     job's result and relevant log evidence. A green candidate run is not proof about
     the rewritten result.
  5. Spot-check claimed additions, modifications, and deletions at the merge result.
     The phantom merge was caught because the claimed documents were absent.
  6. After merging anything that appends to a table document (`DECISIONS.md`,
     `RISK_REGISTER.md`), grep for duplicated row IDs — merges can duplicate doc
     blocks byte-identically, and this file's own D-21/D-22 collision is the standing
     exhibit: `grep -oE '^\| [DR]-[0-9]+[^ |]*' DECISIONS.md RISK_REGISTER.md | sort | uniq -d`
     (match the full ID cell — a looser pattern false-positives on the `R-nn-orig`
     historical rows, which this command's first rehearsal proved)
- The branch auto-deletes on merge. Expected. Anything you still need from it should
  already be in the merge or in `refs/preserve/*`.

## 7. ID allocation — D / ADR / R / H

The next ID is `max + 1` where max is **scanned, in the same session, in the same PR
that claims it** — never remembered, never read from a skill or a plan:

```bash
grep -oE '^\| D-[0-9]+'  DECISIONS.md      | grep -oE '[0-9]+' | sort -n | tail -1
ls docs/adr/ | grep -oE 'ADR-[0-9]{4}' | sort | tail -1
grep -oE '^\| R-[0-9]+'  RISK_REGISTER.md  | grep -oE '[0-9]+' | sort -n | tail -1
```

Hypothesis IDs are campaign-scoped (`H-5T-nnn`, `H-DT-nnn`): scan the hypothesis
document that owns the campaign before claiming.

Concurrent lanes can still collide — two sessions scan the same max on the same
morning and both claim the next number. The resolution rule: **whoever merges second
renumbers their own unmerged claim at rebase.** A merged row is never renumbered —
external citations bind to it the moment it lands (ADR-0025 cites D-21 by number;
renumbering it would silently retarget every citation). The duplicate D-21/D-22 rows
in `DECISIONS.md` are annotated in place as (a)/(b) for exactly this reason: the
collision merged twice before anyone scanned, and by then both numbers were
load-bearing.

**Three namespaces answer to "D", and they are unrelated:**

1. `DECISIONS.md` **D-nn** (hyphenated) — governance decisions, the running index.
2. **Plan work items D1–D4** (unhyphenated) — the current Phase 3 / deep-trading
   lane's items (`docs/DEEP_TRADING_FEASIBILITY.md`, e.g. D2 = the owner's certified
   data pull). These are checklist labels, not decisions.
3. `docs/AI_QUANT_GAME_PLAN.md`'s **historical D1–D4** milestone track — context
   only, superseded as roadmap authority.

Cite the file with the number when there is any room for confusion: "D-22
(ADR-0026)" is unambiguous; a bare "D2" is not.

## 8. Claims and evidence

- Every implementation or review PR states the task-contract YAML — canonical
  definition and field semantics: `docs/VISION_COMPLETION_PLAN.md` §13. The PR
  template carries the skeleton; the plan carries the meaning.
- The claim-evidence ladder binds all prose you write here: **MITIGATED ≠ CLOSED** —
  a mitigation with a disclosed residual stays MITIGATED, and promoting it to CLOSED
  in a summary is how documentation starts lying. "Done", "working", "validated"
  require a named evidence artifact — a test file, a run record, a capture — or they
  are not written.
- **Point-in-time findings are claims.** Reverify against the current commit before
  implementing them (`AGENTS.md`): branches move, defects get fixed, defaults flip.
  A finding from this morning is already historical by the afternoon of a busy day —
  2026-08-22 proved this twice before noon.
- **Baseline = measured at preflight from `origin/<default>`.** Never a documented
  number, including any number in this document. The gates footer carries what you
  measured; the next session measures its own.

## 9. Owner gates

The canonical list is `docs/VISION_COMPLETION_PLAN.md` §11: credentials, market-data
subscriptions, capital and loss decisions, holdout unlocks, mandates, promotions, cap
increases, broker resolutions, tax/regulatory review. No test result, backtest, or
agent recommendation substitutes.

Standing owner-only items as of 2026-08-22 — presented, never worked around:

- The XAI console key (`XAI_API_KEY`) for the Grok worker provider.
- The TWS/IBKR D2 historical pull and the independent corporate-action sample — the
  attestation is the owner's act; an export without one is refused
  (`docs/certified_data_runbook.md`).
- Every `*_FORWARD` flag (`CHRONOS_WORKER_FORWARD`, `CHRONOS_TV_BRIDGE_FORWARD`) —
  built inert, enabled only by the owner.
- Promotion decisions at every rung, and every cap increase.
- Security-sensitive merges: the review verdict advises, the owner merges.

## 10. Constraints not to re-derive

Settled boundaries that look like open questions to a fresh session. Re-deriving them
wastes the session and risks reversing a deliberate decision:

- **R-26 / D-30:** market-open evidence comes from the venue's own `CLOSED` token —
  that is the load-bearing token. The research session calendar
  (`chronos.research.session_calendar`) exists for *certification coverage* and is
  structurally fenced from every authority package; the isolation guard is
  reachability-based precisely so nobody "helpfully" wires the calendar into a
  trading gate. Do not.
- **Hourly adjusted views refuse** (D-32 / ADR-0029): the dividend factor's C_ref is
  the official daily closing print, and the last hourly trade is not it. The refusal
  is the feature.
- **D-12 / ADR-0008 scope is account economics, not data availability.** Hourly
  *data* existing does not reopen executable intraday trading; PDT and capital
  constraints did not move when the bars lane landed.
- **`worker/` imports nothing from `chronos`, and nothing in `src/chronos` imports
  it** — pinned in both directions by AST and subprocess probes (D-23 / ADR-0027).
  The process boundary is the safety property; do not "share a helper" across it.

## 11. Case files

Four incidents, one paragraph each, so the rules above read as scar tissue rather
than ceremony.

**The phantom merge (PRs #74/#75, un-stranded by PR #77, commit `702ab76`).** Two
stacked PRs showed MERGED. Both had merged into their stack parents, and the parents
never reached the default branch — roughly 5,500 lines of work stranded behind two
green badges. Nobody noticed from the badges; someone noticed because documents the
PRs claimed to add were not in the tree. The MERGED badge is a statement about the
PR's base, and the base of a stacked PR is not the default branch. *The rule that
exists because of this:* no stacked PRs without explicit owner ack, and every merge
is verified from its merge result with strategy-aware ancestry or changed-path
equality plus file presence, never by badge (§3, §6).

**The empty review and the wrong-tree reviewer (commit `f259189`).** A four-lens
adversarial review's first run reported zero findings — because all four reviewers
had died on a usage limit before examining anything. Zero findings and zero reviewers
produce identical output unless you check that the reviewers ran. In the same lane, a
reviewer switched the repository's checked-out branch despite read-only instructions,
and three fixes briefly landed on the wrong tree — caught only because someone
checked git state before trusting a verification. *The rule that exists because of
this:* an empty review result is not a pass, reviewers work in fresh worktrees, and
`git rev-parse HEAD` precedes trusting any measurement (§5).

**The un-closed HOLD (PR #80 → #81, commits `a616927`, `a1bc514`).** The cross-seat
ritual worked: codex HOLD, GLM PASS, and the HOLD was right — "a guard with a known
bypass is worse than none." The author fixed it, the fix was real, the PR merged. But
codex's second HOLD on #81 was fixed in `a1bc514` and never formally withdrawn: the
loop closed in code and stayed open in the record, so the history shows a standing
objection against merged work. *The rule that exists because of this:* a HOLD is
closed by the holder re-verifying in writing — the author's fix answers a HOLD, only
the holder retires it (§5).

**The archive push against a stale base (PR #82, the `-mickey-20260822` branches).**
A host-wide sync re-pushed two superseded branch tips as dated archive branches and
opened a PR against a `main` that was 98 commits stale, claiming "48 patch-unique
commits." The true number against the real default branch was zero — the divergence
had been computed against the wrong base. Everything pushed that day was already
merged; the push manufactured branch clutter, red CI, and a false preservation claim.
*The rule that exists because of this:* derive the true default by command, compute
`ahead_by` against it, treat `ahead_by=0` as "nothing to preserve," and when
something *is* worth preserving, it goes to `refs/preserve/<seat>/<name>` — never a
PR (§2, §3).

## Provenance

Written 2026-08-22; verified against `03820c3` (`origin/main` tip at authoring time).
Volatile facts in this document are expressed as commands; run them rather than
trusting prose — including this document's own examples.

| Volatile fact | Re-verify with (read-only) |
|---|---|
| Default branch name | `git ls-remote --symref origin HEAD` |
| Gate definition (canonical) | `cat .github/workflows/ci.yml` — CI is ground truth |
| `make gates` still matches CI | `grep -A1 '^gates:' Makefile` vs the workflow steps |
| Ruleset: PR-only, required `quality`, no bypass | `gh api repos/Ayyitskevin/Chronos/rulesets --jq '.[].name'` and the repo settings |
| `delete_branch_on_merge` | `gh repo view Ayyitskevin/Chronos --json deleteBranchOnMerge` |
| PR base, reviewed candidate, and merge result | `gh pr view <n> --json baseRefName,headRefOid,mergeCommit,state` |
| Exact PR changed-path set | `gh api repos/Ayyitskevin/Chronos/pulls/<n>/files --paginate --jq '.[] | .filename, (.previous_filename // empty)'` |
| Exact merge-result CI | `gh run list --commit <merge-result-sha>` and inspect the required run's jobs/log |
| Next D / ADR / R number | the scan commands in §7 — never a remembered value |
| Task-contract YAML fields | `sed -n '/^## 13/,/^## 14/p' docs/VISION_COMPLETION_PLAN.md` |
| Owner-gate list | `sed -n '/^## 11/,/^## 12/p' docs/VISION_COMPLETION_PLAN.md` |
| Test/skip baseline | `make gates` at `origin/<default>` — measured, per session |
| Preservation refs in existence | `git ls-remote origin 'refs/preserve/*'` |

If any re-verification disagrees with this document, the repo wins — update this file
using the supersede-in-place pattern (`.claude/skills/chronos-change-control` §6), and
record the change.
