# ADR-0055 — Every admission journal row records the provenance posture it was judged under

Status: **accepted design direction — the lead ruled the design's three decisions on 2026-09-04
(Option 2, admission rows only, absence reads "not recorded"); implementation remains
owner-gated at merge.** Index entry: DECISIONS.md D-70. Risk entry: RISK_REGISTER.md R-73.

## Context

ADR-0051 capped submitting mandates to the authenticated provenance posture and left one
residual: *"A per-decision security-posture field in the admission journal (so a journal row is
self-describing about the posture it was judged under) is a separate change: it alters every
journal row and therefore the byte-identical compatibility test, and belongs in its own PR."*

The admission journal is the hash-chained `admitted` / `refused` rows `durable.record_outcome`
appends (`src/chronos/supervisor/durable.py`). Until this ADR a row carried `decision_id`,
`admitted`, `refusal`, `detail` and the check list — and nothing about whether the identity it
judged was the static ingress stamp or a registry-authenticated proposer, whether evidence
binding was in force or check 9 was comparing the placeholder against itself, or whether the
row was bound to a credential epoch (ADR-0048) or was a legacy row. A reader could *infer* two
of those from `provenance.proposer_id == ""` and check 9's detail. Inference across two fields
is what the residual named as not good enough, and "absence means legacy" is the ambiguity
ADR-0048 and ADR-0051 spent the same sprint removing from the queue and the assembly.

## Decision

1. `record_outcome` takes a required `posture: DecisionPosture` and writes a `posture` block on
   every admission row:

   ```json
   "posture": {"version": 1, "identity": "static" | "authenticated",
               "registry": "configured" | "unset", "evidence_binding": "in_force" | "unset",
               "credential_epoch_bound": true | false}
   ```

   The three facts are recorded **as the cycle saw them** — `registry_configured` from the
   drain (whether a resolver was wired), `evidence_binding` from `run_cycle`'s `bind_evidence`,
   `credential_epoch_bound` from the row's own epoch and entry digest — never re-read from
   settings, which can change between rows.
2. `identity` is **derived, never set**: `authenticated` iff a registry was configured AND the
   row itself was bound. A registry-configured runtime meeting an unbound legacy row reads
   `static` about itself rather than inheriting the runtime's posture. Evidence binding is
   recorded beside identity and does not enter into it.
3. The block lives **in the hash-chained payload**, not in a column. The journal row is the
   tamper-evident record; a column on `autonomy_decision_attempts` would be a second, mutable
   copy of the same claim. No migration; `SCHEMA_VERSION` 12 is unchanged.
> **Amendment, 2026-09-05 (ADR-0057).** Decision 2 below says `credential_epoch_bound` is read
> from the row's own epoch and entry digest. Until ADR-0057 the *code* read `run_cycle`'s
> `proposer_*` parameters, which the drain fills from the **resolved** identity. No admission row
> was ever wrong — an admission row is written only after resolution succeeds, and the resolver
> hands back the values it was given, so the two sources are identical on that path — but the
> divergence became a false statement the moment the block was written before resolution.
> ADR-0057 moves the source to the queue row, for both streams, and this ADR's goldens are the
> proof that no admission byte moved.

4. **Absence reads "not recorded", never "static".** `durable.read_posture` returns `None` for a
   row with no block, refuses a version it does not know, and never infers a posture from
   `proposer_id` or check details. That is ADR-0048's NULL rule applied to the journal.
5. **The byte-identity behind ADR-0028's acceptance criterion is retired for the bytes and kept
   for the meaning.** The criterion was "a posture switch must not quietly change the default
   posture's *behaviour*". Its test asserted semantics — admission outcome, provenance, check 9,
   no evidence rows — not bytes. Those assertions all stand, the test is renamed to say what it
   proves, and it gains the exact byte delta as a golden: today's payload minus the `posture` key
   canonicalises to the pre-ADR-0055 bytes captured at #151's head. Behaviour did not move; the
   record gained one key. ADR-0028 carries a dated note.
6. Scope is the admission rows only. The cycle-refusal rows `_record` writes at INGRESS and
   STAMP know their posture too and are an explicit follow-up, not this PR.

## Rejected alternatives

**Omit the block under the unset posture** (bytes stay identical). Rejected: a row with no
block would then mean either "pre-ADR-0055" or "unset posture", and the field's presence would
itself depend on configuration — the plausible-absence shape ADR-0023 forbids.

**A `journal_version: 2` with both shapes writable.** Rejected: two writers for one stream is a
second place for the truth to fork, and nothing needs v1 rows to keep being written.

**A column on `autonomy_decision_attempts`.** Rejected (decision 3).

## Consequences and bounds

Every new admission row is self-describing about identity posture, evidence posture and epoch
binding. Old rows read "not recorded". `hash_chain.verify` recomputes from stored bytes, so old
and new rows verify unchanged; the terminal's chain view reads named keys (`decision_id`,
`refusal`, `detail`) and is indifferent to the new one.

What this does **not** do: it records the posture, it does not enforce one — ADR-0051's cap at
assembly remains the enforcement; it does not attest which model or policy ran (P1-06, owner
decision); it does not touch the `_record` rows (decision 6); it proves nothing about a broker,
PAPER, LIVE, promotion or operating campaign.

## Verification

- `tests/safety/test_supervisor_durable_state.py`: the keyword is required (no silent omission
  path); the block is exactly five keys at version 1; `identity` derivation over all eight
  combinations; a pre-change row reads `None` and an unknown version is refused.
- `tests/safety/test_evidence_bundles_exercised.py`: the renamed semantic test keeps every prior
  assertion, pins the unset evidence posture's block (`authenticated / configured / unset /
  true` — that fixture runs with a registry and a bound row) and the golden byte delta; a
  bound row under evidence binding reads `authenticated / configured / in_force / true`.
- `tests/safety/test_autonomy_runtime.py`: the drain passes `registry_configured` equal to the
  resolver's presence (call-site pin); a tick with no registry journals the all-static block.
- Mutation table in the PR: each guard removed alone fails exactly the tests named.
