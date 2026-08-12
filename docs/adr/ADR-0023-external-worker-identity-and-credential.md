# ADR-0023 — External-worker identity and the ingress credential

Status: ~~**proposed — owner decision required.** No `DECISIONS.md` row until accepted.~~
**Accepted 2026-08-12 — Option A, owner-directed** ("lets build the worker identity
protocol next", Kevin, 2026-08-12, skipping the B-then-A sequencing the recommendation
proposed; building A directly delivers B's credential split as a subset). Recorded as
D-24. Implemented the same day: the proposer registry
(`chronos.supervisor.proposers`, `AUTONOMY_PROPOSERS_FILE`), the proposal-only
`X-Chronos-Proposer-Token` credential, registration-derived provenance stamped at
drain time, and the digest honesty change (`"0" * 64` → `None`). Every "Requires"
proof below is exercised in `tests/safety/test_proposer_credentials_exercised.py`.

Date: 2026-08-02

Closes, if accepted: `docs/VISION_COMPLETION_PLAN.md` §6 finding 6.

## Context

R-35 inverted model isolation: Chronos does not call a model, a worker calls in through
`supervisor.ingress`. That inversion is sound and is not in question here — it is why the
broker-holding process holds no provider SDK, no API key, and no egress path.

Two consequences of it are unresolved.

### 1. Provenance is a constant

Every proposal arriving through the ingress is stamped with one hardcoded identity
(`src/chronos/api/autonomy_wiring.py:84-94`, applied at `:361`):

```
provider="external-worker"   model_id="ingress"        model_version="1"
prompt_version="1"           tool_schema_version="1"   decision_schema_version="1"
policy_version="1"           evidence_bundle_id="owner-workspace"
evidence_bundle_digest="0" * 64
```

So the audit trail records the same author for every decision, forever. It cannot
distinguish two workers, two models, two prompt revisions, or two evidence bundles — and
the evidence digest is sixty-four zeros, which is not a weak digest but the absence of one.

This matters beyond tidiness. `ADR-0016` promises that a promotion artifact binds "the
exact account, commit, dependencies, configuration, mandate, strategy-policy, model/prompt/
tools… evidence/data versions". None of those model-side bindings can be honoured while
the fields carrying them are constants. **Version-pinned authorship currently authenticates
the ingress, not a model.**

### 2. The credential is not proposal-only

The ingress accepts the same local API token every other mutating route accepts. A worker
holding it to submit proposals holds a credential that also reaches the routes that arm,
kill, acknowledge and revoke. R-35's guarantee — the worker holds no Chronos capability —
is true of its *address space* and not of its *credential*.

The blast radius is bounded by loopback binding and by the worker being the owner's own
process today, which is why this is finding 6 rather than an incident.

### The design constraint that shapes the fix

The obvious fix — let the worker declare its own identity — is already refused, correctly.
`supervisor.ingress` rejects writer-owned fields (`provenance`, `decision_id`) loudly
rather than stripping them, because a hostile worker must not be able to write its own
authorship into the audit trail.

So identity cannot come from the payload. It has to come from **which credential
authenticated**, which is exactly what a single shared token cannot express. The two
problems are therefore one problem: there is no per-worker credential, so there is nothing
to derive a per-worker identity from.

## The decision

### Option A — Per-worker credentials, identity derived from the credential

Issue a proposal-only credential per worker, scoped to the ingress route and nothing else.
`HarnessIdentity` is populated from the credential's registration record (provider, model,
prompt/tool/schema versions, evidence-bundle binding), never from the payload.

- **For:** fixes both halves with one mechanism and keeps the no-self-declared-provenance
  rule intact. Makes ADR-0016's promotion bindings achievable. Revoking one worker stops
  being "rotate the token and restart everything".
- **Against:** a new credential store, issuance, and expiry — a real build, and it adds an
  authorization surface, so it needs the same care R-39 got.
- **Requires:** exercised tests that the ingress credential is refused on `/orders/*`,
  `/live/*` and every other mutating route; that an unregistered credential refuses; and
  that identity in the journal matches the credential used.

### Option B — Split the credential now, defer rich identity

Ship a proposal-only credential that the other routes reject, and keep the constant
identity for now with the digest field made explicitly absent (`None`) rather than sixty-
four zeros.

- **For:** removes the sharp edge — a leaked worker credential stops being an arming
  credential — for much less work. Honest about identity instead of asserting a fake one.
- **Against:** provenance stays uninformative, so ADR-0016's model-side bindings stay
  unachievable and finding 6 only half closes.

### Option C — Refuse ingress until the protocol exists

Disable the ingress route until the full job/evidence/response protocol from plan §6 is
built.

- **For:** maximally fail-closed; nothing can propose on unverifiable provenance.
- **Against:** disables the only path by which any model can currently propose anything,
  for a risk whose realistic exposure today is a loopback-bound process the owner runs
  themselves. Fail-closed is the right default, not a reason to remove a capability nobody
  can currently misuse.

## Recommendation

> **Superseded by the owner's decision, 2026-08-12.** The owner directed Option A
> directly; the sequencing argument below is preserved as written because its gating
> rule still binds (and is now satisfiable: `HarnessIdentity` is no longer a constant
> once a registry is configured). Two honest bounds of the implementation, disclosed
> where the acceptance is recorded rather than discovered later:
>
> - **Authorship pins by version tuple, not by name.** A mandate's `VersionPins` has
>   no `proposer_id` field; "a mandate pinned to worker A refuses bridge B" holds
>   because their seven version fields differ, and two registrations that share an
>   identical version tuple are indistinguishable to admission check 8. The registry
>   permits that (only ids and credential hashes must be unique), and
>   `mandate check` lists every matching registration so the ambiguity is visible.
> - **Evidence binding is still uniform.** `ProposerRegistration` carries no
>   evidence-bundle fields; every registered identity stamps the placeholder bundle
>   id and an honestly-absent digest. The job/evidence protocol remains future work
>   (see "What this ADR does not decide").
> - **The registry is a boot-time snapshot.** Both planes read the file once at
>   startup, the mandate-file precedent. Expiry travels in the snapshot and is
>   enforced live at verification and at drain; disabling or deleting a
>   registration lands at the next restart. Live mid-session revocation of a
>   single proposer would need a DB-backed revocation act (the shape mandate
>   revocation already has) and is deliberately left to future work; the live
>   stand-downs today are the kill switch, mandate revocation, and a restart.

**Option B now, Option A before any autonomous rung above shadow.**

The credential split is the part with a real, present sharp edge and a small cost, and it
should not wait. Rich identity is the part that gates *promotion* — and no family has
cleared any rung, so nothing is currently blocked by its absence. Sequencing B first buys
the safety improvement immediately without pretending the provenance problem is solved.

The gating rule is what makes B honest rather than a dodge: **no promotion artifact may be
issued while `HarnessIdentity` is a constant.** Recording an artifact that claims to bind a
model version, against a field hardcoded to `"1"`, would be exactly the class of false
evidence the promotion ladder exists to prevent. If accepted, that prohibition belongs in
the ladder's own criteria, not only here.

The zeroed digest should change in either case. `"0" * 64` reads as a computed value and is
not one; absent evidence should be represented as absent, which is the rule the terminal
already follows in showing unknown as unknown rather than as a plausible zero.

## What this ADR does not decide

The job/evidence/response protocol's shape (plan §6's job ID, evidence digest, expiry). If
Option A is taken, that protocol is its own design work and should carry its own ADR.
