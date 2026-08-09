# ADR-0025 — Owner-directed live experimentation: the owner builds, Chronos trades

Status: **accepted — owner directive, 2026-08-09.** DECISIONS.md row D-21.

Date: 2026-08-09

## The directive

Recorded from the owner, 2026-08-09, lightly punctuated:

> I'm willing to risk my own capital to see if this AI algo trading bot can
> trade like me or better. I want to outsource my day-trading tasks to AI.
> This is my hobby: I build and improve Chronos, while Chronos trades and
> implements my investment strategies.

This is the same class of decision as the 2026-07-25 directive that produced
ADR-0016/0017, and it is handled the same way: recorded, scoped, and
implemented through structure. Prior ADRs are not rewritten — the house rule is
supersession with history visible, which is also what protects the owner's own
reasoning trail.

## What this decides

### 1. The project's operating posture

Chronos is the owner's hobby build whose *purpose* is delegated trading: the
owner authors strategies and improves the platform; Chronos executes them. The
two 10/10 outcomes (platform, proven trader) stand, but they gain an explicit
operating mode between them: **live experimentation at owner-capped size**.
The platform's job is to make that experiment safe and honest — not to forbid
it until the multi-year evidence ladder completes.

### 2. Live sequencing is re-scoped: the ladder gates claims, not the owner

The promotion ladder (plan §9) and its calendar (plan §12) remain the
unchanged, frozen bar for **claims** — "validated," "proven," "trades better
than the owner" may only ever be asserted on that evidence, and live P&L at
small N stays anecdote, recorded as such. What changes is **sequencing**: an
owner-authorized live mandate at bounded size no longer waits for the full
prospective ladder. It waits for the mechanical-readiness checklist in §"Go
gate" below. The ladder keeps running *concurrently* — reconciled live records
are prospective evidence feeding the same registry/replay machinery, so the
experiment and the evidence accumulate together instead of in sequence.

### 3. Standing authority wins the finding-4 contradiction

"Chronos trades while I build" is the standing-authority model ADR-0017
already chose in prose. This ADR resolves plan §6 finding 4 in that
direction: **the persistent mandate is the live authority; the per-session
arming requirement in the live path is to be retired** in a reviewed change
with exercised tests. Until that change lands, the code wins over this prose
(AGENTS.md precedence rule 2) and arming remains in force. The kill switch's
absolute precedence over any mandate is unchanged and non-negotiable.

### 4. The ADR-0024 interim rule is exercised, not bypassed

Rungs above SHADOW remain owner-declared decisions recorded in DECISIONS.md
(never a session's act, never a model's). D-21 is the standing record that the
owner intends to declare EQUITY-family rungs for the lead strategy up to
CANARY_LIVE_AUTONOMOUS **once the go gate below is satisfied**, by authoring
the mandate and its DECISIONS.md row personally. ADR-0024's Option B
(evidence-referenced rungs) remains proposed and is *more* urgent under this
posture, not less.

## What this does not change — the load-bearing list

Every deterministic execution-correctness mechanism survives this directive
unweakened, exactly as ADR-0017 scoped "maximal":

- single transmit site; ten-gate live stack; the ADR-0009 nine-conjunct live
  configuration; writer lease; reconciliation gates (D-20 cadence and evidence
  expiry); stale-data refusal; protected collared MARKET form;
- kill-switch absolute precedence, and the platform halt's opposite-default
  disclosure (docs/INCIDENT_RESPONSE.md);
- deny-by-default mandates: floors, ceilings, and loss limits stay mandatory,
  typed by the owner, validated at authoring time (`chronos mandate check`);
- model isolation (R-35 inversion): the model plane still holds no broker
  path, no credential, no registry import, and cannot widen its own authority;
- the research discipline in full: registry-counted trials, certified reads,
  replay artifacts, preregistration, frozen thresholds, burn-once holdouts.

Capital risk buys experience. It does not buy evidence, and no document —
including this one — may launder the first into the second.

## Go gate: required before the first live-armed mandate

Frozen now, before observation. Every item is an owner action or produces an
artifact the owner can point to:

1. **Capital and loss numbers.** A funded account and the owner's typed
   capital allocation, per-order ceiling, and loss limits in the mandate
   (validation already refuses a submitting mandate without them). The current
   snapshot ≈ USD 110 cannot express a meaningful position in the panel; the
   long-open capital-envelope question is hereby narrowed to "fund to what
   number" — the number is the owner's and is still open.
2. **Read-only gateway campaign complete** (plan §7): ≥5 sessions including a
   restart, sanitized evidence captured, fixtures replay offline exactly. No
   real gateway has ever been connected; this stays the first physical step.
3. **Paper machinery proof:** ≥20 order lifecycles across ≥10 paper sessions
   through the identical propose→preview→confirm→submit path. This is a
   plumbing floor, not an alpha gate; it is frozen here so it cannot be
   negotiated against a good-looking week.
4. **Parity proof for the lead strategy:** one real TradingView export of the
   Five-Tool trace replayed against the Chronos twin (A-03 closes).
5. **Market-data subscription decision** (owner queue #5).
6. **Mandate authored by the owner**, `chronos mandate check` clean, and one
   live kill-switch engage/disengage drill performed and audit-logged.

## Day-trading honesty

"Outsource my day trading" begins as **daily-decision delegation**: the lead
strategy (Five-Tool v3.6) is a daily-bar, confirmed-close system, which is
also the only form the account can currently trade. True intraday day trading
runs into PDT (sub-$25k margin accounts get 3 day trades per 5 sessions; cash
accounts trade against settlement) and needs intraday data the repo does not
ingest. D-12's intraday scoping therefore stands until the owner funds past
the PDT floor and an intraday lane gets its own ADR, data, and preregistration.

## The benchmark for "like me or better"

A comparison requires a frozen benchmark. Phase-0 economics (plan §5) gains
one owner input: either the owner's own discretionary track record (supplied
as a dated series), or a declared proxy (e.g., SPY total return plus a frozen
hurdle). Chosen before comparison, compared at frozen horizons, with small-N
honesty applying in both directions — including when Chronos looks better.

## Supersessions and amendments

- **Plan §9/§12** — amended (dated note in the plan): the ladder is the claims
  bar and runs concurrently with owner-authorized live experimentation; the
  24–36-month calendar governs *claims*, not the owner's right to trade their
  own account under the go gate.
- **Finding 4** (plan §6) — resolved in the standing-authority direction;
  implementation is follow-up work, owner-reviewed, with exercised tests.
- **ADR-0024** — dated note added: the interim owner-decision rule is the
  mechanism D-21 uses; the ADR's build options remain proposed.
- **D-12** — intraday scoping unchanged; daily-bar delegation proceeds.

## Consequences and sharpened open items

1. Retiring live session-arming (finding 4) becomes sanctioned, tracked work.
2. Finding 3's code half (recovery boots kill-engaged) grows in importance
   under standing authority — a restore that silently re-arms a standing
   mandate is exactly the hazard it closes. Recommended as the companion
   change to №1; still owner-reviewed.
3. ADR-0022 (arming authority model) is largely subsumed by §3 above and
   should be decided or withdrawn against it; ADR-0023's proposal-only worker
   credential gets sharper the day standing live authority exists. Both remain
   the owner's per-ADR calls.
4. The five-tool campaign's owner-evidence blocker (Phase-0 freezes) is now
   also the go gate's item 1 and benchmark input — one sitting of owner
   decisions unblocks both the research campaign and the live experiment.
