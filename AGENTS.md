# AGENTS.md — Chronos repository contract

These instructions apply to every AI agent and contributor working in this repository.

## Read before acting

Read these files completely before proposing, reviewing, or changing Chronos:

1. `docs/VISION_COMPLETION_PLAN.md` — canonical north star, sequencing, scorecards, and
   promotion evidence.
2. `DECISIONS.md` and the ADRs relevant to the change — accepted authority and
   architecture.
3. `docs/safety.md`, `docs/limitations.md`, and `RISK_REGISTER.md` — controls, honest
   capability boundaries, and residual risk.

Historical game plans and handoffs preserve rationale; they are not current roadmap
authority when they conflict with the vision-completion plan.

## Non-negotiable build rules

- Treat **platform/safety 10/10** and **proven autonomous trader 10/10** as independent
  outcomes. Code completion is not operating or economic proof.
- A correct `NO_TRADE` result is success when evidence is insufficient. Never weaken a
  gate to manufacture progress.
- Complete one vertical first: liquid U.S. equities/ETFs, unless the owner explicitly
  changes the canonical plan. Promotion never transfers across asset families.
- Freeze statistical, operational, and financial thresholds before observing the evidence
  they judge. A failed holdout rejects the candidate; it does not invite threshold edits.
- Every economic-looking field must be mechanically enforced, explicitly advisory, or
  forbidden. Inert authority, risk, exit, or protection fields are release blockers.
- Preserve one canonical execution boundary, broker truth, deterministic veto authority,
  idempotency, auditability, and fail-closed behavior under missing or ambiguous facts.
- Money-critical, live-broker, security-sensitive, capital/risk-limit, and promotion
  changes require explicit owner review. Tests, CI, and agents must not place live orders.
- Reverify point-in-time findings against the current commit before implementing them.
  Branches, capabilities, broker APIs, data, and prior handoffs are claims, not live state.
- At task start, name the plan phase, KPI, acceptance gate, and intended file set. At task
  close, provide rerunnable verification and state what remains. Do not claim a rung or
  score advanced without its required evidence artifact.

## Document precedence

When repository documents disagree:

1. Explicit owner direction within safety and human-approval boundaries.
2. Current executable facts and unresolved safety/security defects. A live defect always
   blocks promotion; roadmap or ADR prose cannot waive it.
3. Accepted ADRs plus `DECISIONS.md` for intended authority and architecture.
4. `docs/safety.md`, `docs/limitations.md`, and `RISK_REGISTER.md` for controls and
   disclosed residuals.
5. `docs/VISION_COMPLETION_PLAN.md` for roadmap order and completion criteria.
6. Historical plans, task boards, briefs, and handoffs for context only.

Stop and surface a contradiction; never average incompatible instructions.
