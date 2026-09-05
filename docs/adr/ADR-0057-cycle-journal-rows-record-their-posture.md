# ADR-0057 — Every cycle-journal row records the posture it rests on, read from the queue row

Status: **accepted design direction — the lead ruled this design's five decisions on 2026-09-05
(D-A … D-E in the design handoff); implementation remains owner-gated at merge.** Index entry:
DECISIONS.md D-73. Risk entry: RISK_REGISTER.md R-75.

## Context

ADR-0055 put a `posture` block on every **admission** journal row — the `admitted` / `refused`
rows `durable.record_outcome` appends to `autonomy.decisions:<fingerprint>`. Its own scope note
named the gap it left:

> Scope is the admission rows only. The cycle-refusal rows `_record` writes at INGRESS and STAMP
> know their posture too and are an explicit follow-up, not this PR.

`_record` writes a **different stream**, `autonomy.cycles:<fingerprint>`, keyed by the cycle's
stage. For every refusal before admission it is the *only* row written, because `admit()` was
never called — so INGRESS and STAMP refusals had no record of the authority they rested on. The
terminal journal view reads that stream on its own, so a reader there could not recover the
posture from the sibling stream either.

## The finding that shapes this decision

Extending ADR-0055's derivation unchanged would have written something **false**.

The drain resolves identity *before* calling `run_cycle` and passes `resolved.*` down. Every
refusal path in `build_identity_resolver` returns a bare `ResolvedIdentity(refusal=…, detail=…)`,
whose binding fields default to `None`. So on any resolution refusal — revoked credential,
replaced registration, expired or disabled entry, registry removed — `run_cycle` receives `None`
for both binding values **even when the queue row was bound at enqueue**.

Measured on a row carrying a real ADR-0048 binding whose registration expired between enqueue and
drain:

```text
row as enqueued:  epoch=54b74fa3b751…  digest=914204cec953…      → the row IS bound
resolved-sourced: {"identity":"static",        …,"credential_epoch_bound":false}   ← false
row-sourced:      {"identity":"authenticated", …,"credential_epoch_bound":true}    ← true
```

A hash-chained row that misstates the authority behind it is the exact defect this family of ADRs
exists to remove, so the source is the queue row.

**This also corrects a latent divergence in ADR-0055 itself.** That ADR says the fact comes "from
the row's own epoch and entry digest"; the code read `run_cycle`'s `proposer_*` parameters, which
the drain fills from the resolved identity. No admission row was ever wrong, and none could be: an
admission row is written only after resolution **succeeds**, and on success the resolver hands back
the very values it was given, so the two sources are the same two strings on that path. The
divergence was invisible until the block was written before resolution — that is, until now.

## Decision

1. `_record` takes a **required keyword-only** `posture: DecisionPosture` and writes the same
   five-key block ADR-0055 defined onto **every** cycle-journal row — all 23 call sites, every
   stage, not only INGRESS and STAMP. A block present at some stages re-creates the "absent means
   what?" question ADR-0055 retired one stream over.
2. The posture is built **once**, near the top of `run_cycle`, before anything is parsed, from the
   drain's wiring (`registry_configured`, `bind_evidence`) and the **queue row's** ADR-0048
   binding — new `row_credential_epoch` / `row_registry_entry_digest` parameters, passed by the
   drain straight off the queue row.
3. The **admission** row moves to that same value in the same change, so one named fact has one
   derivation across both streams. Byte-neutral by construction (see the Context), and pinned by
   ADR-0055's existing goldens, which pass unchanged.
4. The `proposer_credential_epoch` / `proposer_registry_entry_digest` parameters keep their own,
   separate job — evidence-bundle resolution. They are deliberately no longer the posture's source.
5. `identity` keeps ADR-0055's two values and its derivation. It names **the authority the row
   rests on** — what the enqueue authenticated — and never claims the cycle got far enough to
   exercise it. A row refused at STAMP can honestly read `authenticated` while its `refusal` says
   the registration no longer resolves; both are true, and both are on the same row.
6. `version` does **not** bump: the key set and every key's meaning are unchanged, and no existing
   row's bytes move. The delta on a cycle row is exactly the `posture` key.
7. No schema change, no migration, `SCHEMA_VERSION` untouched. Rows written before this carry no
   block, and `durable.read_posture` already reads that as "not recorded", never `static`.

## Rejected alternatives

**Source the fact from the resolved pair, as ADR-0055's code did.** Rejected — it is the defect
above, measured.

**A third `identity` value, `"unresolved"`, at stages before STAMP succeeds.** Rejected: how far
the cycle got is a property of the cycle, not of the posture, and `stage` and `refusal` are in the
same payload. It would also make one row's posture depend on where it stopped.

**Name the resolution refusal inside the block.** Rejected: `refusal` already carries it.

**INGRESS and STAMP only** (the literal scope ADR-0055 named). Rejected under decision 1; the
narrower shape remains reachable later without a version bump if the owner prefers it.

## Consequences and bounds

Every cycle-journal row is now self-describing about the authority behind it, whatever stage it
stopped at, and a cycle that reaches admission writes the identical block to both streams — they
are built from one value in one transaction and cannot disagree. The terminal reads named keys and
is indifferent to the new one; `hash_chain.verify` recomputes from stored bytes, so old and new
rows verify unchanged.

What this does **not** do: it records a posture, it does not enforce one (ADR-0051's cap at
assembly remains the enforcement); it does not attest which model or policy ran (P1-06, an owner
decision); and it proves nothing about a broker, PAPER, LIVE, promotion or operating campaign.

## Verification

- `tests/safety/test_proposer_credentials_exercised.py` — a STAMP refusal on a **bound** row reads
  `authenticated / configured / unset / true` with the golden byte delta (the defect case); a STAMP
  refusal on a genuinely **unbound** pre-registry row reads `static / … / false`; and the resolver
  echoes the row's binding on success, which is why decision 3 is byte-neutral.
- `tests/safety/test_autonomy_cycle.py` — an INGRESS refusal carries the block sourced from the
  row, with its golden delta; an AST walk proves **every** `_record` call site states a posture;
  `_record` refuses to write without one; and a cycle row and its sibling admission row carry an
  identical block.
- `tests/safety/test_evidence_bundles_exercised.py` — ADR-0055's admission goldens, unchanged, are
  the regression proof for decision 3.
- Eight mutations, each applied alone, in the PR body.
