# ADR-0048 — Queued autonomy work is bound to the authenticated credential epoch

Status: **accepted design direction — Kevin authorized Sprint 1 on 2026-09-03;
implementation remains owner-gated at merge.** Index entry: DECISIONS.md D-62. Risk entry:
RISK_REGISTER.md R-65.

## Context

ADR-0023 derives proposal identity from a registered credential, and ADR-0028 issues each
evidence bundle to a registered proposer. Their durable records retained only `proposer_id`.
That value is a reusable owner-facing label, not an immutable authentication fact.

If credential A queued a proposal or issued a bundle, the owner revoked A, and a restart
loaded credential B under the same proposer id, drain looked up B by id. The old proposal was
stamped with B's identity and version labels. A B-authenticated proposal could also cite A's
old bundle because bundle resolution compared only proposer ids. Both paths reached the safe
test handoff instead of refusing at STAMP.

The correction must survive process restart, distinguish replacement from expiry and
revocation, and preserve old rows without claiming facts the database never recorded.

## Decision

1. `ProposerRegistration.secret_sha256` is the credential epoch. It is already the
   immutable, non-secret identifier used by the durable revocation ledger.
2. `registration_binding()` additionally computes a per-entry SHA-256 over domain-separated,
   canonical JSON containing the proposer-registry schema version and the complete validated
   registration. Expiry is normalized to UTC before hashing. The digest binds proposer id,
   credential epoch, identity/version labels, expiry, and enabled state without coupling work
   to whitespace or unrelated registry entries.
3. Registry-authenticated proposal enqueue and evidence issuance persist both values beside
   `proposer_id`. They come from the exact frozen in-memory registry used by authentication,
   never from proposal content or a second file load. Impossible authentication/app-state
   disagreement refuses before either row is written.
4. Drain requires both stored values, finds the named entry in its boot-time registry, and
   compares the exact current binding before stamping identity. Missing, legacy-unbound, or
   replaced entries refuse at STAMP. If a registered row meets a runtime whose registry is
   now unset, it also refuses; removing configuration cannot downgrade authenticated work to
   the static legacy identity.
5. After validating that the stored binding is complete, drain consults durable revocation
   using the stored credential epoch before resolving the reusable proposer id. A replacement
   credential therefore cannot hide the revocation identity of queued work. If it is not
   revoked, drain performs the exact-entry match and independently checks enabled/expiry state.
6. Evidence resolution requires its stored epoch and entry digest to equal the already
   validated proposal binding. Proposer-id equality alone is insufficient.
7. Migration 0011 adds four nullable columns and never backfills them. Existing records remain
   legible but unbound; under registry/evidence binding they refuse rather than being adopted
   by current configuration.
8. Registry-off behavior is unchanged in this item. The separate Sprint 1 posture item will
   cap unauthenticated operation to SHADOW. Until then, only a genuinely all-NULL
   pre-registry row may use the static identity; any registration marker forbids fallback.
9. No authority or promotion gate is added. This decision only narrows which queued work may
   reach the deterministic admission and order planes.

## Consequences and bounds

Rotating a credential, renewing expiry, changing a version label, or editing any other
registration field intentionally invalidates outstanding proposals and bundles. The proposer
must fetch fresh evidence and submit fresh work under the new grant.

The entry digest proves correlation to an owner-authored registry record. It does not attest
which model, prompt, tool, or policy the external worker actually ran. Registry grants remain
boot-time snapshots; durable revocation remains the mid-session credential stand-down.
Authenticated posture is still optional until the separate SHADOW-cap lands. No broker,
PAPER, LIVE, promotion, profitability, or operating campaign is proved by this change.

## Verification

- `tests/safety/test_evidence_bundles_exercised.py` uses one file-backed database across
  dispose/reopen, rotates a credential, independently refuses the old queued proposal and old
  bundle, refuses registry-removal downgrade, then proves new-epoch proposal plus new-epoch
  bundle still reaches the safe no-submission handoff.
- `tests/safety/test_proposer_revocation_exercised.py` proves the stored old epoch retains the
  `PROPOSER_REVOKED` identity even after a same-id replacement is loaded.
- `tests/safety/test_proposer_credentials_exercised.py` proves route persistence, canonical
  per-entry binding, unbound refusal, and write refusal when authenticated app state disagrees.
- `tests/integration/test_migrations.py` constructs genuine pre-0011 table shapes, preserves
  both historical rows with NULL bindings, and passes the fail-closed schema initializer.
