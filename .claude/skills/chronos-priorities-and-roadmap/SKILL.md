---
name: chronos-priorities-and-roadmap
description: >
  Open this FIRST at the start of any Chronos session. Load it whenever you ask
  "what should I work on", "what's next", "what are the priorities", "what's the
  current status", "where is this project", "what's the roadmap", "what's the game
  plan", "is X done yet", "what matters right now", or you are about to pick a task,
  scope a milestone, or judge whether a proposed feature advances the project. It is
  the distilled execution plan grounded in docs/VISION_COMPLETION_PLAN.md (canonical,
  2026-08-01) and AGENTS.md precedence: the two independent 10/10 outcomes, a dated
  current-truth snapshot with re-verification commands, the ordered near-term work
  queue, the owner-decision queue, anti-goals, and the honest long-horizon calendar.
  NOT for how-to-run questions (chronos-run-and-operate), config (chronos-config-and-flags),
  doc trust (chronos-docs-map), or change classification (chronos-change-control).
---

# Chronos — priorities and roadmap

**Audience:** a session with zero prior context deciding what to do next.
**Authority:** this skill summarizes; it never overrides. The canonical roadmap is
`docs/VISION_COMPLETION_PLAN.md` (Status: canonical, Effective 2026-08-01) under the
`AGENTS.md` document-precedence rule (AGENTS.md:42-54): owner direction > current
executable facts & unresolved defects > ADRs+DECISIONS.md >
safety/limitations/RISK_REGISTER > VISION_COMPLETION_PLAN.md > historical docs.
Stop and surface contradictions; never average them.

**Rule zero (AGENTS.md:35-36):** every dated fact below is a point-in-time claim.
Re-verify against the live repo before building on it — the one-line commands are
provided. Historical game plans, task boards, and handoffs (TASKS.md "Open" list,
HANDOFF.md, OPUS_BUILD_BRIEF, AI_QUANT/LIVE_WHEEL game plans, QQQ_GOLD_SPY brief)
are NOT the work queue. Which docs to trust: see `chronos-docs-map`.

---

## 1. The two independent 10/10 outcomes — never blur them

The project has TWO definitions of done (VISION_COMPLETION_PLAN.md §1). They are
independently valuable and move on completely different clocks:

| Outcome | What it means | Honest clock |
|---|---|---|
| **Platform / safety 10/10** | One coherent, installable, observable, recoverable system whose declared capabilities exactly match executable behavior: one execution authority, broker-truth accounting, mechanical enforcement of every authority/risk field, real-gateway conformance evidence, tested recovery, no unresolved Critical/High defect. **A 10/10 platform may correctly remain `NO_TRADE`.** | The assistant/infrastructure half (dashboard, terminal, reconciliation, monitoring, doc coherence) can mature in **weeks** of focused work. |
| **Proven autonomous trader 10/10** | Per asset family, one exact strategy-policy config independently clears research → replay → shadow → supervised paper → autonomous paper → live canary → capped live, with prospective, post-cost, reconciled, version-bound proof inside owner-frozen loss limits. | Realistically **24–36+ months** for ONE family from the 2026-08-01 baseline (plan §12). Options possibly years. |

Binding rules (AGENTS.md:21-24): code completion is not operating or economic proof;
a correct `NO_TRADE` is success when evidence is insufficient; never weaken a gate to
manufacture progress. **Do not manufacture urgency on the slow half** — a session that
"speeds up" the trader outcome by softening evidence requirements is making the
project worse, not faster. The two scores are also asymmetric in what a session can
do: an AI session can genuinely advance the platform half; the trader half advances
mostly on owner actions and calendar time.

---

## 2. Current-truth snapshot — dated 2026-08-02, REVERIFY before relying on it

Each row: the fact as verified on 2026-08-02, then the read-only command that
re-verifies it today. If a re-check disagrees with this table, the repo wins —
update your understanding, not the gate.

| # | Fact (2026-08-02) | Re-verify with |
|---|---|---|
| 1 | **No real IBKR gateway (paper or live) has EVER been connected** in this project's history. Every adapter-path control is fixture-verified only. MITIGATED ≠ CLOSED. | `grep -n "never been exercised" docs/limitations.md` and `ls research/data/history/` (only HOLDOUTS.json + README.md = capture store still empty) |
| 2 | **Zero strategies selected for promotion.** Best cell: `regime_trend_v1`/QQQ, **18 closed trades** on the validation window vs the frozen ≥20 floor (C4). Frozen before observation; not editable after. | `grep -n "Selected candidates" docs/STRATEGY_SELECTION.md` (→ NONE); `grep -n "maximum is 18" docs/RESEARCH_REPORT.md` |
| 3 | **The wheel has ZERO backtest evidence and no options simulator exists.** Expired-option history is unobtainable at any spend (ADR-0012); validation is forward-capture-bound. | `grep -rn "option" src/chronos/backtest src/chronos/strategies \| wc -l` (→ 0) |
| 4 | **Account snapshot ≈ USD 110** vs the ~USD 3,000 premise still carried by ~25 doc/config sites. This is a LIVE, UNRESOLVED owner decision — never quietly assume either number. | `sed -n '68,70p' docs/VISION_COMPLETION_PLAN.md` |
| 5 | Suite green with exactly one skip (the opt-in IBKR smoke test); ruff/format/mypy-strict clean. ~~Expect ~2489 passed as of 2026-08-02~~ ~~expect ~2745 passed~~ **Corrected 2026-08-09: expect ~2767 passed** — 2543 at `721d7f1`, +202 from the Five-Tool slice and its merge-review tests, +22 from the canonical ADR-0013 registry integration. Authoritative baseline and suite map: `chronos-validation-and-qa` §2. | `.venv/bin/pytest -q` (~120 s) |
| 6 | **Option-chain selection work lives on `codex/chronos-option-chain-selection-v1` @ ae9d256 and is NOT assumed integrated** into the default branch. | `sed -n '65,67p' docs/VISION_COMPLETION_PLAN.md`; `git log --oneline --all \| grep -ci "option-chain"` (→ 0 locally) |
| 7 | **Default branch is still `feat/wheel-dashboard-mvp` — target your PRs there.** A remote `main` was created 2026-08-02 at `46b2ad0` (owner request; a new ref only, no history rewritten), but flipping the *default* is a repository setting only the owner can change and it has not happened. Open owner items: whether the old branch is retired, and re-adding branch protection, which does not follow the default. CI is unaffected (no branch filter in `ci.yml`). | `git ls-remote --symref origin HEAD` |
| 8 | Experiment-registry ledger **ships empty** (0 records, 0 trials); the burned QQQ 2022-2024 holdout is documented ONLY in docs/results, not the ledger — never infer holdout cleanliness from the empty ledger. | `ls research/registry/` (→ does not exist); `grep -n "must not be treated as clean" docs/VISION_COMPLETION_PLAN.md` |

Deeper detail lives elsewhere: statistical gates → `chronos-research-methodology`;
suite map → `chronos-validation-and-qa`; doc contradictions → `chronos-docs-map`;
drift-measurement scripts → `chronos-diagnostics`.

---

## 3. The ordered near-term queue

Work items in execution order. Anything not on this list should justify itself
against the dependency chain in plan §3 (scope constitution → authority correctness
→ broker truth → certified data → strategy evidence → ladder).

### 3a. FIRST: fix the self-disclosed doc/code contradictions and Phase-1 authority-coherence items — before any new feature work

`docs/VISION_COMPLETION_PLAN.md` §6 lists 8 findings observed 2026-08-01. All 8 were
re-verified **still open on 2026-08-02** (file:line below). Reverify each against the
live commit before editing, and check no branch already addresses it (plan §6 preamble).

| # | Finding (plan §6) | Verified current state, 2026-08-02 | Re-verify with |
|---|---|---|---|
| 1 | Reconciliation readiness is **consumed by one opening submission**; no supervised callback consumer or bounded periodic convergence loop exists. | `src/chronos/orders/reconciliation_readiness.py:145-147` resets status to PENDING on claim; no periodic loop in the backend lifespan. OPEN. | `grep -n "consumed by an opening-order submission" src/chronos/orders/reconciliation_readiness.py` |
| 2 | **Incident runbook invokes the wrong halt**: `chronos.cli halt` stops only the deterministic platform; the live order plane's separate kill switch (`POST /live/kill`) is never mentioned. | `docs/INCIDENT_RESPONSE.md:19-23` still leads with `python -m chronos.cli halt`; zero occurrences of the live kill switch in the file. OPEN. | `grep -c "kill" docs/INCIDENT_RESPONSE.md` (→ 0) |
| 3 | **Restore guidance overstates safety**: a missing live kill-switch file boots DISENGAGED, yet BACKUP_AND_RECOVERY claims "restore must never auto-resume trading, and the code guarantees it" and omits `data/live_kill_switch.json` from its backup list. Recovery must boot kill-engaged, read-only, unreconciled. | `src/chronos/orders/kill_switch.py:83-85` (FileNotFoundError → `engaged=False`); `docs/BACKUP_AND_RECOVERY.md:3-6`. OPEN. | `sed -n '83,85p' src/chronos/orders/kill_switch.py` |
| 4 | **Mandate-vs-arming contradiction**: standing-authority prose says the mandate replaces session arming; the code unconditionally requires a current arm for every LIVE submit. | `src/chronos/orders/submission.py:441` reads the arm; `chronos.orders` has zero mandate awareness. OPEN — **choosing which authority model wins is an OWNER decision** (money/authority change), not a session's. | `sed -n '441p' src/chronos/orders/submission.py`; `grep -rn "mandate" src/chronos/orders/ \| wc -l` (→ 0) |
| 5 | **Supervisor records COMPLETE on refusal**: any non-exception handoff return — including `SubmissionOutcome(submitted=False)` refusals, ambiguous sends, venue rejections — journals as `CycleStage.COMPLETE`. | `src/chronos/supervisor/loop.py:405-453`: only exceptions map to ORDER_PLANE_REFUSED. OPEN. Fix needs typed outcomes (plan §6 design outcomes) WITHOUT letting the supervisor import order-plane types. | `sed -n '405,425p' src/chronos/supervisor/loop.py` |
| 6 | **External-worker provenance is static and its credential is not proposal-only**: every proposal is stamped with the constant `INGRESS_IDENTITY`, and the ingress accepts the same local token every mutating route accepts. | `src/chronos/api/autonomy_wiring.py:84-94` (all-constant identity). OPEN. | `sed -n '84,94p' src/chronos/api/autonomy_wiring.py` |
| 7 | **Dead economic fields on the decision contract**: `exit_plan`, `protective_order_required`, `max_acceptable_loss_usd`, `requested_risk_budget_usd` affect nothing but the dedup fingerprint. Violates AGENTS.md:29-30 ("inert authority, risk, exit, or protection fields are release blockers"). | Only readers in src/ are `autonomy/decision.py` (definition) and `supervisor/queue.py` (fingerprint). OPEN. | `grep -rln "exit_plan\|max_acceptable_loss_usd" src/chronos --include='*.py'` (→ those two files only) |
| 8 | **Promotion is not evidence-bound**: `FamilyPromotion` rungs are self-declared in the owner's mandate JSON; no signed/expiring evidence artifact, no grant/demote code, nothing binds a rung to the evidence that earned it. | Only carriers: `autonomy/mandate.py` (+ consistency checks in admission, display in terminal views). OPEN. | `grep -rln "FamilyPromotion" src/chronos --include='*.py'` |

How to work this list: items 1 and 2, plus the DOC halves of all eight findings, are
work an AI session CAN do under the normal task contract (plan §13). The CODE halves of
item 3 (making recovery boot kill-engaged — a pinned-by-test safety-mechanism
modification) and item 5 (typed handoff outcomes for the supervisor) require explicit
owner review per `chronos-change-control` §1 (the "Safety-mechanism MODIFICATION" row).
Items 4, 6, 7, 8 involve authority-model choices (which arming model wins; what a worker
credential may do; whether a dead field becomes enforced or forbidden; what a promotion
artifact attests) — **those are owner gates**. Classify and route every one of them
through `chronos-change-control` before touching code. Fixing prose toward the weaker,
true statement is always safe; fixing code toward the stronger prose is an authority
change.

### 3b. THEN: the real-gateway read-only evidence gate — THE campaign

The single highest-leverage step in the whole repository (plan §7): the owner
installs the official IB API, supplies a paper account, keeps every transmit/live
flag false, and runs **≥5 read-only sessions (including a gateway restart)** capturing
sanitized evidence for account scope, server time, positions, executions, orders,
contract qualification, option chains, market rules/min ticks, trading sessions,
pacing, callbacks, and subscription cancellation. EXIT: no mutation call, no leaked
subscription, and captured fixtures replay offline exactly.

Why it dominates: fact #1 above — every gateway-facing behavior in the codebase is
fixture-verified conjecture, and every kernel-defect fix (R-24..R-27) carries the
residual "verified against fixtures, not a live gateway". Until this gate closes,
nothing can leave MITIGATED, no promotion rung is reachable, and the histdata/option
stores stay empty.

**Execute it via `chronos-real-gateway-campaign`** — the numbered, branch-gated,
measurable campaign plan. Do not improvise a connection procedure from this skill.

Calendar consequence (plan §8 first line + ADR-0012): **forward option capture must
begin as soon as a gateway exists** — IBKR sells no expired-option history at any
price, so every uncaptured day is unrecoverable forever. This is the one place where
real calendar urgency exists, and it is gated on the owner's gateway step, not on code.

### 3c. IN PARALLEL: the owner-decision queue (ready to ask)

Decisions only Kevin can make (plan §11: "No test result, backtest, backup, or agent
recommendation substitutes for an owner gate"). Present these when the owner is
available; do not work around them:

1. **Capital envelope** — the ≈USD 110 reality vs the ~$3k premise (fact #4). Fund,
   descope, or freeze; every sizing doc and R-10 depend on the answer.
2. **Phase 0 economics** — benchmark, minimum useful edge, loss/drawdown/CVaR and
   concentration limits, data budget (plan §5). Must be frozen BEFORE observing the
   evidence they will judge.
3. **Phase 0 scope freeze** — explicit include/exclude for short equity, option
   structures, futures roots, index options, crypto (plan §5).
4. **Which arming model wins** — mandate-replaces-arming vs arm-required (finding 4).
5. **Market-data subscriptions** and option-reference-data licensing (plan §11) —
   prerequisite for the gateway campaign to produce quote/chain evidence.
6. **The real-gateway session itself** — credentials, ibapi install, paper account
   (§3b; owner-only by definition).
7. **TradingView parity exports** — `fixtures/tradingview/` holds only a README, no
   reference exports; parity remains spec-level until the owner exports references
   (TASKS.md:51-52, A-03).
8. **Option-chain branch** — integrate, rework, or drop
   `codex/chronos-option-chain-selection-v1` (fact #6). Until decided, do not build
   on the assumption it lands.

### 3d. ALSO USEFUL NOW: assistant-half maturation (valuable regardless of autonomy)

Genuinely useful platform work that needs no gateway and no owner economics — good
tasks when the queue above is blocked:

- ~~**Terminal client gaps**: there is no emergency-stop button in the UI today.~~
  **DONE 2026-08-02 (R-43).** The system panel now carries ENGAGE KILL SWITCH and
  DISARM LIVE SESSION, both typed-confirmed and both working on a demoted backend.
  **Still open, and deliberately so:** arming and kill-disengage have no buttons —
  they *grant* authority, and whether a browser session should hold that is an owner
  posture decision (ADR-0018 §4 permits it). Also still open: mandate authoring in
  the terminal, and streaming instead of the 5 s poll.
- **Mandate authoring aid**: the owner hand-writes raw mandate JSON validated only at
  boot; a validate/preview tool (read-only, no authority change) would prevent
  boots-inert surprises.
- **Streaming/pushed panel updates**: the terminal polls every 5 s (`POLL_MS = 5000`).
- **Bounded periodic reconciliation loop** (finding 1's build-side; plan §7
  deliverable: startup, reconnect, order/fill-triggered, and bounded periodic
  reconciliation with a maximum evidence age).
- **Off-host alerting** — alert delivery is local-only BY STRUCTURAL TEST
  (`src/chronos/supervisor/delivery.py:9-29`); a network channel is a deliberate
  authority change that **requires a NEW ADR and owner approval**. Design it as an
  out-of-process sidecar; never add a network import to the trading process.
- **Backup/incident doc corrections** — the doc halves of findings 2-3 (add
  `data/live_kill_switch.json` and friends to the backup list; give the incident
  runbook a live-plane section). Weaker-true-statement doc fixes are safe now.

---

## 4. Anti-goals — work that does NOT advance either score

From plan §3, §10, §12 and AGENTS.md. Decline or re-scope tasks shaped like these:

- **Feature breadth off the dependency chain** — "Feature breadth that does not
  advance this dependency chain does not advance either 10/10 score" (§3). The
  critical path is authority coherence, provenance, reconciliation, trusted data, an
  eligible strategy, and calendar-time evidence — "not more UI or unsupported asset
  vocabulary" (§12).
- **New asset vocabulary** — adding futures/index-option/etc. types, enums, or stubs
  ahead of their Phase 5 lane. "A broad vocabulary with unimplemented or unproven
  families cannot" be 10/10 (§10).
- **Promotion transfer across families** — "Success in one family never promotes
  another" (§10); a stock promotion authorizes neither options nor futures.
- **Threshold edits after observation** — frozen statistical/operational/financial
  gates are not editable after seeing evidence; a failed holdout rejects the
  candidate (AGENTS.md:27-28; plan §13). The 18-vs-20 near-miss is exactly the case
  the floor exists for.
- **Weakening any gate to "unblock" progress** — fail-closed and deny-by-default are
  the permanent posture; an untested control is never "proven"; `NO_TRADE` is success.

---

## 5. Long horizon — honest calendar (do not compress this)

Clearly labeled estimates, not promises (plan §9, §12). No task in this section is
urgent this week, and pretending otherwise corrupts the evidence discipline.

**The autonomy ladder** (per family; thresholds to be frozen in Phase 0 before
observation — plan §9): Replay (byte-identical decisions over the full corpus) →
Shadow (max(90 days, power-required opportunities); ≥99.5% availability; zero
illegal intents) → Supervised paper (max(90 days, 50 round trips, power-N); ≥100
order lifecycles across 20 sessions) → Autonomous paper (≥60 trading days, 100
lifecycles, ≥99.9% scheduler availability) → Live canary (max(6 months, 50 round
trips) at minimum meaningful size) → Capped live (max(12 months, 100 independent
trades, power-N) across ≥2 regimes). Every promotion artifact binds exact versions
of everything; any material change demotes. Paper proves machinery, not alpha.

**Calendar math** (§12): engineering-complete first-family platform ≈ 6-9 months;
shadow/paper proof another 2-4 months; canary + capped-live require ≥18 months of
prospective evidence AFTER a strategy is frozen → **one-family proven autonomy is a
24-36+ month objective from 2026-08-01**; multi-family is longer. Without licensed
expired-options history, option validation is calendar-bound and **possibly years** —
the forward-capture clock (§3b) is the only way to shorten it, and only from now on.

**What "beyond state of the art" means here** (owner's own framing): not a demo, not
a chatbot with a brokerage key — a genuinely **evidence-bound autonomous trader**,
where a model originates decisions inside owner-set boundaries and every rung of
authority was earned by prospective, reconciled, version-bound proof. The reference
"Quant Guild" bot is the inspiration for the autonomy ergonomics (ADR-0017), NOT for
the evidence bar; Chronos deliberately refuses the reference project's unbounded
market orders and credential-holding patterns. If the evidence never materializes,
the honest end state is a 10/10 platform that correctly stays `NO_TRADE` — that
outcome is a success, and this skill's job is to keep future sessions from
"rescuing" the project out of it.

---

## 6. When NOT to use this skill

| You actually need | Use instead |
|---|---|
| How to run/launch anything; kill/halt/arm/revoke procedures; backup/restore reality | `chronos-run-and-operate` |
| Env vars, config files, flags, safety classes | `chronos-config-and-flags` |
| Which document to trust; the stale/contradiction ledger | `chronos-docs-map` |
| Is this change allowed? Owner gates, ADR discipline, task contract, claim rules | `chronos-change-control` |
| Load-bearing invariants and known-weak points | `chronos-architecture-contract` |
| AITradeDecision / gateway / mandate / model_discretion details | `chronos-autonomy-and-mandates` |
| Wheel state machine, options gating, assignment | `chronos-wheel-and-options` |
| IBKR adapters, Contract-vs-ContractDetails, inert-control prevention | `chronos-ibkr-boundary` |
| Walk-forward / DSR / bootstrap / holdout mechanics | `chronos-research-methodology` |
| What counts as evidence; test-suite map; proof patterns | `chronos-validation-and-qa` |
| Environment from scratch; lockfile; container traps | `chronos-build-and-env` |
| Symptom → triage | `chronos-debugging-playbook` |
| Why past decisions went the way they did; the four kernel defects' story | `chronos-failure-archaeology` |
| Executing the first real-gateway session | `chronos-real-gateway-campaign` |
| Read-only state-inventory / drift scripts | `chronos-diagnostics` |

---

## 7. Provenance and maintenance

Compiled 2026-08-02 against HEAD `47a8d72` ("docs: make vision completion plan
canonical", 2026-08-01). Grounding documents: `docs/VISION_COMPLETION_PLAN.md`
(canonical plan), `AGENTS.md` (contract + precedence), `DECISIONS.md` D-16/D-17,
`RISK_REGISTER.md`, `docs/STRATEGY_SELECTION.md`, `docs/RESEARCH_REPORT.md`,
`CHANGELOG.md`. Volatile facts and their one-line re-verification commands:

| Volatile fact (as of 2026-08-02) | Re-verify |
|---|---|
| Compiled against `47a8d72` (last non-skill commit, tip of `feat/wheel-dashboard-mvp`); the skill-library branch runs ahead of it by its own checkpoint commits; no `main` | `git log -1 --oneline && git branch -a -v` |
| Plan still canonical / effective 2026-08-01 | `sed -n '1,6p' docs/VISION_COMPLETION_PLAN.md` |
| All 8 Phase-1 findings still open | run the per-row commands in §3a |
| Suite green, 1 skip (~~~2489 passed as of 2026-08-02~~ ~~~2745~~ **corrected 2026-08-09: ~2767**, post Five-Tool merge and the ADR-0013 registry integration; baseline home: `chronos-validation-and-qa` §2) | `.venv/bin/pytest -q` |
| Zero strategies selected; best cell 18 trades | `grep -n "Selected candidates" docs/STRATEGY_SELECTION.md` |
| No gateway evidence yet; capture store empty | `ls research/data/history/` |
| Account ≈ USD 110 decision still unresolved | `sed -n '68,70p' docs/VISION_COMPLETION_PLAN.md` |
| Option-chain branch still unintegrated | `git log --oneline --all \| grep -ci "option-chain"` |
| Registry ledger still empty | `ls research/registry/ 2>&1` |

Maintenance rule: when any row above changes (a finding closes, a gateway session
lands, a strategy is selected, the capital decision resolves), update §2/§3 of this
skill in the same change set, citing the commit or artifact that changed the fact —
the same discipline `VISION_COMPLETION_PLAN.md` §13 imposes on itself. All
re-verification commands here are read-only; none connects to a broker, and nothing
in this skill authorizes weakening a gate, widening authority, or treating an
untested control as proven.
