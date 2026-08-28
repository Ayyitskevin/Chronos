---
name: chronos-docs-map
description: >
  The Chronos documentation map: which doc to trust for what, the authority spine, the
  complete stale/contradiction ledger, and the house doc style. Load this BEFORE citing
  any repo document as authority, and whenever you ask "which doc covers X", "where is X
  documented", "is this doc current", "can I trust this doc", "these docs conflict /
  contradict each other", "this doc looks stale", "what's the real test count", or you
  are about to quote TEST_RESULTS.md, HANDOFF.md, TASKS.md, ARCHITECTURE.md /
  architecture.md, INCIDENT_RESPONSE.md, BACKUP_AND_RECOVERY.md, SECURITY.md, any IBKR
  doc, or any capital/account figure. Also load it when asked to "update the docs",
  add a banner, or fix a stale claim — it defines the supersession style. NOT for
  deciding what to work on (chronos-priorities-and-roadmap), change authority
  (chronos-change-control), or the historical narrative (chronos-failure-archaeology).
---

# Chronos docs map — what to trust, what lies, and how to fix it

Base inventory verified against HEAD `47a8d72`, 2026-08-02. Ledger #7 and the validation-doc
entries were re-verified against exact main `d44fc4ac7d2f`, 2026-08-28. Every quote below carries
`file:line`; re-verify line numbers before editing (docs move).

Chronos has ~65 markdown documents written across an 11-milestone sprint by multiple
agent sessions. Most are honest. Several present old state as current truth, and a few
contradict the code on safety-critical procedures. This skill is the map: the reading
order, the per-document trust rating, the complete contradiction ledger, and the house
style for fixing a stale doc without destroying its history.

## When NOT to use this skill

| You actually want | Use instead |
|---|---|
| What to work on next, current status snapshot | chronos-priorities-and-roadmap |
| Whether you MAY change something; ADR/owner-gate process; which doc *binds* when two conflict | chronos-change-control |
| The story of past defects, pivots, dead ends | chronos-failure-archaeology |
| Test counts, suite map, what counts as evidence | chronos-validation-and-qa |
| Kill/halt/arm/revoke operational procedures | chronos-run-and-operate |
| Scripts that measure doc drift instead of eyeballing it | chronos-diagnostics |

This skill tells you which document to open and how far to trust it. It does not decide
priorities and it does not grant change authority.

## The authority spine — read in this order

1. **AGENTS.md** — the repository contract: read-first list, non-negotiable build rules,
   and the document-precedence ladder (AGENTS.md:41-54). Everything else is subordinate.
2. **docs/VISION_COMPLETION_PLAN.md** — "Status: canonical execution plan, Effective
   2026-08-01". The north star: two 10/10 definitions, the current-truth snapshot (§2),
   the Phase-1 defect list (§6), owner gates (§11), the agent task contract (§13).
3. **DECISIONS.md + the ADRs relevant to your change** — accepted authority and
   architecture. For anything touching autonomy read ADR-0016 and ADR-0017 completely;
   ADR-0009 for the live branch; ADR-0007 for the deterministic platform's mode lock.
4. **docs/safety.md + docs/limitations.md + RISK_REGISTER.md** — controls, honest
   capability boundaries, disclosed residuals. (Skip RISK_REGISTER R-01's frozen note;
   trust the dated R-24..R-42 rows after spot-checking code — see Ledger #12.)
5. **README.md ("Where that stands today") + CHANGELOG.md + docs/TEST_RESULTS.md** — README is
   the freshest capability narrative, CHANGELOG preserves build history, and TEST_RESULTS carries
   the latest dated gate artifact. Counts are never live state; re-run `make gates` before citing
   another tree (suite map: chronos-validation-and-qa §2).

### The precedence ladder (AGENTS.md:41-54, restated verbatim in structure)

When repository documents disagree:

1. Explicit owner direction within safety and human-approval boundaries.
2. **Current executable facts and unresolved safety/security defects.** A live defect
   always blocks promotion; roadmap or ADR prose cannot waive it.
3. Accepted ADRs plus DECISIONS.md for intended authority and architecture.
4. docs/safety.md, docs/limitations.md, RISK_REGISTER.md for controls and residuals.
5. docs/VISION_COMPLETION_PLAN.md for roadmap order and completion criteria.
6. Historical plans, task boards, briefs, and handoffs — context only.

> "Stop and surface a contradiction; never average incompatible instructions."
> (AGENTS.md:54)

Practical consequence used throughout the ledger below: **when prose and code disagree,
the code is the fact (tier 2) and the prose is aspiration or history** — but whether the
code *should* change is a separate question that may need an owner decision
(chronos-change-control).

## Trust vocabulary

- **CURRENT** — safe to cite as of 2026-08-02.
- **HISTORICAL-HONEST** — carries a banner labeling itself history; cite as history only.
- **STALE-UNBANNERED** — *danger class*: presents old state as current, no adequate
  label. Never cite without re-verifying against code.
- **MIXED** — banner or current sections exist, but the body contains unmarked stale
  claims. Cite specific lines only after checking them.

The complete per-document table (every root .md, all 34 docs/*.md, all 19 ADRs, with
tier, role, and status) is in **references/doc-inventory.md**. The danger list is below.

## Never cite without re-verifying (the compact danger list)

STALE-UNBANNERED (presents old state as current — the worst class):

- **docs/INCIDENT_RESPONSE.md** and **docs/BACKUP_AND_RECOVERY.md** for anything on the
  live order plane — they only know the deterministic platform's halt (Ledger #1).
- **docs/IBKR_INTEGRATION.md** ("ONLY code path", Ledger #9), **docs/IBKR_RUNBOOK.md**
  ("no service loop exists yet"), **docs/ibkr_setup.md** ("read-only until M5-7"),
  **docs/DEPLOYMENT.md** §"Future work" ("does not exist") — all Ledger #10.
- **docs/QQQ_GOLD_SPY_CAPABILITY_BRIEF.md** — unbannered "standing instruction set"
  (Ledger #13).

MIXED (check the specific line you are about to cite):

- **HANDOFF.md** (internally contradictory, Ledger #6), **docs/ARCHITECTURE.md** autonomy
  paragraph (Ledger #3), **docs/live_trading_runbook.md** §"Autonomous operation"
  (stale in both directions, Ledger #2), **docs/SECURITY.md** (Ledger #11),
  **RISK_REGISTER.md R-01**, **ASSUMPTIONS.md A-10/A-21/A-22** (Ledger #8),
  **docs/GO_LIVE_CHECKLIST.md** body statuses
  ("current as of 2026-07-17", Ledger #4), **TASKS.md** counts and Open list (Ledger #5).

Always, regardless of document (AGENTS.md:35-36): any **branch name, test count,
capability claim, or "does not exist yet" claim** anywhere is a point-in-time finding —
"Branches, capabilities, broker APIs, data, and prior handoffs are claims, not live
state." Re-verify against the current commit before acting on it.

---

# The contradiction ledger

Numbered, both sides quoted, winner under the AGENTS.md ladder, and a status:
**fix-candidate** (a doc edit any session may propose under house style) vs
**owner-decision-needed** (surfacing only — do not resolve silently; route via
chronos-change-control). Cross-check entries #1, #2, #8 against
VISION_COMPLETION_PLAN.md §6 — the plan itself records them as open Phase-1 findings.

## #1 — Incident/backup runbooks invoke the wrong halt and overstate restore safety

*The highest-consequence documentation defect in the repo.* Two subsystems have two
different emergency stops with OPPOSITE missing-file defaults, and both runbooks only
know the safe-defaulting one.

Side A — the runbooks:

> "**Halt. Always safe, never harmful:** `python -m chronos.cli halt --reason …` — The
> halt persists across restarts and blocks all new order generation."
> (docs/INCIDENT_RESPONSE.md:19-23)

> "restore must never auto-resume trading, and the code guarantees it — a restored (or
> missing) halt file reads as HALTED" (docs/BACKUP_AND_RECOVERY.md:3-5)

Side B — the code:

> `except FileNotFoundError:  # No file: a fresh deploy is DISENGAGED (trades subject to
> other gates).  return KillSwitchState(engaged=False)`
> (src/chronos/orders/kill_switch.py:83-85)

`chronos.cli halt` writes `data/platform_halt.json` in `chronos.control` — the
deterministic platform. The plane that can actually transmit (`chronos.orders`) has a
separate kill switch (`data/live_kill_switch.json`, settings.py:113) that the incident
runbook never mentions (zero occurrences of "kill" in INCIDENT_RESPONSE.md). And under
ADR-0017, "A running backend plus a valid mandate file is now sufficient to trade; there
is no per-boot ritual" (ADR-0017:83-84) — so a restore of a mandate-configured backend
with no kill-switch file CAN auto-resume autonomous trading. BACKUP_AND_RECOVERY.md's
"What to back up" table lists `data/platform_halt.json` (:13) but omits
`data/live_kill_switch.json` entirely.

**Winner:** the code (tier 2). The vision plan already records both defects as open
Phase-1 findings #2 and #3 (VISION_COMPLETION_PLAN.md:146-150).
**Status: fix-candidate** for the docs (scope banners + kill-switch procedures at the
stale sites). The matching *code* change ("recovery must always boot kill-engaged") is a
safety-mechanism change → owner gate, chronos-change-control. Operational kill/halt
procedures live in chronos-run-and-operate.

## #2 — "Mandate replaces arming" prose vs code requiring a current arm

Side A — three docs plus an ADR say an active AutonomyMandate substitutes for live gates
7 (session arming) and 8 (per-order confirmation):

> "an active owner-authored **AutonomyMandate** replaces gates 7 (session arming) and 8
> (per-order confirmation) — **and only those two** — inside its bounds."
> (docs/live_trading_runbook.md:21-24; same claim at docs/AI_QUANT_GAME_PLAN.md:260-264
> and docs/LIVE_WHEEL_GAME_PLAN.md:131-134; ADR-0017:83-84 "no per-boot ritual")

Side B — the code implements no such substitution. `src/chronos/orders/` contains zero
occurrences of "mandate" (verified by grep), and the live gate walk unconditionally
requires an unexpired arm and a valid typed confirmation:

> `armed = self._live_arming.is_armed(now=fresh_now)` (src/chronos/orders/submission.py:441)
> feeding `LiveGateInputs(… armed=armed, confirmation_valid=confirmation_ok …)` (:472-477)

**Winner:** the code (tier 2): autonomous LIVE submission is blocked without a session
arm. The prose overstates *operability*, not safety — the safe direction, but still a
contradiction. The plan records it as Phase-1 finding #4: "Choose and implement one
reviewed authority model" (VISION_COMPLETION_PLAN.md:151-153).
**Status: OWNER-DECISION-NEEDED.** Which authority model wins is the owner's call; no
session may "fix" either side silently. Details of the mandate machinery:
chronos-autonomy-and-mandates.

## #3 — docs/ARCHITECTURE.md frozen at M1 vs the delivered M2-M7.5 stack

Two architecture files differ only by case (a hazard on case-insensitive filesystems).
docs/architecture.md (lowercase) is HISTORICAL-HONEST — its scope note (:3-11) says to
read it as the M1-M10 dashboard posture. docs/ARCHITECTURE.md (uppercase) has **no
banner** and its authority-model paragraph is frozen at M1:

> "The gateway is Milestone 2; as of Milestone 1 the contracts (`chronos.autonomy`)
> exist and are wired into nothing." (docs/ARCHITECTURE.md:28-29)

vs:

> "the whole autonomy stack is built and wired — contracts (M1), gateway/admission/sizing
> (M2), durable state (M3), the compiler and queue (M4) … and the app-plane wiring
> (M7.5/ADR-0017). A backend booted with a valid `AUTONOMY_MANDATE_FILE` auto-activates
> it and drives the autonomy tick" (README.md:20-24; CHANGELOG.md:416 confirms M7.5)

**Winner:** README/CHANGELOG (tier 2 status evidence). ARCHITECTURE.md's platform-plane
description remains useful; only the autonomy paragraph misleads.
**Status: fix-candidate** — dateline that paragraph or add a scoped correction.

## #4 — GO_LIVE_CHECKLIST.md: model correction, frozen body

The closing claim was corrected in place, house-style (preserved, struck, dated):

> "~~No item on any checklist in this repository authorizes live trading.~~ **Corrected
> 2026-07-25 — see the scope note at the top.** No item on *this* checklist authorizes
> live trading … Repository-wide the statement is false: the `chronos.orders` plane has
> a gated live branch (ADR-0009)…" (docs/GO_LIVE_CHECKLIST.md:185-189)

But the body statuses are explicitly frozen: "Statuses are honest and current as of
2026-07-17" (:26-27) — e.g. Gate 0 cites "1115 passed" (:59-60) and Gate 4 still frames
the owner decision around "a ~USD 3,000 cash account" (:179).

**Winner:** no live conflict — the banner and correction already concede the staleness.
**Status: fix-candidate** (refresh statuses or extend the banner); the checklist's
reviewed-release *doctrine* is retained by ADR-0016 §7 and is current.

## #5 — TASKS.md legacy board vs the VISION plan work queue

TASKS.md is honestly bannered: "**Legacy board.** … not repository-wide current truth.
All new work must be sequenced and judged against docs/VISION_COMPLETION_PLAN.md"
(TASKS.md:3-6). But its Open list still misleads a skimmer:

> "Future work (out of scope this build): the long-running shadow/paper service loop"
> (TASKS.md:56-60)

That service loop **exists** — `src/chronos/service/` (`__main__.py`, `cycle.py`,
`startup.py`), delivered in M2 and described at HANDOFF.md:43-48 and
GO_LIVE_CHECKLIST.md:117-121. Its 951/1158 test counts are historical snapshots, not
current evidence.

**Winner:** VISION_COMPLETION_PLAN §§5-6 is the only work queue; TASKS.md is context.
**Status: fix-candidate** (annotate the Open list entries as delivered/stale).
The actual work queue lives in chronos-priorities-and-roadmap.

## #6 — HANDOFF.md contradicts itself

The banner is honest but under-scoped ("as of 2026-07-17 and is partly stale",
HANDOFF.md:3-4) — the body mixes three eras and contradicts itself:

> "Autonomous operation is **not** yet implemented — M1 delivered the governance and the
> `chronos.autonomy` contracts only, wired into nothing." (HANDOFF.md:22-23)

> "the gateway is a gate with nothing routed through it — so no part of the system
> trades autonomously today." (HANDOFF.md:151-157)

…while the SAME file's posture section describes the M9-M11 fixes (2026-07-26/27):
"**R-27 was MITIGATED in M11** — both IBKR adapters now screen each qualified option's
deliverable" (HANDOFF.md:144-146). CHANGELOG.md:598 records "M4: the gate finally has
something routed through it (2026-07-25)". Its test count is stale: "1885 passed …
(2026-07-25)" (HANDOFF.md:24). Its capital premise is stale: "a ~USD 3,000 cash account"
(HANDOFF.md:121).

**Winner:** README + CHANGELOG for anything HANDOFF claims about current state.
**Status: fix-candidate** (either update the body wholesale or widen the banner to name
the internally-mixed eras).

## #7 — TEST_RESULTS.md and TEST_PLAN.md carried stale validation state — RESOLVED 2026-08-28

The original defect was an unbannered "current" test result that stopped at M2a while
TEST_PLAN.md still described the results as forthcoming, the safety suite as 29 cases, and CI as
four gates under a 10-minute timeout.

The 2026-08-28 refresh replaces the operational CI instructions with the executable six-target
`make gates` contract, records the 20-minute workflow timeout and installed-wheel gate, updates the
single safety file to its observed 36 cases, removes volatile whole-suite counts from the plan, and
dates TEST_RESULTS against exact main `d44fc4ac7d2f`: 4239 passed, one owner-gated skip, 294+10
mypy files, 548 formatted files, and a passing installed-wheel smoke. Older measurements remain
explicitly historical.

**Winner:** executable CI + a fresh `make gates` run. Counts drift; TEST_RESULTS is a dated artifact,
not a live counter. **Status: RESOLVED** for the stale-current presentation. Suite map:
chronos-validation-and-qa.

## #8 — The capital premise: ~USD 3,000 vs ~USD 110

Current fact (tier 2): "The last documented account snapshot was approximately USD 110.
That makes cash-secured options and most futures economically unavailable without a
separate owner capital decision. Engineering must not disguise that constraint."
(VISION_COMPLETION_PLAN.md:68-70; same figure in ADR-0016:417, ADR-0017:267,
ADR-0018:82,266, ADR-0019:162).

Still carrying the ~27x-larger premise, unamended:

| Site | Stale text |
|---|---|
| ASSUMPTIONS.md:28 | "**A-10 — Account type.** Assumed a small IBKR **cash account** (~USD 3,000)" — never amended, unlike A-12 |
| ASSUMPTIONS.md:51,56 | A-21/A-22 cost/sizing math "On a USD 3,000 account" |
| RISK_REGISTER.md:17 | "R-10 \| USD 3k account economics … ACCEPTED" (economics get *worse* at $110 — stale in the conservative direction) |
| docs/GO_LIVE_CHECKLIST.md:179 | owner decision framed around "a ~USD 3,000 cash account" |
| HANDOFF.md:121 | same framing |
| docs/RESEARCH_REPORT.md:42,176,180,233 | cost model premised on USD 3,000 |
| docs/adr/ADR-0008:9,32 | candidate scope premised on "~USD 3,000" |
| docs/OPERATIONS.md:87, docs/DEPLOYMENT.md:97, docs/IBKR_RUNBOOK.md:175,182 | `--cash 3000` CLI examples (cosmetic) |

**Status: OWNER-DECISION-NEEDED — a LIVE, unresolved decision.** Flag it wherever it
matters; NEVER quietly assume either number in analysis, sizing, or research. (Contract
non-negotiable #7.) The owner-decision queue lives in chronos-priorities-and-roadmap.

## #9 — IBKR_INTEGRATION.md's "ONLY code path" claim

> "The ONLY code path in the repository that can hand an equity order to IBKR, and only
> to a verified paper account." (docs/IBKR_INTEGRATION.md:17, about
> `chronos/execution/brokers/ibkr_paper.py`)

False since M5-M7: the one reachable `transmit=True` lives at the `chronos.orders`
submission boundary (README.md:107-110), and the adapter this doc praises is
**quarantined** — "`IBKRPaperExecutionAdapter` now refuses construction unless passed
`quarantine_ack=True`, which **no** module in `src/` passes" (RISK_REGISTER.md R-28).

**Winner:** code + RISK_REGISTER. **Status: fix-candidate** (banner or scoped
correction). Adapter map: chronos-ibkr-boundary.

## #10 — Three docs deny the service loop that exists

> "no long-running shadow/paper service loop exists yet" (docs/IBKR_RUNBOOK.md:8-9)
> "Both are read-only until the Milestone 5-7 order service exists" (docs/ibkr_setup.md:5-6)
> "## Future work — shadow/paper service (NOT IMPLEMENTED)" … "`ExecStart=… python -m
> chronos.service --mode shadow   # does not exist`" (docs/DEPLOYMENT.md:133,152)

All three are false: `python -m chronos.service` is the M2 supervised service loop
(`src/chronos/service/__main__.py` — module docstring "the supervised shadow/paper
service loop", `--mode` flag at :46, default SHADOW/NO_ORDERS), and M5-M7 delivered the
order service (README.md:68-90). GO_LIVE_CHECKLIST.md:117-121 already describes the loop
as existing.

**Winner:** the code. **Status: fix-candidate.** Note the failure direction: these docs
*understate* capability — dangerous because an operator planning incident response or
deployment around "nothing can run unattended" is wrong.

## #11 — SECURITY.md is stale twice, despite self-declaring "as implemented"

SECURITY.md:4-5 promises "describes only controls that exist in code today". Two claims
have rotted:

1. > "`ALLOW_LIVE_TRADING=true` makes settings validation raise
   > (`src/chronos/config/settings.py`)" (docs/SECURITY.md:40)

   Now conjunction-gated, not an unconditional raise: settings validation raises **only
   if** the full ADR-0009 live conjunction is unmet (broker=ibkr + official adapter +
   LIVE environment + ALLOW_ORDER_TRANSMIT + U-pattern allowlisted account + arming +
   typed-confirmation flags) — src/chronos/config/settings.py:165-200.

2. > "Neither system implements remote access, authentication, or multi-user features"
   > (docs/SECURITY.md:50)

   Authentication now exists: a per-installation local API token (`X-Chronos-Token`,
   src/chronos/api/auth.py) and terminal browser sessions via a login endpoint +
   httpOnly cookie scoped to `/terminal` (src/chronos/api/terminal_session.py, M8b).
   Remote access and multi-user remain absent — that half still holds.

**Winner:** the code. **Status: fix-candidate** — but any SECURITY.md edit is
security-sensitive → owner review per AGENTS.md build rules (chronos-change-control).

## #12 — Internal register and ADR status drift

**a. RISK_REGISTER R-01 vs R-38.** R-01's note (RISK_REGISTER.md:8, "restated
2026-07-25") still says the mandate is "**not yet an enforced control**: no autonomy
startup path, mandate store, or gateway exists (M2) … `DEFAULT_AUTONOMY_MODE` is
`SHADOW` as a contract constant with no reader yet." The same register's R-38
(RISK_REGISTER.md:46, M7.5) says "A valid, account-matching `AUTONOMY_MANDATE_FILE`
auto-activates on every boot". Both cannot be current; **R-38 matches the code**.
Status: fix-candidate (restate R-01's note).

**b. ADR-0012 / ADR-0014 / ADR-0015 say "proposed" though implemented.** ADR-0012
"Status: proposed" — yet D-14 records the decision and `python -m chronos.histdata
options` is a shipped subcommand (docs/histdata_runbook.md:9-11). ADR-0014 and ADR-0015
both "proposed (design-review pending)" — yet `src/chronos/research/walkforward.py`,
`stats.py`, `campaign.py`, `purged_cv.py` exist and are the documented campaign tooling.
Status: **fix-candidate for an implementation-status annotation** on each status line;
flipping "proposed" to "accepted" is a governance act (the pending design reviews never
happened) → chronos-change-control. Until then: trust neither direction of a
proposed-ADR status line without checking the code.

## #13 — QQQ_GOLD_SPY_CAPABILITY_BRIEF.md: unbannered standing instructions

> "This brief is the standing instruction set for an Opus-class (or stronger) session
> executing that directive." (docs/QQQ_GOLD_SPY_CAPABILITY_BRIEF.md:6-7)

Every other historical plan got a 2026-08-01 subordination banner (AI_QUANT_GAME_PLAN,
LIVE_WHEEL_GAME_PLAN, OPUS_BUILD_BRIEF — see inventory); this one did not. It
acknowledges the D-16 supersession but still presents itself as active instructions. A
session that opens it first could execute a 2026-07-19 directive against the 2026-08-01
plan. **Status: fix-candidate** (add the standard banner). Related: the
live_trading_runbook's "Autonomous operation … **not yet operable**" header +
"Nothing here is operable yet" (docs/live_trading_runbook.md:19,26) is stale in the
OPPOSITE direction of its own mandate prose (Ledger #2) — the single section is wrong
both ways at once.

## Minor defects (cite-with-care, not conflicts)

| Site | Defect |
|---|---|
| docs/RESEARCH_REPRODUCIBILITY.md:8 | Dead link to `RESEARCH_READINESS.md` — file does not exist (hedged "(if present)") |
| docs/LIVE_WHEEL_GAME_PLAN.md:33 | "Branch: `feat/live-wheel-dashboard`" — default branch is `feat/wheel-dashboard-mvp` (VISION plan §2); banner-protected |
| DECISIONS.md | Rows D-17 and D-18 appear after D-19 in the file — ordering quirk, not staleness |
| docs/ARCHITECTURE.md vs docs/architecture.md | Case-only filename collision — hazardous on case-insensitive checkouts; always specify which you mean |

---

# House doc style — how Chronos fixes a stale doc

Observed conventions, with verbatim exemplars. Follow these when updating any doc.

**1. In-place supersession, never deletion.** The wrong claim stays visible, struck or
annotated, with a date. Three exemplars:

- DECISIONS.md:18 (D-11): "~~No generative model output feeds any runtime decision…~~
  **SUPERSEDED by D-16 (2026-07-25).** Kept for history; read it as the posture through
  that date, not as current policy."
- ADR-0016 status line: "accepted (owner directive, 2026-07-25); §4 and §6 superseded
  in part by ADR-0017" — the supersession is IN the status line.
- ADR-0010 §4 (the model scoped correction, :118-124): "**Correction (2026-07-27,
  M10).** Neither half of that sentence was true when it was written, and the paragraph
  stayed wrong for five milestones. … The claim is left standing above rather than
  edited away, because an ADR that quietly rewrites its own history is worth less than
  one that shows [its corrections]."

**2. Banner conventions for historical docs.** A blockquote banner at the top, dated,
naming what supersedes it and how to read the body. Exemplar (TASKS.md:3-6): "**Legacy
board.** This file primarily tracks the deterministic strategy-platform build and is not
repository-wide current truth. All new work must be sequenced and judged against
docs/VISION_COMPLETION_PLAN.md; verify every open/done claim against the live commit
before relying on it." See also architecture.md:3-11 ("Scope note"), OPUS_BUILD_BRIEF
("**ARCHIVED (2026-08-01)**"), GO_LIVE_CHECKLIST's paired banners.

**3. Disclosed-residual style (RISK_REGISTER).** A mitigated risk is never silently
closed: status `MITIGATED (M<n>, date)` + a "**Residual (disclosed):** …" clause naming
exactly what remains unproven (e.g. R-26: "the parse is verified against fixtures, not a
live gateway"). MITIGATED ≠ CLOSED — "All four kernel defects are now mitigated and
**none is closed**" (README.md:102-103, CHANGELOG.md:65-67).

**4. Enforcement labels.** README safety bullets are tagged **[enforced]** (a test
proves it) or **[contract]** (structural guarantee of the types) — README.md:122+. Never
add an unlabeled safety bullet.

**5. Datelines on volatile claims.** "current as of 2026-07-17"
(GO_LIVE_CHECKLIST.md:26-27), "Summary (current — M2a, 2026-07-25)", "(Amended
2026-07-25)" (ASSUMPTIONS A-12). A dated stale claim decays gracefully; an undated one
becomes Ledger material.

**6. The Phase-0 deliverable rule (where this converges).** The plan's Phase 0 requires:
"One generated current-state page. Historical documents retain history but cannot
present old milestone state as current truth." (VISION_COMPLETION_PLAN.md §5, Phase 0).
Until that page exists, README is the capability-status stand-in, TEST_RESULTS carries dated gate
evidence, and CHANGELOG remains the milestone history.

# How to update docs when state changes

1. **Record the evidence first** — the test run, commit, or artifact that made the old
   claim false. No evidence, no edit (AGENTS.md task contract; chronos-change-control).
2. **Update the CANONICAL home of the fact** (one home per fact): status narrative →
   README "Where that stands today"; dated gate evidence → TEST_RESULTS after fresh
   `make gates` and exact-SHA CI; milestone history → CHANGELOG; risk posture → the
   RISK_REGISTER row; decision → DECISIONS.md + a new/amended ADR; roadmap →
   VISION_COMPLETION_PLAN (see rule 5).
3. **Add supersession notes at the stale sites** in house style (§ above): strike or
   annotate in place, date it, point to the canonical home. Do not delete history.
4. **Never fork a second current-truth home.** If two docs would both claim to be
   current for the same fact, one of them gets a banner deferring to the other.
5. **VISION_COMPLETION_PLAN edits are special:** "Update this document only when live
   state, scope, sequencing, or a gate actually changes; record the evidence and commit
   that caused the change." (VISION_COMPLETION_PLAN.md §13). Scope/threshold/gate
   changes need explicit owner approval — agents may only propose (§13).
6. **Security-sensitive, money-critical, or safety-mechanism doc changes** (SECURITY.md,
   safety.md, runbooks' halt/kill procedures) require owner review like the code they
   describe (AGENTS.md build rules; chronos-change-control).
7. When you FIND a new contradiction: stop and surface it (AGENTS.md:54) — add it to
   your task report and, if durable, propose a ledger entry here. Never average the two
   sides or pick one silently.

# Provenance and maintenance

Written 2026-08-02 against HEAD `47a8d72`; validation-document facts re-verified
2026-08-28 against exact main `d44fc4ac7d2f`. Other entries retain their own dates. Docs move:
treat every `file:line` here as a pointer, not an address — grep the quoted text if lines shifted.

| Volatile fact | Re-verify with |
|---|---|
| HEAD / branch | `git log -1 --format='%h %s' && git branch --show-current` |
| Latest dated gate artifact; live test count | `sed -n '1,35p' docs/TEST_RESULTS.md`; `.venv/bin/python -m pytest -q` |
| Ledger #1 still open (no "kill" in incident runbook) | `grep -ci kill docs/INCIDENT_RESPONSE.md` (0 = still open) |
| Ledger #1 backup-table omission | `grep -n live_kill_switch docs/BACKUP_AND_RECOVERY.md` (empty = still open) |
| Ledger #2 still open (no mandate in orders plane) | `grep -rn mandate src/chronos/orders/*.py` (empty = code still requires arm) and `grep -n "is_armed" src/chronos/orders/submission.py` |
| Ledger #3 still open | `grep -n "wired into nothing" docs/ARCHITECTURE.md` |
| Ledger #5 still open | `grep -n "service loop" TASKS.md` |
| Ledger #7 remains resolved | `grep -n "Summary (current — re-measured 2026-08-28)" docs/TEST_RESULTS.md`; `grep -n "20-minute timeout" docs/TEST_PLAN.md` |
| Ledger #8 still open ($3k sites) | `grep -rn "USD 3,000\|USD 3k\|3,000" *.md docs/*.md docs/adr/*.md \| grep -v OPUS_BUILD` |
| Ledger #9/#10 still open | `grep -n "ONLY code path" docs/IBKR_INTEGRATION.md; grep -n "does not exist" docs/DEPLOYMENT.md` |
| Ledger #11 still open | `grep -n "settings validation raise\|remote access, authentication" docs/SECURITY.md` |
| Ledger #12b still open | `grep -m1 "^Status" docs/adr/ADR-0012*.md docs/adr/ADR-0014*.md docs/adr/ADR-0015*.md` |
| Ledger #13 still open | `grep -n "standing instruction set" docs/QQQ_GOLD_SPY_CAPABILITY_BRIEF.md` |
| Doc/ADR inventory complete | `ls *.md docs/*.md docs/adr/*.md \| wc -l` (63 as of 2026-08-02) |

When a ledger entry is fixed in the repo, update this skill in the same change: mark the
entry RESOLVED with the fixing commit, keep the entry (house style: history stays
visible), and update references/doc-inventory.md's status column.
