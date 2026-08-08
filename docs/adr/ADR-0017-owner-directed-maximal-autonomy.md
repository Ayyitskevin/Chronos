# ADR-0017: Owner-Directed Maximal Autonomy and the Persistent Mandate

Status: accepted (owner directive, 2026-07-25)
Date: 2026-07-25
Index entry: DECISIONS.md **D-17**.
Supersedes, in place and by scope:
- **ADR-0016 §4** — the 30-day live-mandate ceiling and the "an environment
  variable alone may never activate live autonomous trading" rule.
- **ADR-0016 §6** — the "`OrderForm` has no `MARKET` member" rule (this ADR is
  the "instrument-specific ADR, tests, and mandate permission" §6 required
  before the enum could grow).
- The M4 compiler's "prefer the least aggressive permitted form" rule.
- ADR-0016 §4's "deny-by-default" as applied to capital **ceilings**, and only
  where a mandate explicitly grants `model_discretion`.

Everything else in ADR-0016 — §1-3, §5, §7, §8, and the whole deterministic
kernel — is **preserved and load-bearing**, and this ADR depends on it.

## Context

ADR-0016 gave a model trade-time authority inside an owner-set envelope, but
built that envelope to be maximally cautious: a 30-day mandate the owner renews
monthly, an explicit per-boot activation on top of configuration, every capital
ceiling deny-by-default so silence sized to zero, no market order at all, and a
compiler that preferred the passive fill even when the owner granted the
aggressive one. It also, by the close of M7, left the runtime a *class* — the
tick existed but nothing constructed it with real facts and a real handoff
(RISK_REGISTER R-36 residual).

The owner has now given an explicit, directive-level instruction: Chronos is to
be **as close to fully autonomous as possible**, modeled on the reference
"Quant Guild" AI trading bot, where bringing the process up is enough for the
model to trade and to size its own positions. Two design questions were put to
the owner directly and answered:

- **Autonomy scope — "maximal / near-parity":** loosen the sizing clamp and the
  capital/drawdown ceilings toward the model's discretion, matching how the
  reference bot lets the model self-size.
- **Mandate model — "persistent, auto-activating":** a long-lived mandate that
  survives restart and re-arms on startup; bringing the process up is enough to
  trade, superseding the "an env var alone may not activate live" rule.

This ADR records that override the way this project has always recorded
overrides — dated, scoped, with the owner as the authority and the superseded
text marked in place (the D-11→D-16 pattern) — rather than by quietly deleting
the guarantees the previous posture asserted.

### The line this ADR draws, stated plainly

"Maximal autonomy" was read as **removing friction and owner-optional ceilings,
not removing execution-correctness mechanisms.** Two kinds of gate were
distinguished:

- **Friction / loss-prevention ceilings the owner may choose to set or not** —
  per-order notional, share/contract caps, allocation, drawdown and exposure
  ceilings, the monthly renewal, the per-boot activation ritual. These are the
  owner's risk call, and the owner chose to make them optional. This ADR loosens
  them.
- **Execution-correctness mechanisms that make an order *correct* rather than
  *smaller*** — the single transmit site, the writer lease and fencing, durable
  idempotency, reconciliation to broker truth, the kill switch and halt, the
  cash/buying-power floors and reserve, stale-data refusal, and the
  deterministic veto. These are not "how big" but "is this a valid, non-duplicate,
  reconciled order at a sane price." **None of these is touched.** Removing them
  would not be more autonomy; it would be a different, broken system.

Market orders were genuinely ambiguous in the reference model, which uses
unbounded `MKT`. This ADR implements them as **protected** (a collared marketable
limit), honoring the autonomy directive while keeping the "no unbounded market
order" safety property. The literal unbounded-`MKT` interpretation is called out
below as a separate decision **not** taken; it remains available as its own
explicit owner act if ever wanted.

## Decision

### 1. The persistent, auto-activating mandate

An owner-authored JSON document, named by `AUTONOMY_MANDATE_FILE`, is validated
against `AutonomyMandate` on **every boot**. Present, valid, and scoped to this
account → it is loaded and **auto-activated**: the activation row is written with
an owner-event id derived from the file's SHA-256 digest, so the audit trail
records *which text* granted the authority and an edited file writes a new,
distinguishable activation. A running backend plus a valid mandate file is now
sufficient to trade; there is no per-boot ritual.

> **Amendment note, 2026-08-02 (record, not a rewrite — this ADR stands as
> written).** The sentence above holds for the PAPER submission branch. It does
> **not** hold for LIVE: gate 7 (session arming) was never removed from the order
> plane, so a LIVE autonomous submit still requires a current, in-process arm
> (`src/chronos/orders/submission.py:441`; `grep -rc "mandate" src/chronos/orders/`
> returns zero). The intended substitution is therefore unimplemented for live
> trading. This is open finding 4 in `docs/VISION_COMPLETION_PLAN.md` §6, and
> resolving it — whether the code moves to the ADR or the ADR moves to the code —
> is an owner decision requiring a new ADR. It is deliberately not resolved here.

Two supporting defaults moved:

- `MAX_LIVE_MANDATE_DURATION` 30 days → **365 days**. For a single-operator
  system a monthly re-authorship added friction without adding safety. Renewal at
  the year boundary is still a fresh owner action — there is still no perpetual
  live authority, and `expires_at` is still required and still enforced.
- `AutonomyMandate.restart_behavior` default `REQUIRE_REACTIVATION` →
  **`RESUME_UNTIL_EXPIRY`**. The vocabulary lost nothing; `REQUIRE_REACTIVATION`
  remains available for an owner who wants the stricter behavior back. Only the
  default moved.

The wiring lives in `chronos.api.autonomy_wiring`, in the **app plane**
deliberately: it is the one place allowed to import both the supervisor (to
drive it) and the broker surface (to gather facts), the combination the
supervisor itself is structurally forbidden from holding. `build_autonomy_runtime`
assembles the stack from settings; the backend lifespan constructs it and drives
a tick task. This closes the R-36 residual: a shipped entrypoint now constructs
the runtime with a real fact gatherer and a real order-plane handoff.

**What auto-activation deliberately does NOT override:**

- **A revoked activation stays revoked across restart.** Revocation is the owner
  standing the system down; a restart must not undo it, or revoking would mean
  racing the process supervisor. Re-granting is a new `mandate_version` in the
  file — a fresh owner act. `ensure_activation` refuses to re-arm a revoked
  mandate and raises a WARNING alert instead.
- **An invalid, unreadable, or wrong-account mandate file is no mandate.** The
  backend boots, trading stays inert, and a CRITICAL owner alert says why. A
  broken grant must not take down the process that can still close positions.
- **Expiry still expires**, and admission still refuses an expired mandate
  regardless of what was loaded.
- **No mandate file → no runtime.** This is the one remaining non-maximal
  default, kept on purpose: a fresh checkout with no owner-authored grant
  anywhere boots inert, because the grant is the owner act everything hangs from.

### 2. Model self-sizing (`model_discretion`)

`CapitalLimits` gains a `model_discretion: bool = False` flag — the owner's grant
of self-sizing. When True, the `max_*` capital and exposure ceilings become
**optional overlays**: a ceiling the owner explicitly set (a positive value)
still binds exactly as before, but an unset (zero) ceiling no longer reads as
"authorizes nothing." Sizing is then bounded by what the account can actually
afford — cash and buying power **net of the floors** — instead of clamping to
zero.

This is a deliberate, owner-written **inversion of the zero-authorizes-nothing
doctrine**, and it is scoped precisely:

- It applies **only** to a mandate that states `model_discretion=True`. The flag
  defaults False, so every existing mandate keeps ADR-0016 semantics exactly.
- It applies **only** to the `max_*` ceilings. The `min_*` **floors** are still
  required in every mode, discretion included: `_validate_submitting_floors`
  validates the floors *before* the discretion waiver returns. **Discretion over
  size is not discretion over the reserve.**
- An explicitly set ceiling still binds, and still refuses when its evidence is
  absent — the "never larger than any mandate ceiling" guarantee holds for every
  ceiling the owner actually wrote down. Only an *unset* ceiling under discretion
  neither binds nor demands evidence.

The mechanism is small and local, in `chronos.supervisor.sizing._size`: a
`discretionary = capital.model_discretion` guard skips a zero-valued ceiling
rather than binding at zero, and the affordability bound (cash/buying-power floor
subtraction) is unchanged and always applies. The compiler, the floors, and the
deterministic veto are untouched.

### 3. Protected market orders (`OrderForm.MARKET`)

`OrderForm` gains a `MARKET` member. It is **not** the reference project's
unbounded `MKT`. Two properties keep it protected:

- It must be **granted in the mandate's `order_forms`** like any other form. A
  mandate that never lists it can never produce one.
- The compiler renders it as a **protected marketable limit**: `_derive_limit_price`
  sets the limit at the touch plus a bounded `MARKET_PROTECTION_COLLAR` (1%) —
  buy at `ask × 1.01`, sell at `bid × 0.99` — tick-conformed. It fills like a
  market order in any sane book, while a flash-crash print cannot fill it at an
  absurd price. Every compiled intent is therefore **still a positive-price
  limit** at the order plane. A literally unbounded venue market order remains
  unexpressible.

The compiler's form selection also flips: `_select_order_form` now prefers the
**most** aggressive granted form. M4 preferred the passive form, reasoning that
paying the spread should be an explicit grant. ADR-0017 keeps that reasoning and
turns it around: **listing an aggressive form in the mandate is the explicit
grant**, and a compiler that quietly preferred `LIMIT` anyway would be
second-guessing a written authorization. An owner who wants passive fills grants
only `LIMIT`; nothing overrides that.

### 4. The single execution plane is unchanged

The order handoff (`order_plane_handoff`) runs the **full existing pipeline** —
propose → risk → preview → confirm → submit — the same risk engine, preview,
confirmation, and ten-gate live stack a human proposal walks. A refusal at any
surface returns to the cycle as a refusal; nothing is skipped. Autonomy added a
gate stack and removed none, which is the sentence that has governed every
milestone since M2, and it still holds here.

The supervisor's structural isolation is preserved. `chronos.autonomy` still
imports nothing that can transmit, and the supervisor still cannot hold both a
broker handle and the decision contracts. The one seam is the app-plane wiring
module, which the "only the supervisor consumes the contracts" test now permits
**by explicit module name** (`api/autonomy_wiring.py`) rather than by weakening
the check.

### 5. What this ADR does NOT supersede

Everything in ADR-0016 §8 stands, unweakened: one canonical transmission
boundary and its single-transmit-site AST test; the single-writer lease and
fencing re-check adjacent to the wire; durable idempotency and replay protection;
reconciliation to broker truth; account and contract qualification; stale-data
refusal (a crossed or empty book still refuses compilation); the durable kill
switch and halt; the session and rolling-drawdown breakers; the cash and
buying-power **floors** and the reserve they protect; order and cancellation rate
limits; restart recovery and orphan handling; immutable hash-chained audit;
DEMO/non-live defaults for anything the owner did **not** grant discretion over;
and the prohibition against broker mutations from tests or CI.

The **degraded-state rule stands verbatim**: if the model, broker, market data,
clock, database, lease, contract resolver, risk engine, or reconciliation state
is unavailable, ambiguous, or stale, the system creates no new exposure, records
the denial, and alerts the owner. Facts that cannot be gathered return `None`,
and the runtime treats that as a refusal-to-run — facts are never invented to
keep a tick alive. **An AI failure never becomes permission to trade**, and
neither does maximal autonomy.

Deny-by-default is **not** globally repealed. It is inverted only for capital
ceilings, only under an explicit `model_discretion` grant, and never for floors,
scope tuples, order forms, asset classes, strategies, or data-quality
permissions. Silence is still not a grant for anything the owner did not
explicitly hand to the model's discretion.

## Consequences

- Bringing the backend up with a valid mandate file on disk is now sufficient
  for autonomous trading, including self-sized positions and protected market
  fills. This is the largest single reduction in operator friction in the
  project's history, and it is exactly what the owner directed.
- The residual risk of ADR-0016 R-29 is *broadened*, not newly created: the
  model can now size toward affordability rather than a fixed per-order ceiling
  when the owner grants discretion. It stays bounded by the floors/reserve, the
  drawdown and session breakers, any ceiling the owner did write down, the kill
  switch, and expiry. Recorded as RISK_REGISTER **R-37**.
- The persistence of the grant is bounded by revocation-survives-restart, by
  expiry, by the wrong-account and invalid-file inert paths, and by the
  digest-stamped audit trail. Recorded as RISK_REGISTER **R-38**.
- Cost accepted: a persistent mandate that re-arms on boot means an operator who
  wants trading *off* must revoke (durable) or remove the file, not merely
  restart. That is the deliberate inversion the owner chose; the revoke path is
  the off switch and it survives restart.

## Known limitations and residuals

1. **The literal unbounded market order is deliberately NOT implemented.** The
   reference project's `MKT` has no price protection; this ADR's `MARKET` is a
   collared limit. If the owner ever wants true unbounded market orders, that is
   a separate, explicit owner decision with its own ADR — it is not implied by
   "maximal autonomy," because an unbounded order is a correctness/catastrophe
   surface, not a friction ceiling. Flagged here so the choice is visible and
   was made, not defaulted.
2. **Options still refuse at the instrument seam.** `BackendGatherers.instrument_facts`
   resolves equities and crypto; an option decision refuses rather than pricing
   against a guessed strike/expiry, because chain resolution needs selection this
   wiring does not own. Autonomous options remain gated on that work, regardless
   of what a mandate lists. R-27 was listed here as a second gate; it was
   mitigated in M11, so chain selection is now the whole of what stands in the
   way — and it is the larger half.
3. **The 1% protection collar is a judgment, not a derived number.** It is wide
   enough to fill through a normal spread and narrow enough to refuse a broken
   print. A per-instrument collar (tighter for liquid large-caps, wider for thin
   names) is a refinement a future milestone can add; a single constant is the
   honest starting point.
4. **The 365-day ceiling is a judgment, like the 30-day one it replaces.** It
   bounds unattended authority to a horizon a single operator can be expected to
   revisit annually. It is longer, not infinite, on purpose.
5. **`model_discretion` widens the blast radius of a prompt injection that
   produces a differently-shaped proposal (R-30).** A self-sizing mandate lets an
   injected proposal size toward affordability rather than a fixed ceiling. The
   floors, the drawdown breaker, and the deterministic veto are the controls that
   hold; an owner who wants a hard per-order cap under injection simply sets
   `max_order_notional_usd` — which still binds under discretion — rather than
   leaving it unset.
6. **The account still holds ~USD 110.** Nothing here changes the arithmetic that
   makes small-account trading cost-dominated. Maximal autonomy is a capability
   and governance change, not a claim that operating it at this size is sound.
