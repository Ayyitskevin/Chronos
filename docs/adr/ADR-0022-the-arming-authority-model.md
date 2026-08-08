# ADR-0022 — Does an autonomy mandate replace session arming?

Status: **proposed — owner decision required.** No `DECISIONS.md` row until accepted.

Date: 2026-08-02

Closes, if accepted: `docs/VISION_COMPLETION_PLAN.md` §6 finding 4.

## Context

Three documents said an active `AutonomyMandate` replaces live gates 7 (session arming)
and 8 (per-order typed confirmation). The code disagreed, and on 2026-08-02 the prose was
corrected to say "is intended to replace" rather than "replaces", which stopped the docs
being false but decided nothing. This ADR is the decision they were deferring.

### What the code actually does — verified 2026-08-02

The current state is **not** "the mandate replaces neither gate". It is inconsistent
between the two, and that is the finding worth reading twice:

| Gate | Autonomy path today | Evidence |
|---|---|---|
| 8 — typed confirmation | **Effectively replaced already.** The wiring calls `service.confirm(...)` itself, programmatically, before submitting. No human types anything. | `src/chronos/api/autonomy_wiring.py:202` |
| 7 — session arming | **Not replaced.** `armed = self._live_arming.is_armed(now=fresh_now)` runs unconditionally on the live path, and the gate refuses when false. `chronos.orders` contains zero references to a mandate. | `src/chronos/orders/submission.py:441`; `src/chronos/orders/live_gate.py:61, 133-135`; `grep -rc mandate src/chronos/orders/` → no matches |

So the mandate has already, de facto, taken over one of the two gates it was said to take
over — by the wiring supplying the confirmation rather than by any mandate-aware code —
while the other still requires a human act the mandate cannot perform.

The practical consequence: **live autonomous trading is currently impossible without a
human arming the session that day**, and the arm is in-process memory with a bounded TTL,
so it also dies on restart. That directly contradicts ADR-0017's stated intent — "A
running backend plus a valid mandate file is now sufficient to trade; there is no per-boot
ritual" — which is why ADR-0017 carries an amendment note recording that the sentence does
not hold for LIVE.

### One thing that is not a defect

The same wiring passes `writer_lease_held=True` as a literal (`autonomy_wiring.py:203`).
That reads alarming and is not the only defense: the submission boundary re-checks lease
ownership *in the database* through the verifier bound at startup, immediately before the
transmit line. The literal is a stale-looking argument, not a bypass. It should be cleaned
up, but it is not part of this decision.

## The decision

The real question is narrower than "which document wins":

> **Should unattended live autonomous trading be possible with no same-session human act?**

Everything else follows.

### Option A — The mandate replaces arming (code moves to the prose)

Gate 7 accepts a valid, active, account-matching mandate in place of a session arm for
autonomy-originated orders. Human-originated live orders keep requiring the arm.

- **For:** honours ADR-0017's explicit owner directive. The mandate is arguably the
  *stronger* artifact: durable, expiring, revocable, digest-stamped, account-bound and
  surviving restart, where the arm is a phrase in process memory with a short TTL that a
  restart erases. Unattended operation is the declared product goal.
- **Against:** this is a **widening**. It removes the last same-session human checkpoint
  from the live path, and it does so for the exact orders no human reviewed. It also means
  `chronos.orders` must become mandate-aware, which today it deliberately is not — that
  import direction is currently clean and would stop being so.
- **Requires:** a new gate input, mandate-awareness (or an injected authorization token)
  in the order plane, exercised tests proving the substitution fires *and* that an
  expired/revoked/wrong-account mandate still refuses.

### Option B — Arming stays for everything (prose moves to the code)

The mandate never replaces gate 7. Live autonomous operation is *supervised-live only*: a
human arms the session, and within that window autonomy may trade.

- **For:** no widening; preserves a daily human presence signal on the one path that moves
  real money; smallest change (documentation plus an ADR-0017 amendment).
- **Against:** contradicts ADR-0017's owner directive, and makes "autonomous" mean
  "autonomous within a human-opened window". Unattended overnight or multi-day operation
  becomes impossible on live.
- **Requires:** amending ADR-0017 §4 to except live arming explicitly, and correcting the
  three prose sites to say the mandate replaces gate 8 only.

### Option C — Legitimize the split (mandate replaces confirmation, not arming)

State as policy what the code already does: the mandate stands in for per-order typed
confirmation, and never for session arming.

- **For:** the smallest gap between what is written and what runs — the confirmation
  substitution already happens. Keeps the human-presence signal. Honest immediately.
- **Against:** it is Option B with the confirmation half acknowledged, so it inherits B's
  conflict with ADR-0017; and it blesses a substitution that currently happens by a wiring
  call rather than by a reviewed mechanism, which deserves its own exercised tests either
  way.

## Recommendation

**Option C now, Option A only as a deliberate later step.**

The reasoning is sequencing, not preference. Option A is a real widening of the live path,
and this system has **never connected to a real gateway** — no paper session, no live
session, no operational evidence of any kind. Removing the last same-session human
checkpoint before a single order has ever been placed against a real broker would be
widening authority on a path whose behaviour has never once been observed. That is
precisely the trade the promotion ladder exists to prevent.

Option C makes the documents true today, costs nothing, and leaves A available the moment
there is evidence to justify it — specifically, after the read-only gateway gate and
supervised paper rungs have produced operating history. At that point A is a small change
against a system whose live path has actually run, and it can be judged on evidence rather
than intent.

If the owner prefers A now, that is a legitimate call and it is the owner's to make — but
it should be taken knowingly as a widening, sequenced with its own exercised tests, and
not as a documentation cleanup.

## Consequences if accepted as recommended (Option C)

- The three prose sites and ADR-0017's amendment note are updated to state the split as
  policy: mandate replaces gate 8, never gate 7.
- The confirmation substitution gains exercised tests it currently lacks — proving that an
  autonomy-originated order is confirmed by the wiring, and that a *human*-originated live
  order still requires a real typed confirmation.
- `writer_lease_held=True` at `autonomy_wiring.py:203` is replaced by the real lease check
  it currently shadows. Separate from this decision, but it should not outlive it.
- Finding 4 closes. Unattended live autonomy remains out of reach by design until an owner
  takes Option A in a later ADR.

## What this ADR does not decide

Whether autonomy may trade live at all — that is the promotion ladder's question, and no
family has cleared any rung. This decides only which gates a mandate may stand in for.
