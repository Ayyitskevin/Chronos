# Continuation plan — 2026-08-12

Status: **working plan, precedence tier 6.** `docs/VISION_COMPLETION_PLAN.md`
(canonical, 2026-08-01) remains the roadmap authority under the `AGENTS.md`
precedence ladder; this document sequences the next work *inside* that plan.
When they disagree, the canonical plan wins. Every fact below is a
point-in-time claim with the commit or command that grounds it — re-verify
before building on it. Supersedes `docs/CONTINUATION_PLAN_2026-08-09.md` as
the working sequence (that document's examination remains a valid record of
its date).

Authored by a Fable session at the owner's request ("lets build out a plan for
opus to build out from here"). Division of labor: Track A items are executable
by AI sessions under the plan-§13 task contract, each on its own reviewed PR;
Track B items are smaller quality-of-operation work usable as fillers; Track C
is owner-only and is presented, never worked around. Items marked
**[owner-review gate]** are safety-mechanism modifications per
`chronos-change-control` §1 — the PR body must state the safety delta
explicitly, and the owner merging the PR is the review act.

## 1. Where the autonomy loop stands at `4fda36f` (merge of PR #66)

The proposal path is now built end to end, inert by default at every layer:

    TradingView ──► bridge (ADR-0026/D-22) ──┐
                                             ├──► POST /autonomy/proposals ──► durable queue
    Claude ──► model worker (ADR-0027/D-23) ─┘         (proposer credential,      │
                                                        ADR-0023/D-24)            ▼
                                                                    runtime tick ──► ingress → STAMP
                                                                    (identity from the │ (registration)
                                                                     credential's      ▼
                                                                     registration)   15 admission checks
                                                                                       │
                                                                                       ▼
                                                                             sizing → compiler → handoff
                                                                                       │
                                                                                       ▼
                                                                          the unchanged ten-gate order plane

Merged since the 08-09 plan: the TradingView signal bridge (PR #65, with the
model worker), and the proposer registry (PR #66) — per-proposer proposal-only
credentials, provenance derived from which credential authenticated, honest
`None` evidence digest, registry-aware `mandate check`, and the fix for the
dead-ingress defect (`_fingerprint_of` read an attribute that never existed;
every real proposal POST since M7 had answered 503 `BACKEND_UNSCOPED`).

What this means for the next session: **the loop's remaining defects are now
about what the journal says, not whether the pipe exists.** The highest-value
work left inside the autonomy stack is truthfulness (item A1), evidence
binding (item A2), and revocation liveness (item A3).

## 2. Current-truth snapshot — dated 2026-08-12, REVERIFY before relying on it

| # | Fact | Re-verify with |
|---|---|---|
| 1 | Default branch `feat/wheel-dashboard-mvp` at `4fda36f`; suite baseline **3040 passed, 1 skipped**; ruff, `mypy src/chronos`, `mypy --strict worker` all clean. | `git log --oneline -1 origin/feat/wheel-dashboard-mvp`; `.venv/bin/pytest -q` |
| 2 | Plan §6 scoreboard: findings 1, 2, 6 CLOSED (D-20, doc fix, D-24); finding 3 doc-half closed / code-half owner-gated; findings 4, 7, 8 OPEN behind proposed ADRs 0022, 0021, 0024; **finding 5 OPEN and unowned by any ADR** — the only §6 code defect an AI session can take next. | `grep -n "Status" docs/adr/ADR-002{1,2,4}-*.md`; item A1's greps below |
| 3 | Supervisor still journals COMPLETE on any non-exception handoff return: only `except Exception` maps to ORDER_PLANE_REFUSED (`loop.py:428-448`), then activity is counted and COMPLETE recorded (`:450-476`) even for `SubmissionOutcome(submitted=False)`. | `sed -n '428,476p' src/chronos/supervisor/loop.py` |
| 4 | Evidence binding is uniform: every identity (static or registered) stamps `evidence_bundle_id="owner-workspace"`, digest `None`; `EvidenceBundle` exists as a type (`chronos.autonomy.evidence:130`) but nothing issues, stores, or expires bundles per job. | `grep -n "owner-workspace" src/chronos/api/autonomy_wiring.py` |
| 5 | The proposer registry is a boot-time snapshot on both planes: expiry transitions live, disable/delete lands at restart. Disclosed in ADR-0023's acceptance note, D-24, R-48(c). | `grep -n "boot-time snapshot" RISK_REGISTER.md` |
| 6 | Worker: token usage is logged, never capped (`worker/model.py:212-213`); policy content is unpinned (registration `prompt_version` is an owner-typed label). Terminal polls at 5 s (`POLL_MS = 5000`). | `grep -n "usage.get" worker/model.py`; `grep -n "POLL_MS" src/chronos/terminal/static/*.js` |
| 7 | No real IBKR gateway has ever been connected; every gateway-facing control remains fixture-verified (MITIGATED ≠ CLOSED). The §7 read-only campaign is still the single highest-leverage step in the repository and is owner-gated. | `grep -n "never been exercised" docs/limitations.md` |
| 8 | Account snapshot ≈ USD 110 vs the ~$3k premise; Phase-0 economics and scope are unfrozen; zero strategies selected; the wheel has zero backtest evidence. Unchanged from the canonical plan. | plan §5, §11; `docs/STRATEGY_SELECTION.md` |

## 3. Track A — the ordered build queue for AI sessions

Work these in order unless the owner redirects. One item per PR; every PR
follows the §13 contract (frozen acceptance criteria before code, exercised
tests, revert-the-fix proofs, measured counts, honest residuals).

### A1. Typed handoff outcomes — the supervisor stops journaling refusals as COMPLETE

**[owner-review gate]** (safety-mechanism modification; plan §6 finding 5).

> **BUILT 2026-08-13, awaiting the owner's merge** (the merge IS the review act
> for an owner-review-gated item). Branch `claude/chronos-typed-handoff-a1`,
> based on `f4ac14a`. Delivered as specified: four supervisor-owned dispositions
> in the new `chronos.supervisor.handoff`, translated at the
> `autonomy_wiring` seam (`classify_submission_outcome`) so no order-plane type
> reaches the supervisor; two additive `CycleStage` members and new refusal codes
> with nothing existing weakened; the counting rule stated once and documented in
> the open — an attempt is consumed exactly when the supervisor cannot prove
> nothing reached the wire, so REFUSED_NOT_SENT counts nothing while
> SENT_AMBIGUOUS counts and alerts CRITICAL. Evidence:
> `tests/safety/test_typed_handoff_outcomes_exercised.py` (42 tests), six
> conjuncts each reverted alone and watched fail, isolation suite green, gates
> green at 3082 passed / 1 skipped (baseline 3040 / 1). Governance: R-49,
> plan §6 finding-5 annotation. Residuals are disclosed in R-49 — most
> importantly that an exception out of a *non-wiring* handoff callable is still
> journaled as not-sent, and that the post-submission typed outcomes (partial
> fill, full fill, cancellation, late commission) are out of scope here because
> they belong to the order plane's lifecycle tracker.

*Why first:* the journal is the only thing that can answer "why did it not
trade," and today it answers falsely for every non-exception refusal: a
`SubmissionOutcome(submitted=False)` (venue rejection, read-only lease,
kill-switch refusal at the boundary) records COMPLETE and **counts an
activity attempt**. Every downstream ambition — evidence-bound promotion,
owner trust in the terminal's story — leans on this journal.

*Design constraints (from plan §6 design outcomes + `chronos-architecture-contract`):*
- The supervisor must NOT import order-plane types. Inspect the handoff result
  through a duck-typed protocol (e.g. `getattr(result, "submitted", None)`),
  or have `order_plane_handoff` in `autonomy_wiring` (app plane, allowed to
  import both) translate the outcome into a supervisor-owned typed result.
  Prefer the wiring translation: the seam already exists and keeps the
  supervisor plane-clean.
- Distinguish at least: SUBMITTED (confirmed send), REFUSED_NOT_SENT (refused
  before the wire — must NOT count an `orders_submitted` attempt),
  SENT_AMBIGUOUS (transmitted but unconfirmed — must count, must alert),
  REJECTED_AFTER_SEND (counts; the current comment's "sent then rejected"
  case). Decide and document the activity-counting rule per outcome in the
  ADR/PR — the current "count everything that didn't raise" is wrong in both
  directions.
- New `CycleStage`/refusal codes are additive; no existing refusal weakens.

*Acceptance:* exercised tests that each outcome class journals its own stage
and refusal and that `orders_submitted` advances only per the documented rule;
a revert-the-fix proof that restoring the old behavior fails the new tests;
`test_autonomy_contracts.py` isolation suite still green (no order-plane
import appears in `chronos.supervisor`).

*Size:* M. *Files:* `supervisor/loop.py`, `supervisor/runtime.py` (report
fields), `api/autonomy_wiring.py` (translation), tests, RISK_REGISTER row,
plan §6 finding-5 annotation.

### A2. ADR-0028: the per-job evidence protocol — evidence binding becomes real

**[owner-review gate]** (admission-semantics change; the ADR is the review
vehicle — draft it first, get the owner's acceptance, then build).

> **BUILT 2026-08-14, awaiting the owner's merge** (the merge IS the review act
> for an owner-review-gated item). Branch
> `claude/chronos-evidence-protocol-a2`, based on `b5d61dd` (the merge of PR #69,
> which landed the ADR draft; A1 landed earlier as PR #68). The ADR was accepted
> in the owner's own words — "merged 69, go with option C, have opus build it"
> (Kevin, 2026-08-14) — so this PR carries the acceptance flip as well as the
> build, following the ADR-0023 precedent. Delivered as specified, Option C
> whole: the durable hash-chained `autonomy_evidence_bundles` record (migration
> 0008, SCHEMA_VERSION 8 → 9), `POST /autonomy/evidence` composing and digesting
> the exact bytes it serves, resolution at STAMP against the drain's clock with
> provenance stamped from the record, and admission check 9's missing
> payload-side half. Every recommended parameter honored: 300 s TTL judged at the
> drain (ceiling 3600 s, out-of-range refuses to start), the bridge on its own
> `alert_attested` kind that may back a proposal and never a promotion rung, the
> unset posture byte-identical to `b5d61dd` proven against a recorded journal row,
> and issuance bounded by a per-proposer cap plus a retention rule that never
> prunes the hash-chained issuance record. Evidence:
> `tests/safety/test_evidence_bundles_exercised.py` (25 tests), 18/18 conjuncts
> each reverted alone and watched fail, gates green at 3108 passed / 1 skipped
> (baseline 3082 / 1). Governance: ADR-0028 status flipped in place, D-25, R-50,
> R-48 amended with the issuance route as its one named, tested exception, plan
> §6 finding-6 annotation. Two additions beyond the ADR's text, both disclosed
> rather than absorbed: a third additive refusal code
> (`EVIDENCE_BUNDLE_KIND_MISMATCH`, so a relabelled attestation stays
> distinguishable from a forged digest), and the fix for a **second live
> dead-route defect** the build surfaced — `chronos.api.bars.provider_for` cached
> by assigning a new attribute to the `slots=True` `BackendState`, so
> `GET /terminal/bars` had answered 500 for every symbol, on every backend, since
> the route existed, with no test ever calling it. Residuals are in R-50 — most
> importantly that **equality catches accident, not malice**, and that **attested
> is not witnessed**.

*Why:* ADR-0023 closed identity and deliberately left evidence uniform: every
proposal cites the placeholder bundle (`owner-workspace`, digest `None`).
ADR-0016's promotion bindings and the "no promotion artifact while identity or
evidence is a constant" rule stay unachievable until a proposal's evidence is
bound per job. This is the last constant in provenance.

*Shape to propose (the worker already computes the digest):* the backend
issues an `EvidenceBundle` id + digest when a proposer reads evidence (or
accepts a worker-computed digest of the exact bytes served, which the worker
already produces as `worker_evidence_snapshot`); the proposal must cite it;
admission check 9 compares against the issued record with an expiry, refusing
stale or unissued evidence. Decide in the ADR: who computes the digest
(backend-served bytes vs worker-canonicalized), where issued bundles persist
(durable table + hash chain), expiry length, and what the bridge (whose
"evidence" is the alert itself) does — likely its own bundle kind.

*Acceptance:* exercised tests that an unissued digest refuses, an expired
bundle refuses, the digest of the actual served bytes verifies end-to-end
through the real drain, and the registry-off posture still works.

*Size:* L (ADR + migration + route/worker/admission changes). Do not start it
in the same PR as anything else.

### A3. Live proposer revocation — `proposer revoke`, DB-backed

> **BUILT 2026-08-14, awaiting the owner's merge.** Branch
> `claude/chronos-proposer-revocation-a3`, based on `b993789` (the merge of
> PR #70). Not owner-review-gated — the change only removes authority — but the
> CLI grew its first mutating command, which is disclosed loudly rather than
> absorbed. Delivered as specified: `chronos.cli proposer revoke` writes one row
> in `autonomy_proposer_revocations` (migration 0009, SCHEMA_VERSION 9 → 10) and
> one hash-chained record; `require_proposer` refuses at the route (401) and the
> drain-time resolver at STAMP (`PROPOSER_REVOKED`, additive), both reading the
> ledger **per check rather than per boot** — which is the whole point — and both
> fail-closed when it cannot be read. **Keyed on the credential hash, not the
> proposer id** (D-26): revoking burns the secret that leaked, not the name, so
> re-minting for the same proposer is a working recovery path instead of a
> permanently poisoned id. `mint` and `check` stay stdout-only and the
> writes-nothing pin was narrowed to them rather than deleted. Evidence:
> `tests/safety/test_proposer_revocation_exercised.py` (15), ten conjuncts each
> reverted alone and watched fail, gates green at 3124 passed / 1 skipped
> (baseline 3108 / 1 at `b993789`, measured). Governance: D-26, R-51, R-48
> residual (c) narrowed in place, and the now-false restart claims corrected in
> `docs/model_worker.md`, ADR-0023's acceptance note, and D-24. Residuals are in
> R-51 — most importantly that **the rest of the registry is still a boot-time
> snapshot** (enabling or re-registering still needs a restart, deliberately),
> that revocation is **permanent with no un-revoke**, and that the ledger read
> puts **database health on the proposal path**. One disclosed asymmetry, not
> resolved here: mandate revocation is a terminal route, proposer revocation is
> a CLI command; whether the terminal should also surface it is a later
> question.

*Why:* R-48 residual (c): the registry is a boot-time snapshot, so disabling a
leaked credential mid-session requires a restart today. Mandate revocation
already has the right shape — a durable act the running process honors.

*Shape:* a `chronos.cli proposer revoke --proposer-id X` writes a revocation
row (hash-chained, like mandate revocation); `require_proposer` and the drain
resolver check the revocation table in addition to the snapshot. Revocation is
permanent for that registration (re-registering is a new owner edit + restart).
This is authority-REMOVING, so it is not owner-gated to build — but the CLI
grows a mutating command, so say so loudly in the PR and keep `mint`/`check`
stdout-only.

*Acceptance:* exercised tests that a revoked registration refuses at the route
(401) and at STAMP without restart; that revocation survives restart; that
`proposer check` reports REVOKED.

*Size:* M. *Files:* `supervisor/proposers.py` or new durable module,
`cli/proposer_commands.py`, `api/auth.py`, `api/autonomy_wiring.py`,
migration, tests.

### A4. Policy content pinning — `mint --policy-file` digests the prompt

*Why:* R-47 residual (b): `prompt_version` is an owner-typed label; nothing
binds it to the policy file's content, so policy edits are unattributable
unless the owner remembers to bump it.

*Shape (small, honest):* `proposer mint` (and a new `proposer fingerprint
--policy-file`) accepts `--policy-file` and derives `prompt_version` as
`sha256(policy bytes)[:16]`; docs tell the owner to re-mint (or at least
re-pin) on policy edits, and `mandate check` already surfaces the mismatch.
No backend change; no claim that the worker proves which policy it ran —
disclose that bound plainly.

*Size:* S.

### A5. Worker cost ceiling — a cap, not a log

*Why:* R-47 residual (e): cost is logged, never capped; cadence is the only
throttle.

*Shape:* `CHRONOS_WORKER_MAX_DAILY_TOKENS` (or USD via published prices —
tokens is more honest, prices drift). The worker accumulates usage per UTC day
in memory; at the ceiling it stops calling the model (cycles log
`COST_CEILING` and skip thinking) until the day rolls. Fail-closed: an
unparsable ceiling refuses to start. Unset means today's behavior, disclosed.

*Size:* S. *Files:* `worker/config.py`, `worker/model.py` or `worker/cycle.py`,
tests, `docs/model_worker.md`.

## 4. Track B — operate-and-observe fillers (any order, low risk)

- **B1. Terminal streaming:** replace the 5 s `POLL_MS` with SSE from the
  backend (same-origin, read-only, no new credential surface — the session
  cookie already scopes to `/terminal`). Keep polling as fallback.
- **B2. Mandate authoring aid in the terminal:** a read-only panel that runs
  the `mandate_check.review_mandate` logic against a pasted document and
  renders the findings. It validates and previews; it must not write or
  activate anything (ADR-0016 §3 stands).
- **B3. Off-host alerting sidecar:** REQUIRES A NEW ADR + owner approval — a
  network channel out of the alert plane is a deliberate authority change.
  Design as an out-of-process tailer of `data/owner_alerts.jsonl` (ntfy/email);
  never add a network import to the trading process (structural test pins
  delivery local-only). Draft the ADR, present, wait.

## 5. Track C — the owner-decision queue (presented, never worked around)

1. **The real-gateway read-only campaign** (§7; execute via
   `chronos-real-gateway-campaign`). Still the single highest-leverage step:
   nothing leaves MITIGATED, no promotion rung is reachable, and the
   irreplaceable option forward-capture clock cannot start until it runs.
2. **ADR-0022 — which arming model wins** (finding 4). Options are written;
   picking one unlocks a build item.
3. **ADR-0021 — dead economic fields** (finding 7): enforce (needs the durable
   position-management lifecycle — L work once decided) or forbid.
4. **ADR-0024 — evidence-bound promotion** (finding 8). Becomes buildable
   after A2 lands (a promotion artifact needs real identity AND real evidence;
   identity is done, evidence is A2).
5. **Kill-switch missing-file posture** (finding 3 code half): boots
   DISENGAGED on a missing file today; making recovery boot kill-engaged is a
   pinned-safety-mechanism change only the owner can direct.
6. **Capital envelope** (USD 110 vs ~$3k premise) and **Phase-0 economics /
   scope freeze** — prerequisites for any strategy-evidence work meaning
   anything.
7. **Registering the proposers** (5-minute owner acts, now available): mint
   credentials for the worker and bridge, set `AUTONOMY_PROPOSERS_FILE`,
   author mandate pins against the registration (`mandate check` verifies).

## 6. Anti-goals — unchanged and binding

No new asset vocabulary; no promotion transfer across families; no threshold
edits after observation; no weakening any gate to unblock progress; no options
simulator built on unobtainable data (ADR-0012 — forward capture is the only
path); feature breadth off the dependency chain advances neither 10/10 score.
A session that cannot find its task on this plan should re-read plan §3's
dependency chain before inventing one.

## 7. Session protocol for the executing model

1. Read `CLAUDE.md` → `AGENTS.md` → `docs/VISION_COMPLETION_PLAN.md`, then
   load `chronos-priorities-and-roadmap` and the domain skill for the item
   (A1/A2: `chronos-autonomy-and-mandates`; A3: also `chronos-change-control`;
   B3: `chronos-change-control` first).
2. Re-verify this plan's §2 snapshot; the repo wins over this document.
3. Branch discipline: restart the working branch from the live default branch
   (`git fetch origin feat/wheel-dashboard-mvp && git checkout -B <branch>
   origin/feat/wheel-dashboard-mvp`); one item per PR; a merged PR is never
   reused.
4. Gates per PR: `ruff check .`, `ruff format --check .`, `mypy src/chronos`,
   `mypy --strict worker`, full `pytest -q` against the §2 baseline; measured
   test counts (`pytest --collect-only -q | grep -c '::'`), never estimates.
5. Claim discipline: "done" only with the evidence artifact named; MITIGATED ≠
   CLOSED; a correct refusal is success; residuals disclosed in the PR body
   and RISK_REGISTER row.
