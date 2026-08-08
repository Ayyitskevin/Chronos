# ADR-0021 — The four inert economic fields on `ProposedDecision`

Status: **proposed — owner decision required.** No `DECISIONS.md` row until accepted.

Date: 2026-08-02

Closes, if accepted: `docs/VISION_COMPLETION_PLAN.md` §6 finding 7.

## Context

`AGENTS.md:29-30` is unambiguous:

> Every economic-looking field must be mechanically enforced, explicitly advisory, or
> forbidden. Inert authority, risk, exit, or protection fields are release blockers.

Four fields on `ProposedDecision` are currently none of the three:

| Field | Declared at | Read anywhere else? |
|---|---|---|
| `requested_risk_budget_usd` | `autonomy/decision.py:270` | `supervisor/queue.py:144-145` only |
| `max_acceptable_loss_usd` | `autonomy/decision.py` | `supervisor/queue.py:149-150` only |
| `protective_order_required` | `autonomy/decision.py` | `supervisor/queue.py:152` only |
| `exit_plan` (`profit_target`, `protective_stop`, `time_exit`) | `autonomy/decision.py:273`, `ExitPlan` at `:223-228` | `supervisor/queue.py:169-173` only |

`supervisor/queue.py` reads all four **solely to compute the dedup fingerprint** — the
UUIDv5 over economic content that gives a re-proposed trade the same decision id. So they
change *whether a proposal is recognised as a repeat*, and nothing else. A model can set
`protective_order_required=True`, have the decision admitted, sized, compiled, and
submitted, and no protective order will exist.

### A precise wrinkle worth naming

`autonomy/decision.py:19-23` groups two fields together:

> **Requests, not instructions.** `requested_quantity` and `requested_risk_budget_usd` are
> *requests*. Deterministic code independently resolves and qualifies the contract,
> computes and clamps the final quantity…

That is true of `requested_quantity`, which sizing consumes. It is **not** true of
`requested_risk_budget_usd`, which sizing never reads. The sentence is accurate about the
kernel's authority and misleading about this specific field: it implies the field is
considered and clamped, when it is simply ignored. Whichever option below is chosen, that
docstring needs correcting.

### Why this is not a cosmetic cleanup

The repository's signature failure class is a control that is wired, documented, and tested
while being structurally unable to act — R-25, R-26, and R-27, each caught only by a
dedicated adversarial review. These four fields are the same shape at the contract layer:
they read as risk and protection controls to anyone reviewing the decision type, and they
control nothing. `protective_order_required` is the most dangerous of the four, because a
reader — human or model — may reasonably conclude a protective order is guaranteed.

## The decision to make

`AGENTS.md` permits exactly three dispositions per field. They are not equally available
here, because two of these fields require machinery that does not exist.

### Group A — the monetary caps (`requested_risk_budget_usd`, `max_acceptable_loss_usd`)

These can be enforced **cheaply and monotonically**: the gateway clamps to
`min(mandate ceiling, model request)`. A model asking for *less* than its mandate allows
can only ever reduce exposure, so honouring it never widens authority and never requires a
new ceiling. Absent or malformed values fall back to the mandate ceiling, i.e. today's
behaviour.

| Option | Consequence |
|---|---|
| **A1 — Enforce as a self-imposed ceiling** | Monotonically restricting; small change confined to sizing; the field becomes real. Cost: sizing gains a second input and needs exercised tests proving the clamp fires. |
| A2 — Explicitly advisory | Honest and nearly free, but a "risk budget" that binds nothing invites the same misreading it does today, only with a label. |
| A3 — Forbid (remove) | Cleanest contract; loses a genuinely useful safety expression the model can volunteer. |

### Group B — the exits and protection (`protective_order_required`, `exit_plan`)

Enforcement is **not** cheap. `docs/VISION_COMPLETION_PLAN.md` §6 states plainly that
"deterministic exits/protection require a durable position-management lifecycle", and that
lifecycle does not exist: nothing in the order plane holds a position open, watches a
trigger, and emits a protective or closing order. Enforcing these fields means building
that first.

| Option | Consequence |
|---|---|
| **B1 — Forbid until the lifecycle exists** | The contract stops promising protection the system cannot deliver. Re-add them in the same change that builds the lifecycle, where they can be enforced on arrival. Cost: a model may no longer express an exit intent, and the narrative fields must carry it instead. |
| B2 — Explicitly advisory | Keeps the vocabulary, but a **protection** field that is advisory is precisely the trap R-25/26/27 describe: it looks like a control and is not. A reviewer who sees `protective_order_required=True` on an audited decision would have to know, from outside the contract, that it meant nothing. |
| B3 — Enforce now | Requires building the position-management lifecycle: order-triggered state, durable triggers, a second submission occasion, and its own ADR. Large, and it widens what the autonomy plane can cause to happen. |

## Recommendation

**A1 for the monetary caps, B1 for the exits and protection.**

The asymmetry is the point. A self-imposed *cap* is safe to honour immediately because
obeying it can only shrink exposure. A *protective order* is not a cap — it is a promise to
act later, and the system has nothing that acts later. Making the first real and retiring
the second until it can be real leaves the contract saying only true things, which is the
whole intent of the `AGENTS.md` rule.

B3 is a legitimate choice but should be sequenced deliberately: it is a widening of what
autonomy can cause, and it belongs in Phase 2's position-management work rather than being
smuggled in as a field-classification fix.

## Consequences if accepted as recommended

- Sizing gains a clamp against the model's stated budget, with exercised tests proving the
  clamp fires and that an absent value falls back to the mandate ceiling.
- `protective_order_required` and `exit_plan` (with `ExitPlan` and its `PriceTrigger`
  members, if unused elsewhere) leave `ProposedDecision`. Existing recorded decisions keep
  their history; the dedup fingerprint changes shape, so **previously-seen proposals may
  mint new decision ids once** at the boundary — that is a replay-detection consideration
  to state in the implementing PR, not a safety hazard, since every gate still applies.
- `autonomy/decision.py:19-23` is corrected so it no longer implies
  `requested_risk_budget_usd` is clamped when it is ignored.
- Finding 7 closes; `AGENTS.md:29-30` is satisfied for these four fields.

## What this ADR does not decide

The position-management lifecycle itself. If the owner chooses B3, that is a separate ADR
with its own scope, and this one should be superseded rather than stretched.
