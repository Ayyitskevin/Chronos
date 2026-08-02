# ADR-0020 — Bounded periodic reconciliation and a maximum evidence age

Status: Accepted (owner direction, 2026-08-02). Index entry: DECISIONS.md D-20.

## Context

`ReconciliationReadiness` is a fail-closed latch: it starts `PENDING` and becomes
`RECONCILED` only through `complete()`, bound to one broker connection generation
(`src/chronos/orders/reconciliation_readiness.py`).

Two things consume it. `submission_guard` (:131-148) resets it to `PENDING` on **every**
opening submit — deliberately, because "a proven empty/parity snapshot authorizes at most
one opening submission" — and `invalidate()` resets it on any connection uncertainty.

Exactly one thing re-establishes it: `runtime.reconcile_submission_readiness()`, called
**once**, in the backend lifespan at startup (`src/chronos/api/main.py:201`).

The consequence, which is the defect recorded as finding 1 in
`docs/VISION_COMPLETION_PLAN.md` §6: the first opening order of a process consumes
readiness and **nothing ever re-arms it**. Every subsequent opening order is blocked until
the process restarts. `docs/VISION_COMPLETION_PLAN.md` §7 requires "startup, reconnect,
order/fill-triggered, and bounded periodic reconciliation with a maximum evidence age";
only the startup trigger exists, and there is no maximum evidence age at all —
`reconciled_at` is recorded but nothing expires on it.

### This change widens, and is gated accordingly

The current behavior fails closed: after one opening order, everything blocks. A loop that
re-arms readiness therefore makes submissions possible that are blocked today. Restoring
intended function is still **widening in effect**, which places it in the
safety-mechanism-modification row of the change-control rules and makes it an owner
decision rather than an agent's. It is recorded here rather than slipped in, following the
precedent ADR-0018 §4 set for the terminal's authorization surface and R-39 for mandate
revocation. The owner directed both the change and its parameters on 2026-08-02.

### What sets the clock

Reconciliation exists to detect the ways the book changes **without an order this system
placed**. For the declared scope there are two: a fill on a resting limit, which can land
at any moment during regular trading hours, and an assignment, which the OCC posts after
the close and which the operator learns about at the next session. Corporate actions and
manual trades in TWS are the same class of event on a slower clock.

Two constraints bound the cadence from the other side:

1. **The connection is shared.** Reconciliation issues broker requests on the same single
   connection the order pipeline and the autonomy tick use. `PacingController` allows
   `_DEFAULT_MAX_PER_WINDOW = 6` requests per rolling window plus a per-key cooldown
   (`src/chronos/marketdata/pacing.py:40`). R-42 records what happens when a background
   reader is allowed to contend with submission on IBKR's most rate-limited surface.
2. **Headroom is a safety property, not spare capacity.** Rate limit spent watching the
   book is rate limit unavailable to *cancel* something. A reconciliation cadence that
   consumes the budget converts a small operational problem into one the operator cannot
   act on.

## Decision

### 1. A bounded periodic reconciliation task

The backend lifespan runs a writer-only reconciliation task, modeled on
`autonomy_tick_task`: cancelled on shutdown, and **skipping rather than queueing** when a
submission is in flight. `complete()` already refuses to publish while
`_submissions_in_flight` is nonzero (:122), so the latch is race-safe; the loop must not
spin against it. A read-only backend never publishes readiness and stays `PENDING`.

### 2. Frozen intervals

| Condition | Interval | Why this number |
|---|---|---|
| RTH, any open position or working order | **120 s** | Fills land unpredictably in the only window the book can change; two minutes bounds how long the system may be wrong about its own position |
| RTH, flat | **240 s** | A flat book has nothing to drift; do not spend budget that may be needed to act |
| Market closed | **1800 s** | Nothing moves except assignment postings |
| **Maximum evidence age** | **300 s** | Older readiness is stale and authorizes nothing |

### 3. Expiry is enforced at read time

The maximum evidence age is applied in `snapshot()`, not by the loop. A proof older than
300 s degrades to `PENDING` with a stated reason **whether or not the loop is running**.
Correctness must not depend on the health of the component whose failure it is guarding.

**Missed cycles fail closed by arithmetic rather than by error handling**, and the two
cadences are deliberately not equally forgiving:

| Cadence | Next attempt after one miss | Verdict at the 300 s age |
|---|---|---|
| Active, 120 s | 240 s | still valid — one miss survivable, two (360 s) fail closed |
| Idle, 240 s | 480 s | **already expired** — idle tolerates zero misses |

Idle is therefore the stricter of the two, which is the right direction: a flat book has
no position to protect, so blocking early costs nothing. Neither case needs a failure
detector for the system to stop trusting itself.

The `reconciliation_max_evidence_age_seconds` value must exceed both RTH intervals, or
readiness would expire before its own scheduled refresh even with nothing failing. A
settings validator enforces that. It is deliberately **not** required to exceed the
closed-market interval: while the market is closed, readiness expiring and staying expired
is the correct state.

### 4. Readiness does not cross the session open

Readiness established before a regular-session open never authorizes the first opening
order of that session; a fresh reconciliation is required regardless of age or cadence.

Overnight assignment is the single event most likely to make the book differ from what the
system believes, and it lands precisely when the system is about to trade. For a wheel
book this is the dominant case: a short put assigned overnight changes the position, the
cash, and the eligible next action, and none of it came from an order this system placed.

### 5. Explicitly out of scope

Order/fill-triggered reconciliation — also named in plan §7 — is a different trigger
source and is **not** part of this change. Reconnect-triggered invalidation already exists
via `invalidate()`.

## Consequences

- Opening orders past the first become possible again within a process lifetime. That is
  the intended function being restored, and it is the widening this ADR gates.
- Steady-state broker load rises by roughly one reconciliation per 120 s while positioned.
  At an estimated 4-6 requests per reconciliation that is ~2-3 requests per minute against
  a 6-per-window budget, leaving deliberate headroom for submission and cancellation. **If
  the measured per-reconciliation request count exceeds 6, the active interval stretches
  rather than the headroom shrinking** — the headroom is the invariant, not the cadence.
- A stale latch now blocks where an indefinitely-old one previously would have authorized
  had anything re-armed it. This is a tightening that ships in the same change as the
  widening, and it is why the maximum evidence age is not deferred.
- The intervals are frozen operational thresholds. Changing them is an owner decision under
  the same rule that governs every other frozen threshold; they may not be widened to make
  a symptom disappear.
- Paper and live are unaffected in posture: reconciliation authorizes nothing on its own,
  it only re-establishes one of the conditions submission already required.

## Verification

Exercised tests, per the house pattern — proving the control fires, not merely that the
code path exists:

- readiness re-arms after `submission_guard` consumes it;
- the loop does **not** publish while a submission is in flight;
- a proof older than 300 s reads as `PENDING` with no loop running;
- two consecutive failed cycles leave the latch blocked;
- readiness established before the session open does not authorize the first order after it;
- a read-only backend never publishes;
- a failed reconciliation leaves `PENDING` and alerts rather than retrying quietly.

Each conjunct verified by reverting it and confirming a distinct failure.
