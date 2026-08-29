# Chronos Vision Completion Plan

**Status:** canonical execution plan

**Effective:** 2026-08-01

**Applies to:** every Chronos agent, contributor, review, and promotion decision

**Owner:** Kevin; owner approval remains mandatory wherever this plan says `OWNER GATE`

> **Amendment, 2026-08-09 (ADR-0025 / D-21, owner directive):** the §9 promotion
> ladder and the §12 calendar remain the frozen bar for **claims** — "validated,"
> "proven," "trades better than the owner" — and are unchanged as such. They no
> longer sequence the owner's right to run **live experimentation at
> owner-capped size**, which is instead gated by ADR-0025's mechanical-readiness
> checklist (funded capital + typed loss limits, the §7 read-only gateway
> campaign, a frozen paper-lifecycle floor, TradingView parity, market-data
> subscription, an owner-authored mandate and kill drill). Live records feed the
> same registry/replay evidence machinery, so the ladder runs concurrently with
> the experiment rather than in front of it. Finding 4 (§6) resolves toward
> standing mandate authority; implementation is owner-reviewed follow-up work.
> Nothing in this amendment weakens a gate, edits a threshold, or lets live P&L
> at small N count as evidence.

This document turns “10/10” into build order and measurable release gates. It is not an
ADR and does not silently change accepted architecture. `DECISIONS.md` and accepted ADRs
govern implemented authority; this plan governs what gets built next and what evidence is
required before Chronos may claim completion.

If a live repository fact conflicts with this plan, record the discrepancy and correct the
plan or implementation through review. Do not blend contradictory states.

## 1. The two independent definitions of done

### Platform and safety: 10/10

Chronos is one coherent, installable, observable, recoverable trading system whose
declared capabilities exactly match executable behavior. It has one execution authority,
complete broker-truth accounting, mechanical enforcement of every authority/risk field,
real-gateway conformance evidence, secure model isolation, tested recovery, and no
unresolved Critical/High control defect.

A 10/10 platform may correctly remain `NO_TRADE`. That is a valid safety result.

### Proven autonomous trader: 10/10

Every asset family promised by the declared product scope has at least one exact
strategy-policy configuration that independently clears:

`research -> replay -> shadow -> supervised paper -> autonomous paper -> live canary -> capped live`

The proof must be prospective, net of all costs, attributable, reconciled to broker truth,
inside owner-frozen loss limits, and bound to exact code, model, prompt, tools, policy,
compiler, resolver, data, and configuration versions.

“Uncapped autonomy,” guaranteed profitability, and zero residual market risk are not valid
completion targets.

Until the owner explicitly changes the mission declared in `README.md`, repository-wide
10/10 therefore requires independently proven equities/ETFs, exchange-traded futures,
listed equity options, and listed index options. Completing the recommended equities/ETF
wedge earns a 10/10 family lane; it does not by itself complete the whole stated vision.

## 2. Current truth snapshot

This snapshot is context, not remembered state. Reverify it before building on it.

- GitHub default branch observed on 2026-08-01: `feat/wheel-dashboard-mvp` at `06fcee6`.
  ~~No remote `main` branch existed.~~ **Updated 2026-08-02 (owner request):** a remote
  `main` now exists, created from the tip of `feat/wheel-dashboard-mvp` at `46b2ad0` — a
  new ref only; no history was renamed or rewritten. ~~**The GitHub default branch is still
  `feat/wheel-dashboard-mvp`**: changing it is a repository setting, not a git operation,
  and only the owner can make it. Until that flip happens, PRs continue to target
  `feat/wheel-dashboard-mvp`. Two follow-ups are owner decisions and are deliberately
  unresolved here: whether `feat/wheel-dashboard-mvp` is retired or kept alongside `main`,
  and re-adding any branch protection, which does **not** follow the default branch.~~ CI is
  unaffected either way — `.github/workflows/ci.yml` triggers on bare `push:`/
  `pull_request:` with no branch filter. Re-verify with
  `git ls-remote --symref origin HEAD`. **Updated 2026-08-22 (owner direction, D-33): the
  flip happened.** `main` IS the GitHub default branch; `feat/wheel-dashboard-mvp` is
  deleted. Both 2026-08-02 follow-ups are resolved: the old branch is retired, and
  protection returned as the `main-integrity` ruleset (PR-only, required green `quality`
  check, no force-push, no deletion, no bypass actors). PRs target `main`. Derive the
  default by command — `git ls-remote --symref origin HEAD` — never from a document,
  including this one; operating rules in `docs/AGENT_PROTOCOL.md`.
- The fail-closed autonomy and order kernel is substantial, but no real gateway, paper
  order, or live order has supplied operational evidence.
- Zero strategies are selected for promotion; insufficient evidence remains a correct
  result (`docs/STRATEGY_SELECTION.md`).
- Real historical and option stores are not populated as a trusted research corpus; one
  QQQ holdout was consumed and must not be treated as clean.
- Futures and index-option execution are absent. Options support is narrow and must remain
  family-gated.
- ~~The option-chain evidence-boundary work was observed separately on
  `codex/chronos-option-chain-selection-v1` at `ae9d256`; do not assume it is present on the
  default branch until it is actually integrated.~~ **Updated 2026-08-23: it is integrated.**
  The lane merged to `main` as codex's own commits (`6e7429e`, `ae9d256`), renumbered to ADR-0030 / D-34
  (it had claimed ADR-0020 / D-20, which `main` allocated to bounded periodic reconciliation
  on 2026-08-02). Evaluation is still default-off behind
  `ENABLE_AUTONOMY_OPTION_SELECTION`, CANARY/LIVE still require an owner-authored resolver
  promotion artifact this repository cannot create, and both real IBKR adapters still return
  non-authoritative deliverable evidence — so real-gateway option selection remains
  `NO_TRADE`. Verify presence by command, not by this line:
  `git cat-file -e origin/main:src/chronos/supervisor/option_selection.py`.
- The last documented account snapshot remains approximately USD 110; it is an observation,
  not a funded allocation. Owner directive 2026-08-25 freezes the QQQ v1 research reference
  at USD 3,000 while current live allocation and live risk remain USD 0. Funding may be
  considered only after an untouched-holdout pass, at least 90 days of shadow evidence, and
  supervised-paper evidence, followed by fresh owner approval. Funding does not create
  strategy-selection, promotion, submission, or short-selling authority (ADR-0031).

## 3. Build strategy: one complete vertical first

The first end-to-end production wedge is **liquid U.S. equities/ETFs**. It is closest to
the current executable capability and can accumulate evidence without first building the
distinct lifecycle semantics of derivatives.

```text
Scope and risk constitution
           |
           v
Authority correctness -> broker truth -> operations and recovery
           |                                |
           +-------------+------------------+
                         v
Certified data -> eligible strategy -> deterministic replay
                         |
                         v
Shadow -> supervised paper -> autonomous paper
                         |
                         v
Minimum live canary -> capped-live evidence
                         |
                         v
Repeat the entire ladder for each additional asset family
```

Feature breadth that does not advance this dependency chain does not advance either 10/10
score.

## 4. Primary scorecard

These are hard gates. A red gate cannot be averaged away by strength elsewhere.

| Primary KPI | Definition | 10/10 gate |
|---|---|---|
| Safety integrity | Unintended broker mutations, duplicate economic orders, authority bypasses, unauthorized exposure, and severe safety incidents | Zero; any Critical breach fails and demotes the rung |
| Broker truth and operations | Positions, orders, executions, commissions, cash, ownership, alerts, and recovery reconciled within frozen SLOs | 100% critical facts accounted for; zero unresolved unknown submissions or unexplained exposure |
| Net-edge confidence | Preregistered post-cost expectancy and benchmark evidence after multiple-testing and concentration controls | Frozen statistical gates pass in holdout and prospective live evidence while loss/drawdown guardrails remain intact |

Required driver and guardrail metrics include evidence completeness/freshness, decision and
receipt coverage, reconciliation age, alert delivery/acknowledgement, scheduler availability,
implementation shortfall, commission-model error, tracking error, drawdown/CVaR, exposure,
turnover, concentration, abstention/veto rate, incident count, and version drift.

## 5. Phase 0 — Constitution and document truth

**Target:** 1–2 weeks.

**Purpose:** eliminate scope ambiguity before more implementation.

Deliver:

- A machine-readable capability matrix for asset family x decision kind x strategy shape x
  broker adapter x mode x evidence source x promotion status.
- A frozen v1 scope, with equities/ETFs first and explicit include/exclude decisions for
  short equity, equity-option structures, futures roots, index options, and crypto.
- Owner-approved benchmark, minimum useful edge, capital envelope, loss/drawdown/CVaR and
  concentration limits, data budget, incident availability, and legal/tax review needs.
- A content-addressed research constitution: universe, costs, trial family, power analysis,
  contamination map, holdouts, acceptance criteria, model stack, and change/reset rules.
- One generated current-state page. Historical documents retain history but cannot present
  old milestone state as current truth.

**EXIT:** no unresolved scope contradiction; no clean holdout has been opened; owner gates
and criteria are recorded before observation.

**Progress (2026-08-26):** ADR-0031 and its content-addressed constitution freeze the QQQ
execution target, validation panel, staged capital, benchmark, minimum useful edge, risk
limits, cadence, strategy sequence, and zero incremental data budget before any new trial
or data read. D-36 subsequently selects confirmed close versus SMA-200 as the direction
indicator, and D-37 selects an immediate two-state primary transition while reserving a 1%
neutral band and five-close confirmation as prospective robustness variants. Neither
selects a strategy or reads data. D-38 selects CVaR-capped volatility sizing under the
existing 100% gross/1x ceilings, and D-39 selects a 252-session empirical 95% historical
CVaR estimator. D-40 constructs long and short loss tails separately and keeps short
exposure at zero while short-cost evidence is uncertifiable. D-41 sets the sizing base to
the lower of marked strategy NAV and USD 3,000, preventing automatic risk expansion after
gains. D-42 recomputes risk daily, permits next-session reductions, and limits increases to
a weekly schedule. D-43 rounds permitted target magnitude down to whole shares and keeps
sub-one-share targets in cash. D-44 uses a point-in-time total-return series for SMA/CVaR and
raw prices for execution. D-45 includes the newly confirmed session in both the SMA-200 and
CVaR-252 windows. D-46/ADR-0032 then separates that simple SMA attribution control from
the integrated Five-Tool candidate. The control keeps signal-flip exits and CVaR-primary
sizing; the integrated cell preserves the pinned source's EMA-100/two-bar hysteretic
entries, layered Confluence exit stack, and 1% stop-distance sizing inside the stricter
CVaR/gross/owner ceilings. D-42's weekly increase schedule is superseded by new-entry-event
increases while flat, with no later top-up after the native same-event management legs;
daily/next-session reductions remain. Phase 0 has
not exited. D-47/ADR-0033 now freezes the exact control initialization/equality,
raw-price sizing reference, next-session gap/revalidation/order behavior, economic-trade
floor, and five-cell one-axis robustness grid in a content-addressed artifact whose public
compiler remains blocked before data. D-48/ADR-0034 freezes the integrated candidate's
QQQ-specific causal price-domain overlay: technical geometry
uses point-in-time total-return OHLC rebased to the current raw close, while broker truth
remains raw and an intervening corporate action invalidates entry. Its public compiler
authenticates the pinned source/contract/campaign but remains blocked before data.
D-49/ADR-0035 adds a default-off, proposal-only PAPER position-management state machine
for actual QQQ fills: exact candidate/policy pins, durable semantic replay, typed fill
resolution, stop/target/breakeven/runner logic, and risk-reducing proposals only. It is
deliberately absent from runtime imports and creates no authority. D-50/ADR-0036 adds the
still-default-off opening-admission seam: persisted D-48 entry risk is now an authorizing
order check, two stable canonical-broker reads must positively prove the execution and exact
account-level QQQ quantity, and schema v11 atomically enforces one opening order per managed
stream plus one broker permanent identity per account. Multi-fill VWAP/CVaR persistence uses
explicit conservative directional rounding, and buy fills above the protected limit refuse.
This code-mitigates ADR-0035 activation blockers 1 and 2 but does not operationally
close them without real PAPER evidence. Executable activation still requires authenticated
ongoing management observations and outcomes, a trusted management-event identity,
broker-held protection semantics, runtime scheduling, and real PAPER evidence. Certified
data/cost/borrow identities, a clean holdout, incident availability, and legal/tax review
remain open.
D-51/ADR-0037 now composes those exact repository identities into one content-addressed,
authentication-only readiness report. It separates owner actions from Chronos build work,
unavailable short evidence, and deferred PAPER activation, and forbids evidence transfer between
the six-symbol QQQ robustness release and the distinct seven-symbol base Five-Tool intake. This
is current-state legibility only: Phase 0 still has not exited, no market data or holdout was
opened, no trial was registered, and no evidence or promotion gate advanced.
D-52/ADR-0038 now freezes the QQQ SMA-200 primary cell's relative power arithmetic at 6,233
completed OOS daily active returns (24.7302289281 year-equivalents), using the owner-gated 4%
minimum annual alpha and 8% annualized long-run tracking-error ceiling. The independent
100-closed-position floor remains a separate conjunct. The absolute date and successor
campaign binding remain blocked until the owner approves the clean start and covered calendar;
this arithmetic is not observed evidence and advances no gate.

## 6. Phase 1 — Authority and lifecycle coherence

The following findings were observed during the 2026-08-01 review. Reverify each against
the live commit and coordinate with any branch already addressing it before editing.

1. Reconciliation readiness is consumed after one opening submission, while a complete
   supervised callback consumer and bounded periodic convergence loop were not found
   (`src/chronos/orders/reconciliation_readiness.py`).
2. The incident runbook invokes the deterministic-platform halt while the live order plane
   has a separate kill switch (`docs/INCIDENT_RESPONSE.md`,
   `src/chronos/orders/kill_switch.py`).
3. Restore guidance overstates safety: a missing live kill-switch file defaults disengaged.
   Recovery must always boot kill-engaged, read-only, and unreconciled.
4. Standing-authority prose says the mandate replaces arming, while submission still
   requires a current arm (`src/chronos/api/autonomy_wiring.py`,
   `src/chronos/orders/submission.py`). Choose and implement one reviewed authority model.
5. ~~The supervisor treats any non-exception handoff return as `COMPLETE`, although
   `SubmissionOutcome(submitted=False)` represents refusal, ambiguity, or rejection
   (`src/chronos/supervisor/loop.py`, `src/chronos/orders/submission.py`).~~
   **Addressed 2026-08-13 (A1; R-49):** the handoff result is classified into four
   supervisor-owned dispositions — SUBMITTED, REFUSED_NOT_SENT, SENT_AMBIGUOUS,
   REJECTED_AFTER_SEND (`chronos.supervisor.handoff`) — each journaling its own
   `CycleStage` and refusal code, translated at the app-plane seam
   (`classify_submission_outcome` in `api/autonomy_wiring.py`) so the supervisor
   still imports no order-plane type. The activity rule is stated once and
   enforced: an attempt is consumed exactly when the supervisor cannot prove
   nothing reached the wire — so a refusal before the wire no longer spends an
   `orders_submitted` attempt, and an ambiguous send both counts and raises a
   CRITICAL owner alert. Additive: no existing refusal weakens, and
   `ORDER_PLANE_REFUSED` still names an exception out of the callable. Proof:
   `tests/safety/test_typed_handoff_outcomes_exercised.py` (42), with six
   independent conjuncts each verified by reverting it alone. Still open from this
   finding: the post-submission half of the typed-outcome list below — partial
   fill, full fill, cancellation, late commission — which belong to the order
   plane's lifecycle tracker rather than the cycle's handoff; and R-49's
   residual (a), that an exception out of a non-wiring handoff callable is still
   recorded as not-sent.
6. ~~External-worker provenance is static and its credential is not proposal-only.~~
   **Addressed 2026-08-12 (ADR-0023 Option A, owner-directed; D-24/R-48):** with
   `AUTONOMY_PROPOSERS_FILE` configured, proposals require a per-proposer,
   proposal-only credential and provenance is stamped from the credential's
   registration at drain time; the same work fixed the route's dead account scoping
   (every real proposal POST had refused `BACKEND_UNSCOPED` since M7). ~~Still open
   from this finding: the job/evidence/response protocol — registrations carry no
   evidence-bundle binding, so the evidence half (job ID, evidence digest, expiry
   per job) remains future ADR work.~~
   **Evidence half addressed 2026-08-14 (A2; ADR-0028 Option C, owner-directed;
   D-25/R-50) — finding 6 is now closed on both halves.** The finding that made it
   worth building: admission check 9 compared `provenance.evidence_bundle_id`/
   `_digest` against `SupervisorState.expected_*`, and **both sides were two reads
   of the single `INGRESS_IDENTITY` constant** — written correctly, wired to a
   comparison that had never had two independent origins, so it could not refuse
   in any posture for any proposer. `AUTONOMY_EVIDENCE_BUNDLES` unset is today's
   behavior byte-for-byte (proven against a recorded journal row, not by
   inspection). Set, `POST /autonomy/evidence` issues a durable, hash-chained
   bundle to the presenting credential — `backend_served` (the backend digests the
   exact bytes it serves) or `alert_attested` (a proposer's claim about bytes
   Chronos never saw); the drain resolves the cited bundle against that record at
   its own clock, so unissued, foreign-proposer, and expired-between-enqueue-and-
   drain refuse at STAMP with provenance stamped from the record; and check 9
   gains the independent side it never had — the payload's own `EvidenceCitation`,
   authored by the proposer and never written by the backend. Proof:
   `tests/safety/test_evidence_bundles_exercised.py` (25), with 18/18 conjuncts
   verified by reverting each alone. Bounds that survive, disclosed in R-50:
   **equality catches accident, not malice**; **attested is not witnessed** — an
   attested bundle may back a proposal, never a promotion rung (ADR-0024's call);
   and a bundle binds which facts were *served*, never that they were true,
   because no real gateway has ever been connected. Nothing remains open from this
   finding itself — but the promotion artifact it makes *resolvable* is item 8
   below, still open.
7. Several economic-looking fields do not mechanically affect execution. Every field must
   be enforced, explicitly advisory, or forbidden; deterministic exits/protection require
   a durable position-management lifecycle.
8. Promotion is not mechanically bound to the strategy and evidence that earned the prior
   rung. Replace self-declared family levels with signed, expiring evidence artifacts.

Required design outcomes:

- Exactly one transmit-enabling site and no dormant second broker-capable authority path.
- One immutable per-decision evidence snapshot used by both the model and gateway.
- Complete account evidence, exact Chronos ownership resolution, and decision-specific
  contract/quote facts.
- Proposal-only model-worker credentials; no order, live, policy, mandate, or promotion
  authority.
- Typed outcomes for not-sent refusal, ambiguous send, venue rejection, accepted order,
  partial fill, full fill, cancellation, and late commission.

**EXIT:** full safety suite plus property, fuzz, mutation, and chaos coverage; fresh install,
migrations, restore, kill/rearm, clock/disk/gateway failure tests; independent review finds
no unresolved Critical/High issue; `OWNER GATE` for money/security authority changes.

## 7. Phase 2 — Canonical broker and operations plane

Deliver:

- Idempotent persistence of broker order ID, permanent ID, client ID, `orderRef`, execution
  IDs, fills, commissions, state transitions, positions, cash, and buying power.
- Exact allocation provenance for stock lots, option assignments/exercises, manual or
  foreign positions, working orders, and commissions.
- Startup, reconnect, order/fill-triggered, and bounded periodic reconciliation with a
  maximum evidence age.
- P&L attribution, drawdown, exposure, commissions, slippage, and tracking error by exact
  strategy policy and family.
- Atomic reservations, position netting, and conflict resolution across proposals.
- Separate liveness, service readiness, and trading-capability health.
- Off-host alert sidecar, encrypted backups, external audit-chain anchor, automatic clock
  health, watchdogs, dead-man monitoring, measured RPO/RTO, and isolated restore drills.
- Reproducible package/release validation: clean venv install, all migrations, static
  assets, entry points, dependency/secret/static scans, SBOM, and signed artifacts.

  **Partial delivery (updated 2026-08-29, ADR-0040/ADR-0041):** the local API and operator surfaces now
  share one pure, display-only projection separating liveness, operator-service readiness,
  and lane-specific new-exposure capability. Polling reads only bounded local/cached facts,
  retains typed startup/task failures, and cannot be imported by authority modules. An explicit,
  default-disabled chrony provider now calculates and caches a quantitative maximum-error bound;
  missing, malformed, local-reference, stale, or future evidence remains unknown. The development
  host lacks `chronyc`, no acceptable threshold or real capture has been supplied, and the
  observer is not an order-authority input. Dedicated no-store `/health/live` and `/health/ready`
  endpoints now map the existing process/service verdicts to HTTP 200/503 without exposing
  trading capability. Orchestrator deployment/configuration, external monitoring/alerts,
  watchdogs, dead-man behavior, SLOs, and operational proof remain open. This does not satisfy
  the Phase 2 exit.

  **Partial delivery (updated 2026-08-29, through ADR-0044):** the CI release-artifact gate now
  derives a bounded build epoch from exact Git `HEAD`, overrides ambient timestamp input, and builds
  the same source set twice in separate source/output trees with the exact locked backend. It
  refuses filename, byte, or member-timestamp drift before publishing. The verified wheel is then
  installed with a separate runtime-only hash lock outside the checkout.
  It proves the shipped static assets and migration namespace match source bytes, executes the
  installed migration tree from the supported v2 baseline through its single head and validates
  the resulting schema, and exercises the console plus every packaged
  `src/chronos/**/__main__.py` command surface. A separately locked official tool emits validated,
  reproducible CycloneDX 1.6 JSON for that runtime environment; the gate cross-checks its exact
  components and root dependency edge against the runtime lock and wheel metadata. Exact-main CI
  requests 90-day retention for the wheel/SBOM pair. A separate exact-version, fail-closed release
  security gate now audits the hash-locked runtime set without resolution, scans shipped Python at
  medium-severity/medium-confidence or stronger, and checks every tracked file against an explicitly
  reviewed false-positive secret baseline. Its first run removed twelve current GitPython advisories
  and a fixed shared `/tmp` path. CI plus both release venvs now prevalidate an exact two-hash pip
  bootstrap lock and independently verify the installed frontend before other dependency operations;
  the SBOM requires that exact pip component and refuses unlocked environment extras. These scanners
  are current advisory/heuristic evidence: artifact
  signing, Git-history secret review, malicious-package provenance, the interpreter's initial
  offline `ensurepip` trust, independent/cross-platform rebuild evidence, and compromised-builder resistance remain
  open. This does not satisfy the Phase 2 exit.

### Real-gateway read-only gate

The owner installs and pins the official IB API, supplies a paper account and market-data
permissions, enables read-only mode, and keeps every transmit/live flag false.

For at least five sessions, including a gateway restart/reset, capture sanitized evidence
for exact account scope, server time, account summary, positions, executions, open and
completed orders, contract qualification, option chains, market rules/minimum ticks,
trading sessions, quote permissions, pacing, callbacks, and subscription cancellation.

**EXIT:** no mutation call; no leaked subscription, account drift, unexplained callback, or
pacing failure; captured fixtures replay offline exactly.

## 8. Phase 3 — Certified data and anti-overfit research factory

Begin forward option capture immediately; missed days cannot be recreated from IBKR.

Deliver:

- Uniform, point-in-time daily and hourly data across at least 6–10 liquid instruments,
  with exchange calendars, unadjusted prices, corporate actions, delistings where relevant,
  corrections, source receipts, and immutable dataset versions.
- Fresh, declared holdouts that are inaccessible through ordinary research paths.
- One brokered research reader. Every data touch writes `trial_started` before bytes are
  returned; completed and failed trials both count toward multiplicity.
- Order-invariant campaign scoring using one final global trial count and a reviewed
  cross-trial variance estimate. Candidate order or renaming must not alter a verdict.
- Full-campaign byte-identical replay from one manifest. Criteria/data/code/model changes
  invalidate the campaign.
- Point-in-time model evidence, deterministic cache, exact prompt/tool/provider identity,
  and comparison against deterministic baselines.
- Complete Wheel/options lifecycle simulation before an option strategy can qualify.

Starting data-quality gates, frozen before collection:

- At least 99.5% expected-session coverage.
- Every gap and extreme move classified; zero unresolved economically material conflicts.
- Corporate actions independently sampled and reconciled.
- Clean/seen/burned holdout map complete and content-addressed.

**Integrity progress (2026-08-26):** D-53/ADR-0039 makes the corporate-action half
content-addressed: certification v3 binds per-symbol distinct counts and semantic digests,
refuses duplicate/inflated claims, and gives legitimate zero-action windows an exact typed
evidence form. The frozen QQQ helper additionally refuses an all-empty six-symbol panel and
recomputes manifest counts from bytes. This closes a code false-positive only; no provider
completeness, owner reconciliation, or D2 certification gate has advanced.

Starting strategy gates, subject only to stricter preregistered power requirements:

- Sample floor: the typed power-required observation count **and** at least 100 out-of-sample
  closed trades must each pass; unlike units are not compared with a numeric `max`.
- Net expectancy and benchmark-alpha 95% lower bounds above zero after commission, spread,
  slippage, funding/borrow, model, and data costs.
- Deflated Sharpe probability at least 0.95; family-wise error or FDR `q <= 0.05`;
  probability of backtest overfitting at most 10% when applicable.
- Evidence across at least three instruments and two materially different regimes.
- Positive after removing the best trade and best month, and under doubled commissions plus
  stressed slippage.
- Parameter response is a plateau, not an isolated optimum; neighboring variants meet a
  preregistered stability fraction.
- Drawdown/CVaR inside owner-frozen limits; no instrument, trade, or period dominates the
  result beyond the frozen concentration bound.
- One untouched holdout passes unchanged. Failure means rejection, not tuning.

**EXIT:** at least one exact strategy-policy earns an immutable promotion artifact, or the
honest result remains zero selected.

## 9. Phase 4 — Evidence-bound autonomy ladder

The thresholds below are the proposed minimum 10/10 standard. Phase 0 must freeze them
before observation. Power analysis may increase a sample requirement; it cannot lower one
after results are seen.

| Rung | Minimum evidence before promotion |
|---|---|
| Replay | Byte-identical decisions and state over the complete evaluation/stress corpus; 100% decision/receipt coverage; zero unexplained research/runtime difference |
| Shadow | At least 90 calendar days **and** the power-required decision opportunities; at least 99.5% scheduled-cycle/data availability; zero duplicate/illegal intents, unresolved reconciliation, or Sev-1/Sev-2 incidents; restart, stale-data, disconnect, clock, alert, and restore drills pass |
| Supervised paper | At least 90 days, 50 completed round trips, the power-required observations, and 100 controlled order lifecycles across 20 sessions; 100% fill/commission/account reconciliation; implementation shortfall and cost-model error inside frozen bands |
| Autonomous paper | At least 60 trading days and 100 eligible lifecycles with the exact live candidate stack; at least 99.9% scheduler availability; no unplanned rescue, duplicate, unbounded order, unknown exposure, unresolved incident, or severe safety event |
| Live canary | At least 6 months **and** 50 round trips at minimum economically meaningful size under an absolute owner-approved loss budget; real fill/fee/slippage and tracking error stay inside frozen bands; independent review before each cap increase |
| Capped live | At least 12 months, 100 independent completed trades, and the power-required observations across at least two regimes; post-cost live expectancy and benchmark-alpha lower bounds above zero; 100% broker attribution/reconciliation; zero severe safety incidents or unauthorized exposure |

Every promotion artifact binds the exact account, commit, dependencies, configuration,
mandate, strategy-policy, model/prompt/tools, compiler/resolver, evidence/data versions,
criteria digest, incidents, approval, expiry, and rollback plan.

Any material change or statistical/operational health breach automatically demotes the
family to the appropriate earlier rung. Paper evidence proves machinery, not real liquidity
or alpha.

## 10. Phase 5 — Expand one asset family at a time

Success in one family never promotes another.

1. **Equities/ETFs:** close/protection/ownership semantics, one complete evidence ladder.
2. **Equity options:** authoritative deliverables and corporate-action adjustments;
   assignment/exercise, ex-dividend, expiration, cash-in-lieu, and exact lifecycle
   accounting. Multi-leg structures, if admitted, use one atomic combo order—never
   sequential naked legs.
3. **Futures:** contract/root model, deterministic front-contract and roll receipts,
   exchange calendars and overnight sessions, tick/multiplier/price-limit facts, initial
   and maintenance margin, daily settlement, first notice, expiry/delivery, and exact
   contract reconciliation.
4. **Index options:** a distinct capability with cash settlement, exercise style, AM/PM
   settlement, special opening quotes, multipliers, expiry, and package-level risk. SPY
   semantics do not transfer to SPX.
5. **Crypto, only if retained by Phase 0:** keep long-only spot unless explicitly widened;
   use a broker sandbox or separately reviewed paper-equivalent rung. Never silently skip
   the missing IBKR paper capability.

An honestly narrow declared scope can be 10/10 only after the owner explicitly changes the
declared product mission. A broad vocabulary with unimplemented or unproven families
cannot.

## 11. Owner gates

Only the owner may supply or approve:

- Broker credentials, 2FA, account configuration, API permissions, and gateway access.
- Market-data subscriptions and option-reference-data licensing/legal terms.
- Capital, loss, drawdown, CVaR, concentration, turnover, exposure, and incident-response
  availability decisions.
- Holdout unlock, paper mandate, canary authorization, live promotion, and every cap
  increase.
- Manual broker resolution of unknown orders, positions, assignments, or ambiguous sends.
- Tax, regulatory, and account-structure review.

No test result, backtest, backup, or agent recommendation substitutes for an owner gate.

## 12. Rough planning horizon

These are estimates, not promises:

- Engineering-complete first-family platform: approximately 6–9 months for one focused
  owner working with agents.
- Shadow/paper operational proof: another 2–4 months, partly overlapping engineering.
- The proposed canary plus capped-live standard requires at least 18 months of prospective
  evidence after a strategy is frozen.
- From the 2026-08-01 baseline, one-family proven-autonomy 10/10 is realistically a
  24–36+ month objective. Full multi-family completion is longer.
- Without licensed expired-options history, option validation becomes calendar-bound and
  can take multiple years.

The critical path is authority coherence, model/evidence provenance, broker reconciliation,
trusted data, an eligible strategy, and calendar-time operational evidence—not more UI or
unsupported asset vocabulary.

## 13. Agent task contract

Every implementation or review must state:

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

Do not edit a frozen criterion after seeing its evidence. Do not claim plan completion from
code coverage alone. Update this document only when live state, scope, sequencing, or a gate
actually changes; record the evidence and commit that caused the change.

Agents may make evidence-backed factual-status updates with rerunnable verification. Agents
may only **propose** changes to product scope, either 10/10 definition, KPI or promotion
thresholds, owner gates, document precedence, or the rule that a material change resets
evidence. Those governance changes require explicit owner approval before merge; an agent
cannot approve its own easier definition of completion.

## 14. External control references

- [IBKR TWS API documentation](https://www.interactivebrokers.com/docs/tws-api/doc/introduction)
- [IBKR paper-account limitations](https://ibkrcampus.com/campus/glossary-terms/paper-trading-account/)
- [OCC product/series data](https://www.theocc.com/market-data/market-data-reports/other-market-data-info/data-sales)
- [OCC contract-adjustment memos](https://infomemo.theocc.com/infomemo/search)
- [FINRA algorithmic-trading control practices](https://www.finra.org/rules-guidance/notices/15-09) — a useful engineering benchmark, not a claim about Chronos's regulatory status
- [NIST AI Risk Management Framework](https://airc.nist.gov/airmf-resources/airmf/5-sec-core/)
- [OWASP prompt-injection guidance](https://genai.owasp.org/llmrisk/llm01-prompt-injection/)
- [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID2460551_code87814.pdf?abstractid=2460551)
- [A Reality Check for Data Snooping](https://doi.org/10.1111/1468-0262.00152)
