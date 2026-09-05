# ADR-0051 — A submitting mandate assembles only on the authenticated provenance posture

Status: **proposed — owner_gate: required; accepted at the owner's merge.** Index entry:
DECISIONS.md D-66. Risk entry: RISK_REGISTER.md R-69. Sprint 1 item P1-05 of the 2026-09-03
team review (sol's live-path review, finding P1-05).

## Context

Two settings decide who a proposal is attributed to and what evidence it is bound to:

- `AUTONOMY_PROPOSERS_FILE` (ADR-0023) — unset, the local API token authenticates proposals
  and the **static ingress identity** is stamped on every queue row; the model, prompt,
  and policy pins on the journal name the boundary that accepted the proposal, not the
  author that made it.
- `AUTONOMY_EVIDENCE_BUNDLES` (ADR-0028) — unset, every proposal cites the placeholder
  bundle and admission **check 9 compares that constant against itself**; nothing binds
  the decision to evidence this backend issued.

Both defaults were kept deliberately (the pre-ADR-0023 / pre-ADR-0028 posture is preserved
byte-for-byte, and `test_the_unset_posture_is_byte_identical_to_the_pre_adr_0028_journal`
proves it), and both were **legal for every mandate mode**. The review's constructive probe
walked a full cycle under a PAPER_AUTONOMOUS mandate on the unset posture, carrying an
arbitrary, mismatched evidence citation, and check 9 admitted it. The order plane stopped
the probe; nothing before it did. That is disclosed behaviour, and it is unsafe promotion
policy: a decision that can reach the wire was admitted on an identity nobody authenticated
and evidence nobody issued.

ADR-0048 (credential-epoch binding, PR #144) makes the *authenticated* posture honest —
queued work is bound to the exact registration that authenticated it. It cannot help a
backend that never had a registration to bind to.

## Decision

A mandate in a **submitting mode** (`SUBMITTING_AUTONOMY_MODES`: PAPER_AUTONOMOUS,
CANARY_LIVE_AUTONOMOUS, LIVE_AUTONOMOUS) assembles only when **both** halves of the
authenticated posture are configured: a proposer registry **and** evidence binding.
Either half alone is SHADOW-grade — a registry without binding leaves check 9 comparing
the placeholder against itself; binding without a registry has nobody to issue evidence to
(`evidence_posture_is_broken`, ADR-0028).

Where it bites, in order:

1. **Assembly** (`chronos.api.autonomy_wiring.build_autonomy_runtime`): after the mandate
   loads and its account matches, and **before activation**, the cap refuses with
   `UnauthenticatedSubmittingMandate`. No activation row is written — nothing downstream
   can read the grant as one that was once in force on this posture. A CRITICAL owner alert
   (`autonomy.posture_unauthenticated`) is raised, and the log line names both settings.
2. **Lifespan** (`chronos.api.main`): the typed refusal notes the typed startup fault
   `autonomy_posture_unauthenticated` — not the generic `autonomy_wiring_failed` — so the
   health document says *the posture is wrong*, not *assembly crashed*. The backend still
   boots and can still close positions; autonomy is inert.
3. **Preflight** (`python -m chronos.cli mandate check`): a submitting mandate on the static
   posture is a BLOCKING finding, `SUBMITTING_MODE_ON_STATIC_POSTURE`, so the owner learns
   at authoring time rather than from a run of refusals.

The unset posture is **unchanged for SHADOW and every non-submitting mode**. The
byte-identical journal test still passes, because the compatibility posture survives
exactly where it is SHADOW-grade.

## Consequences

- A PAPER campaign now requires an owner-authored proposer registry and
  `AUTONOMY_EVIDENCE_BUNDLES=true`. That is the point: paper evidence gathered on the static
  posture would not have been evidence of an authenticated system.
- Existing test fixtures that booted a PAPER mandate on the static posture now configure the
  authenticated posture (`tests/integration/test_terminal_api.py`,
  `tests/safety/test_autonomy_wiring.py`); the old shape is asserted as the refusal it is.
- Together with ADR-0048, a submitting backend's proposals are attributed to an immutable
  credential epoch and a canonical registration digest, because every submitting backend now
  has a registration to bind to.

## Residuals (stated, not solved here)

- **Admission-time journal field.** The cap lives at assembly and in the health document.
  A per-decision security-posture field in the admission journal (so a journal row is
  self-describing about the posture it was judged under) is a separate change: it alters
  every journal row and therefore the byte-identical compatibility test, and belongs in its
  own PR.
- **Registration labels are declarations.** Model, prompt, and policy pins on a registration
  are what the owner wrote, not runtime attestation (P1-06, owner design decision).
- **Not covered by this cap:** a registry that is configured but invalid. That posture
  already refuses every proposal at the route and at STAMP (ADR-0023) and is reported by
  preflight as `proposer-registry-invalid`; assembly proceeds, and nothing can be proposed.
- No broker, PAPER, LIVE, promotion, or operating evidence results from this change.
