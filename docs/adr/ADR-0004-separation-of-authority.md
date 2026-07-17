# ADR-0004 — Structural separation of authority; no generative AI in any runtime path

Status: Accepted (2026-07-17). Index entries: DECISIONS.md D-04 and D-11.

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
5. **No generative model output feeds any runtime decision.** AI was used offline to author code,
   audits, and documentation; every runtime path is deterministic, versioned, and tested. No LLM
   client, API call, or model artifact exists anywhere in `src/chronos`.

## Consequences

- A strategy cannot fabricate, replay, or forge an approval (tested: forged approval, foreign
  engine token, approval/intent mismatch all refuse).
- Adding a new strategy requires zero changes to risk or execution code, and grants it no new
  authority: its strategy id must still appear on the policy allowlist.
- The cost is boilerplate: four layers between a signal and a broker call. This is accepted.
- "No AI in runtime" is verifiable by inspection: there are no model dependencies in
  `pyproject.toml` and no network calls in the decision path.
