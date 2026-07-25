# ADR-0004 — Structural separation of authority; no generative AI in any runtime path

Status: **Accepted in part; §5 superseded by [ADR-0016](ADR-0016-controlled-autonomous-model-authority.md)
(2026-07-25).** Index entries: DECISIONS.md D-04 (in force) and D-11 (superseded by D-16).

> **Scope of the supersession.** Only **§5 (No generative model output feeds any
> runtime decision)** is superseded. Sections 1-4 — the structural separation of
> authority indexed as D-04 — remain **in force and load-bearing**, and ADR-0016
> depends on them: strategies still emit proposals that cannot express an order,
> the portfolio layer still sizes, the risk engine is still deny-by-default with
> a frozen policy and instance-identity approvals, and only the execution layer
> talks to a broker adapter. ADR-0016 reuses the §1 technique — make the
> dangerous thing unrepresentable in the type — for the model's `AITradeDecision`.
>
> This document is kept as written so the history stays legible. Read §5 as a
> record of the posture through 2026-07-25, not as current policy.

## Context

The most dangerous failure class in a personal trading system is a strategy bug (or a subverted
strategy) reaching a broker directly, or a risk check that can be bypassed, mutated, or forged.
Convention ("strategies shouldn't call the broker") is not enforcement. Separately, the build brief
forbids any generative-model output from feeding a runtime decision.

## Decision

Authority is separated by construction, not convention:

1. **Strategies emit `StrategyProposal` objects only** (`src/chronos/strategies/base.py`). The
   proposal type has no quantity, account, or broker fields — it cannot express an order. The
   safety suite asserts the strategy package does not import broker modules
   (`tests/safety/test_safety_invariants.py::TestStrategyIsolation`).
2. **The portfolio layer converts proposals to sized `OrderIntent`s**
   (`src/chronos/portfolio/sizer.py`): whole shares, floor-rounded; exits sell exactly the held
   quantity; no shorts can be created by construction.
3. **An independent risk engine validates every intent deny-by-default**
   (`src/chronos/risk/engine.py`, policy in `src/chronos/risk/policy.py`). The policy object is
   frozen with `extra="forbid"`; there are no setters; an internal engine exception becomes a
   denial (`INTERNAL_ERROR_FAIL_CLOSED`), never an approval. Approvals are minted with an
   instance-identity token.
4. **Only the execution layer talks to a broker adapter** (`src/chronos/execution/engine.py`). It
   refuses approvals not minted by the exact wired engine instance, refuses duplicates via the
   durable ledger, and refuses everything while halted, unreconciled, or in a non-submitting mode.
5. ~~**No generative model output feeds any runtime decision.** AI was used offline to author
   code, audits, and documentation; every runtime path is deterministic, versioned, and tested. No
   LLM client, API call, or model artifact exists anywhere in `src/chronos`.~~
   **SUPERSEDED by ADR-0016 / D-16 (2026-07-25).** An approved generative model may originate
   runtime trading decisions, but only through a typed `AITradeDecision` and the single
   deterministic `ModelDecisionGateway`, inside an active owner-authored AutonomyMandate. The
   model cannot access IBKR directly, change its authorization, weaken policy, or bypass any
   deterministic gate, and the deterministic kernel retains unconditional veto authority. The
   hazard §5 was aimed at is now addressed by structure (one decision type, one gateway, one
   transmit site, an expiring owner mandate) rather than by prohibition.

## Consequences

- A strategy cannot fabricate, replay, or forge an approval (tested: forged approval, foreign
  engine token, approval/intent mismatch all refuse).
- Adding a new strategy requires zero changes to risk or execution code, and grants it no new
  authority: its strategy id must still appear on the policy allowlist.
- The cost is boilerplate: four layers between a signal and a broker call. This is accepted.
- ~~"No AI in runtime" is verifiable by inspection: there are no model dependencies in
  `pyproject.toml` and no network calls in the decision path.~~ **Superseded by ADR-0016.** This
  consequence also recorded an honest weakness: the rule was enforced *by inspection*, not by
  tests. ADR-0016 replaces it with structural enforcement — the model plane's isolation, the
  decision type's inability to express an order, and the mandate's deny-by-default limits are all
  asserted in `tests/safety/`.
