# ADR-0024 — Binding promotion to the evidence that earned it

Status: **proposed — owner decision required.** No `DECISIONS.md` row until accepted.

Date: 2026-08-02

> **Note, 2026-08-09:** the interim rule this ADR recommends ("no rung above
> `shadow` without an owner decision recorded in `DECISIONS.md`") is now the
> operative mechanism of ADR-0025/D-21: the owner declares rungs personally,
> gated by ADR-0025's go-gate checklist. The build options here (A/B/C) remain
> proposed and undecided — and Option B gets *more* urgent under standing live
> authority, since a self-declared live rung is exactly the field it hardens.

Closes, if accepted: `docs/VISION_COMPLETION_PLAN.md` §6 finding 8.

## Context

`docs/VISION_COMPLETION_PLAN.md` §9 defines a six-rung promotion ladder — replay, shadow,
supervised paper, autonomous paper, live canary, capped live — and states that every
promotion artifact binds "the exact account, commit, dependencies, configuration, mandate,
strategy-policy, model/prompt/tools, compiler/resolver, evidence/data versions, criteria
digest, incidents, approval, expiry, and rollback plan", and that any material change or
health breach "automatically demotes the family to the appropriate earlier rung".

None of that machinery exists.

### What exists — verified 2026-08-02

`FamilyPromotion` appears in exactly two files: its definition in
`src/chronos/autonomy/mandate.py` and its re-export in `src/chronos/autonomy/__init__.py`.

That is the whole implementation. A rung is a value the owner types into the mandate JSON.
It is validated for internal consistency and then believed. There is:

- no evidence artifact of any kind, signed or otherwise;
- no code that *grants* a rung from evidence, and none that *demotes* on a breach;
- no binding between a rung and the strategy-policy, commit, or dataset that earned it;
- no expiry on a rung once written.

So "this family is at supervised paper" is, mechanically, an assertion by the person who
would benefit from asserting it. The kernel enforces the *limits* attached to a rung
faithfully — that part works — but nothing checks the rung was ever earned.

### Why this is not urgent and still matters

**Zero families have cleared any rung**, and no real gateway has ever been connected, so
there is currently no evidence for an artifact to bind and nothing to demote. Today's risk
is close to zero.

The risk arrives precisely when the project starts succeeding. The first time real
operating evidence exists, the temptation to write the rung by hand — because the evidence
"obviously" supports it — is exactly when a self-declared rung becomes load-bearing. This
repository's own history is four controls that were wired, documented, tested, and unable
to act; a promotion field that nothing can grant or revoke is the same shape, sitting one
level above the controls.

The sequencing argument is therefore the opposite of the usual one: this should be built
**before** the evidence exists, not after, because building it after means deciding whether
to honour evidence that predates the mechanism.

## The decision

### Option A — Signed, expiring promotion artifacts (the plan's own design)

A promotion is a content-addressed artifact binding the versions §9 enumerates, produced by
the evidence pipeline, verified before any rung-dependent decision, and expiring. The
mandate names an artifact rather than asserting a rung. A material change invalidates it by
construction, because the binding no longer matches.

- **For:** what the plan already specifies; makes rungs unforgeable-by-typing; demotion
  falls out of verification failing rather than needing a separate detector.
- **Against:** substantial — an artifact format, a producer, a verifier, a store, and
  integration with the registry ledger. Realistically its own milestone.

### Option B — Evidence-referenced rungs (a smaller step with most of the value)

A `FamilyPromotion` must name the evidence that earned it: a registry run id or campaign
manifest digest, plus the commit and strategy-policy it was earned against. The gateway
refuses a rung whose reference does not resolve, or that names a different commit or policy
than the one about to trade.

- **For:** kills the pure assertion — a rung must at least point at something real that
  matches — for a fraction of A's cost, and it composes forward into A rather than being
  thrown away.
- **Against:** a reference is not a proof. It shows evidence was named, not that the
  evidence met the criteria. Demotion still needs building.

### Option C — Forbid rungs above replay until A exists

Constrain `FamilyPromotion` to reject any rung above the lowest until real artifacts exist.

- **For:** maximally fail-closed and honest — the system stops accepting claims it cannot
  check.
- **Against:** blocks the shadow and supervised-paper rungs, which are exactly the rungs
  that must run to *produce* the evidence A would bind. It fails closed into a state where
  the evidence can never be gathered.

## Recommendation

**Option B now, Option A before the first live rung — and a hard rule in the interim.**

B is cheap, composes into A, and removes the pure-assertion property that makes the current
field dangerous. It should land before any family reaches a rung, which is still true today
and may not be in three months.

The interim rule matters as much as the code: **while rungs are self-declared, no rung above
`shadow` may be written without an owner decision recorded in `DECISIONS.md`.** That keeps
the honest part of the current design — the owner *is* the authority — while removing the
part where a rung can be edited into a JSON file with no record anywhere else.

C is rejected on a specific ground rather than on cost: it fails closed into a state where
the evidence needed to escape it cannot be produced. A safety default that makes its own
exit condition unreachable is not fail-closed, it is a deadlock wearing fail-closed's
clothes.

A must precede the first **live** rung — canary or capped live — without exception. At that
point a rung authorizes real money against real markets, and a value someone typed is not
an acceptable basis for it.

## Consequences if accepted as recommended

- `FamilyPromotion` gains required evidence-reference fields; the gateway refuses a rung
  whose reference does not resolve or whose commit/policy does not match what is about to
  trade, with exercised tests proving the refusal fires.
- The interim owner rule is recorded in `DECISIONS.md` and in the ladder's criteria, not
  only in this ADR.
- ADR-0023's rule composes here: no promotion artifact may be issued while
  `HarnessIdentity` is a constant. A rung that binds a model version against a hardcoded
  `"1"` binds nothing.
- Finding 8 closes at the B level. A remains open and gated to the first live rung.

## What this ADR does not decide

The artifact format, the producer, or the registry integration — Option A's design is its
own ADR, and should be written when the evidence pipeline it binds actually has output.
